"""T-W4-007 AI 总线路由单元测试.

验收对照：
  #1 ai_call 按 policy.yaml 路由；L0 返回空，L1/L2/L3 命中不同模型配置
  #2 返回结构化结果（内容+token+耗时+模型）；异常走 fallback
  #3 核心域非 ai/ 禁止 import openai/deepseek/anthropic（CI 静态扫描）
  #4 make accept TASK=T-W4-007 全绿（本文件即单元测试主体）
  #5 不 import 学科包/学段包

测试不消耗真实 API：_RecordingClient（鸭子类型实现 LLMClient Protocol）+ StubClient。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.core.ai.bus.models import AIResult
from src.core.ai.bus.router import (
    ai_call,
    get_policy,
    reload_policy,
)


class _RecordingClient:
    """记录调用参数的 mock 客户端（实现 LLMClient Protocol）.

    fail=True 时 complete() 抛 RuntimeError，用于测试 fallback 链路。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> AIResult:
        if self._fail:
            raise RuntimeError(f"mock failure for {model}")
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return AIResult(
            content=f"ok:{model}",
            model=model,
            token_in=len(prompt),
            token_out=10,
            duration_ms=1.5,
        )


# ── 验收 #1：L0 返回空，不调用供应商 ────────────────────────────────

def test_l0_returns_empty_without_calling_client() -> None:
    """L0（不用 AI）应直接返回空内容，且不触发任何客户端调用."""
    client = _RecordingClient()
    result = ai_call("L0", "hello", clients={"deepseek": client})
    assert result.content == ""
    assert result.token_in == 0
    assert result.token_out == 0
    assert result.model == "L0-noop"
    assert result.fallback is False
    assert client.calls == [], "L0 不应调用任何客户端"


# ── 验收 #1：L1/L2/L3 命中不同模型配置 ──────────────────────────────

def test_l1_l2_l3_hit_different_models() -> None:
    """L1/L2/L3 应分别命中 policy.yaml 中配置的不同模型."""
    client = _RecordingClient()
    ai_call("L1", "p1", clients={"deepseek": client}, bypass_pii_filter=True)
    ai_call("L2", "p2", clients={"deepseek": client}, bypass_pii_filter=True)
    ai_call(
        "L3",
        "p3",
        clients={"deepseek": client, "litellm": client},
        bypass_pii_filter=True,
    )
    models = [c["model"] for c in client.calls]
    assert len(models) == 3, f"应调用 3 次，实际 {len(models)}"
    assert len(set(models)) == 3, f"L1/L2/L3 应命中 3 个不同模型，实际 {models}"
    policy = get_policy()
    assert models[0] == policy["levels"]["L1"]["model"]
    assert models[1] == policy["levels"]["L2"]["model"]
    assert models[2] == policy["levels"]["L3"]["model"]


def test_temperature_and_max_tokens_passed_through() -> None:
    """policy 中的 temperature/max_tokens 应透传到客户端."""
    client = _RecordingClient()
    ai_call("L2", "p", clients={"deepseek": client}, bypass_pii_filter=True)
    policy = get_policy()
    cfg = policy["levels"]["L2"]
    assert client.calls[0]["temperature"] == cfg["temperature"]
    assert client.calls[0]["max_tokens"] == cfg["max_tokens"]


# ── 验收 #2：返回结构化结果 ────────────────────────────────────────

def test_structured_result_fields() -> None:
    """AIResult 含 content/model/token_in/token_out/duration_ms/fallback."""
    client = _RecordingClient()
    result = ai_call(
        "L1", "hello", clients={"deepseek": client}, bypass_pii_filter=True
    )
    assert result.content == "ok:deepseek-chat"
    assert result.model == "deepseek-chat"
    assert result.token_in == 5
    assert result.token_out == 10
    assert result.duration_ms == 1.5
    assert result.fallback is False


# ── 验收 #2：异常时走 fallback 供应商 ──────────────────────────────

def test_l3_fallback_on_primary_failure() -> None:
    """L3 主供应商失败（超时/5xx/配额）后自动切备供应商，标记 fallback=True.

    policy L3：主 provider=litellm/gpt-4o，fallback provider=deepseek/deepseek-reasoner。
    故 primary(fail) 注入 litellm，fallback 注入 deepseek。
    """
    primary = _RecordingClient(fail=True)
    fallback = _RecordingClient()
    result = ai_call(
        "L3",
        "p",
        clients={"litellm": primary, "deepseek": fallback},
        bypass_pii_filter=True,
    )
    assert result.fallback is True
    policy = get_policy()
    assert result.model == policy["levels"]["L3"]["fallback"]["model"]
    assert fallback.calls, "fallback 客户端应被调用"
    assert "primary_error" in result.raw


