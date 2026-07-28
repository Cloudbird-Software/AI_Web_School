"""T-W4-008 LLM 调用台账单元测试.

验收对照：
  #1 record_call 写入台账，字段完整（任务/模型/prompt_hash/token_in/token_out/
     cost_cny/artifact_ref）
  #3 台账查询支持按 item_revision 归集（T-W4-010 消费 query_by_artifact）
  #4 make accept 全绿
  #5 不 import 学科包/学段包

测试隔离：用 set_default_ledger(Ledger(tmp_path)) 注入临时台账，不污染开发库。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.core.ai.ledger.ledger import (
    Ledger,
    compute_cost_cny,
    hash_prompt,
    query_by_artifact,
    record_call,
    set_default_ledger,
)
from src.core.ai.ledger.schemas import LedgerEntry


@pytest.fixture
def isolated_ledger(tmp_path: Path) -> Ledger:
    """每个测试用独立 tmp_path 台账，互不污染."""
    ledger = Ledger(tmp_path / "ai_ledger.jsonl")
    set_default_ledger(ledger)
    yield ledger
    set_default_ledger(None)


# ── 验收 #1：record_call 写入字段完整 ──────────────────────────────

def test_record_call_returns_call_id(isolated_ledger: Ledger) -> None:
    """record_call 返回全局唯一 call_id（ULID）."""
    call_id = record_call(
        task_level="L2",
        task_name="draft_passage",
        provider="deepseek",
        model="deepseek-reasoner",
        prompt="请起草一篇三年级阅读语篇",
        token_in=20,
        token_out=500,
        duration_ms=1500.0,
        artifact_ref="item_revision:abc123",
    )
    assert call_id, "call_id 非空"
    assert len(call_id) >= 20, f"ULID 长度，实际：{len(call_id)}"


def test_record_call_writes_all_fields(isolated_ledger: Ledger) -> None:
    """台账记录字段完整：任务/模型/prompt_hash/token/cost/artifact_ref."""
    record_call(
        task_level="L2",
        task_name="draft_passage",
        provider="deepseek",
        model="deepseek-reasoner",
        prompt="请起草语篇",
        token_in=15,
        token_out=400,
        duration_ms=1200.0,
        prompt_version="v2",
        task_stage="draft",
        fallback=False,
        artifact_ref="item_revision:rev001",
        raw_meta={"pii": ["stripped:name"]},
    )
    entries = isolated_ledger.query_all()
    assert len(entries) == 1
    e = entries[0]
    # 验收 #1 逐字段
    assert e.task_level == "L2"
    assert e.task_name == "draft_passage"
    assert e.provider == "deepseek"
    assert e.model == "deepseek-reasoner"
    assert e.prompt_hash == hash_prompt("请起草语篇")
    assert e.prompt_version == "v2"
    assert e.token_in == 15
    assert e.token_out == 400
    assert e.cost_cny > 0, "成本应按单价表算出正值"
    assert e.duration_ms == 1200.0
    assert e.fallback is False
    assert e.artifact_ref == "item_revision:rev001"
    assert e.task_stage == "draft"
    assert e.raw_meta == {"pii": ["stripped:name"]}


def test_prompt_not_stored_only_hash(isolated_ledger: Ledger) -> None:
    """prompt 原文不入账（防 PII 残留），只存 prompt_hash."""
    prompt_text = "学生张三的隐私内容"
    record_call(
        task_level="L1",
        task_name="validate",
        provider="deepseek",
        model="deepseek-chat",
        prompt=prompt_text,
        token_in=10,
        token_out=5,
        duration_ms=100.0,
    )
    # 读取原始 JSONL 行，确认不含 prompt 原文
    raw_lines = isolated_ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1
    raw_json = json.loads(raw_lines[0])
    assert "prompt" not in raw_json, "prompt 原文不应出现在台账"
    assert raw_json["prompt_hash"] == hash_prompt(prompt_text)
    # PII 原文不应残留
    assert "张三" not in raw_lines[0]


# ── 验收 #1：cost_cny 计算 ─────────────────────────────────────────

def test_cost_cny_computation() -> None:
    """成本按模型单价表计算（T-W4-010 归集依赖此函数一致）."""
    # deepseek-chat: in 0.001/1K, out 0.002/1K
    cost = compute_cost_cny("deepseek-chat", token_in=1000, token_out=1000)
    assert cost == pytest.approx(0.001 + 0.002, rel=1e-6)
    # deepseek-reasoner: in 0.004/1K, out 0.016/1K
    cost = compute_cost_cny("deepseek-reasoner", token_in=1000, token_out=1000)
    assert cost == pytest.approx(0.004 + 0.016, rel=1e-6)
    # 未知模型零成本（不阻断）
    assert compute_cost_cny("unknown-model", 1000, 1000) == 0.0


def test_cost_cny_override(isolated_ledger: Ledger) -> None:
    """显式 cost_cny 覆盖单价表计算（特殊计费场景）."""
    record_call(
        task_level="L1",
        task_name="score",
        provider="deepseek",
        model="deepseek-chat",
        prompt="p",
        token_in=100,
        token_out=50,
        duration_ms=200.0,
        cost_cny=99.9,
    )
    e = isolated_ledger.query_all()[0]
    assert e.cost_cny == 99.9


# ── 验收 #3：按 item_revision 归集（T-W4-010 消费此接口） ──────────

def test_query_by_artifact(isolated_ledger: Ledger) -> None:
    """query_by_artifact 返回该 item_revision 全生命周期调用."""
    # 模拟单题全生命周期：起草 → 验证 → 评分 → 重判
    for stage, model, tokens in [
        ("draft", "deepseek-reasoner", (100, 800)),
        ("validate", "deepseek-chat", (50, 30)),
        ("score", "deepseek-chat", (80, 40)),
        ("rescore", "deepseek-reasoner", (80, 45)),
    ]:
        record_call(
            task_level="L2" if stage in ("draft", "rescore") else "L1",
            task_name=stage,
            provider="deepseek",
            model=model,
            prompt=f"prompt-{stage}",
            token_in=tokens[0],
            token_out=tokens[1],
            duration_ms=500.0,
            task_stage=stage,
            artifact_ref="item_revision:single-item-001",
        )
    # 另一题的调用（不应被查到）
    record_call(
        task_level="L1",
        task_name="validate",
        provider="deepseek",
        model="deepseek-chat",
        prompt="other",
        token_in=10,
        token_out=5,
        duration_ms=100.0,
        artifact_ref="item_revision:other-item-002",
    )

    entries = query_by_artifact("item_revision:single-item-001")
    assert len(entries) == 4, f"应返回 4 条生命周期调用，实际 {len(entries)}"
    stages = [e.task_stage for e in entries]
    assert stages == ["draft", "validate", "score", "rescore"]
    # 全部关联同一 artifact_ref
    assert all(e.artifact_ref == "item_revision:single-item-001" for e in entries)


def test_query_by_artifact_empty(isolated_ledger: Ledger) -> None:
    """不存在的 artifact_ref 返回空列表."""
    assert query_by_artifact("item_revision:nonexistent") == []


def test_query_by_artifact_no_artifact_ref(isolated_ledger: Ledger) -> None:
    """artifact_ref=None 的调用不被任意 artifact_ref 查询命中."""
    record_call(
        task_level="L1",
        task_name="ad_hoc",
        provider="deepseek",
        model="deepseek-chat",
        prompt="p",
        token_in=5,
        token_out=5,
        duration_ms=50.0,
        artifact_ref=None,
    )
    assert query_by_artifact("item_revision:any") == []


# ── append-only：多次调用累加 ──────────────────────────────────────

def test_append_only_accumulates(isolated_ledger: Ledger) -> None:
    """台账 append-only：多次 record_call 累加，不覆盖."""
    for i in range(5):
        record_call(
            task_level="L1",
            task_name="t",
            provider="deepseek",
            model="deepseek-chat",
            prompt=f"p{i}",
            token_in=1,
            token_out=1,
            duration_ms=10.0,
        )
    assert len(isolated_ledger.query_all()) == 5


def test_ledger_file_is_jsonl(isolated_ledger: Ledger) -> None:
    """台账文件为 JSONL（每行一条 JSON），append-only 友好."""
    record_call(
        task_level="L1", task_name="t", provider="deepseek", model="deepseek-chat",
        prompt="p", token_in=1, token_out=1, duration_ms=10.0,
    )
    record_call(
        task_level="L2", task_name="t2", provider="deepseek", model="deepseek-reasoner",
        prompt="p2", token_in=2, token_out=2, duration_ms=20.0,
    )
    lines = isolated_ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "每条记录占一行"
    for line in lines:
        entry = json.loads(line)  # 每行合法 JSON
        assert "call_id" in entry
        assert "model" in entry


# ── LedgerEntry Pydantic 校验 ──────────────────────────────────────

def test_ledger_entry_negative_tokens_rejected() -> None:
    """token_in/token_out 不允许负值（Pydantic Field(ge=0)）."""
    from datetime import datetime, timezone
    with pytest.raises(Exception):
        LedgerEntry(
            call_id="x",
            task_level="L1",
            task_name="t",
            provider="deepseek",
            model="deepseek-chat",
            prompt_hash="abc",
            token_in=-1,
            token_out=0,
            cost_cny=0.0,
            duration_ms=0.0,
            created_at=datetime.now(timezone.utc),
        )


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_ledger() -> None:
    """src/core/ai/ledger/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    ledger_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "ledger"
    )
    assert ledger_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(ledger_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(ledger_dir)))
    assert not violations, f"ai/ledger 存在学科包 import（违反 A5）：{violations}"