def test_no_fallback_configured_raises() -> None:
    """L1/L2 无 fallback 配置，主客户端失败应向上抛异常（不静默吞错）."""
    primary = _RecordingClient(fail=True)
    with pytest.raises(RuntimeError):
        ai_call(
            "L1", "p", clients={"deepseek": primary}, bypass_pii_filter=True
        )


# ── 验收 #1：未知 level 报错 ───────────────────────────────────────

def test_unknown_level_raises() -> None:
    """未知 task_level 应报 ValueError（policy 无此级别）."""
    with pytest.raises(ValueError):
        ai_call("L9", "p", bypass_pii_filter=True)  # type: ignore[arg-type]


# ── 验收 #2：默认桩客户端兜底（未注入任何 client） ──────────────────

def test_default_stub_client_when_no_injection() -> None:
    """未注入 client 时用 StubClient 兜底，router 仍可工作（007 独立验收场景）."""
    result = ai_call("L1", "p", bypass_pii_filter=True)
    assert result.content.startswith("[stub:")
    assert result.model  # 非空


# ── context 透传 ───────────────────────────────────────────────────

def test_context_passed_to_raw() -> None:
    """context（artifact_ref 等）应记入 result.raw 供 ledger 消费."""
    client = _RecordingClient()
    result = ai_call(
        "L1",
        "p",
        context={"artifact_ref": "item_revision:abc123"},
        clients={"deepseek": client},
        bypass_pii_filter=True,
    )
    assert result.raw["context"]["artifact_ref"] == "item_revision:abc123"


# ── 验收 #3：核心域非 ai/ 禁止 import openai/deepseek/anthropic ────

def test_no_vendor_import_in_non_ai_core() -> None:
    """静态扫描：src/core/ 下非 ai/ 的 .py 不得 import 供应商 SDK.

    为什么扫描非 ai/ 而非全仓：ai/adapter/（T-W4-009）是供应商 SDK 的合法落脚点；
    其余核心域（models/events/gate/...）直连供应商即绕过总线，违反 §4.8 收口原则
    与任务卡验收 #3（CI 静态扫描强制）。
    """
    core_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core"
    )
    assert core_dir.is_dir(), f"目录不存在：{core_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:openai|deepseek|anthropic)"
        r"|import\s+(?:openai|deepseek|anthropic))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(core_dir.rglob("*.py")):
        rel = py_file.relative_to(core_dir)
        # ai/ 子包豁免：adapter 是供应商 SDK 的合法落脚点（T-W4-009）
        if rel.parts[0] == "ai":
            continue
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(rel))
    assert not violations, (
        f"src/core/<非 ai>/ 直连供应商 SDK（违反 §4.8 总线收口 / 验收 #3）：{violations}"
    )


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_ai_bus() -> None:
    """src/core/ai/bus/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    bus_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "bus"
    )
    assert bus_dir.is_dir(), f"目录不存在：{bus_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(bus_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(bus_dir)))
    assert not violations, f"ai/bus 存在学科包 import（违反 A5）：{violations}"


# ── policy 热加载（T-W4-009 验收 #3 前置能力） ─────────────────────

def test_policy_reload_clears_cache() -> None:
    """reload_policy 后缓存失效，再次 get_policy 重新读盘."""
    p1 = get_policy()
    reload_policy()
    p2 = get_policy()
    # 内容一致（文件未变），但缓存已重建（p1 与 p2 是不同 dict 对象）
    assert p1 is not p2, "reload 后应返回新 dict（缓存已重建）"
    assert p1["contract_version"] == p2["contract_version"]
    assert p1["levels"].keys() == p2["levels"].keys()


def test_policy_has_four_levels() -> None:
    """policy.yaml 必须含 L0/L1/L2/L3 四档（架构 v2 §4.8）."""
    policy = get_policy()
    assert set(policy["levels"].keys()) == {"L0", "L1", "L2", "L3"}
    assert policy["levels"]["L0"]["use_ai"] is False
    for lvl in ("L1", "L2", "L3"):
        assert policy["levels"][lvl]["use_ai"] is True
        assert "provider" in policy["levels"][lvl]
        assert "model" in policy["levels"][lvl]
    # L3 必须有 fallback 配置（双供应商预案）
    assert "fallback" in policy["levels"]["L3"]
