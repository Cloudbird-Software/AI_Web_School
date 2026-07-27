"""T-W2-021 任务卡遥测单元测试.

对照任务卡四条验收标准：
  1. .agent/telemetry/task_event.schema.json 定义字段与类型
  2. telemetry.record(event) 以 JSONL 追加到 .agent/telemetry/events-YYYYMM.jsonl
  3. python tools/opc dashboard 输出 markdown 产能报告（吞吐/一次通过率/单位成本）
     —— dashboard 已在 tests/unit/test_opc.py 验证；本文件覆盖 render_markdown
  4. 单元测试覆盖记录、读取、聚合

实现策略：所有 fixture 用 tmp_path 注入 tele_dir，避免污染真实 .agent/telemetry/。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

# 注入 tools/ 到 sys.path 以便 import telemetry（无 __init__.py 的目录）
import sys
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import telemetry  # noqa: E402  (sys.path 注入后可导入)


# ────────────────────────────────────────────────────────────────────
# 验收 #1：task_event.schema.json 字段与类型定义
# ────────────────────────────────────────────────────────────────────

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / ".agent"
    / "telemetry"
    / "task_event.schema.json"
)


def test_schema_file_exists():
    """schema 文件存在于 .agent/telemetry/task_event.schema.json。"""
    assert SCHEMA_PATH.is_file(), f"schema 不存在: {SCHEMA_PATH}"


def test_schema_is_valid_json_with_expected_shape():
    """schema 是合法 JSON 且具备 JSON Schema 关键字段。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["type"] == "object"
    # 任务卡 deliverable #1：定义字段与类型
    required = set(schema["required"])
    # 任务卡概念字段 task_id / model / status / gate_signal / token_cost / duration
    # 在 schema 中映射为：
    #   task_id（直接）+ model（直接）+ verifier_verdict+merged（status）
    #   + gate_results（gate_signal）+ tokens_in/out + cost_cny（token_cost）
    #   + started_at + finished_at（duration）
    assert "task_id" in required
    assert "model" in required
    assert "task_type" in required
    assert "started_at" in required
    assert "finished_at" in required
    props = schema["properties"]
    # 任务卡列出的概念字段都能在 schema 中找到对应
    for field in (
        "task_id", "model", "model_tier", "verifier_verdict", "merged",
        "gate_results", "tokens_in", "tokens_out", "cost_cny",
        "started_at", "finished_at", "attempts", "escalated", "misreport",
    ):
        assert field in props, f"schema 缺字段 {field}"
    # 类型约束
    assert props["task_id"]["type"] == "string"
    assert props["model"]["type"] == "string"
    assert props["tokens_in"]["type"] == "integer"
    assert props["cost_cny"]["type"] == "number"
    assert props["merged"]["type"] == "boolean"
    # gate_results 子对象有 unit/contract/golden
    gr = props["gate_results"]["properties"]
    for k in ("unit", "contract", "golden"):
        assert k in gr
        assert gr[k]["enum"] == ["pass", "fail", "review"]


# ────────────────────────────────────────────────────────────────────
# 验收 #2：record(event) 追加 JSONL 到 events-YYYYMM.jsonl
# ────────────────────────────────────────────────────────────────────

def _sample_event(task_id: str = "T-W2-019", **overrides) -> dict:
    """构造一个符合 schema 的样例事件（供多测试复用）。"""
    base = {
        "task_id": task_id,
        "wave": "W2",
        "role": "builder",
        "model": "deepseek/deepseek-chat",
        "model_tier": "T1",
        "task_type": "impl",
        "owner_module": "tests/",
        "started_at": "2026-07-27T09:00:00Z",
        "finished_at": "2026-07-27T11:30:00Z",
        "tokens_in": 100000,
        "tokens_out": 12000,
        "cost_cny": 1.8,
        "attempts": 1,
        "escalated": False,
        "gate_results": {"unit": "pass", "contract": "pass", "golden": "pass"},
        "verifier_verdict": "PASS",
        "pr": "#123",
        "merged": True,
        "misreport": False,
    }
    base.update(overrides)
    return base


def test_record_creates_monthly_jsonl_with_hyphen_name(tmp_path: Path):
    """record() 写入到 events-YYYYMM.jsonl（连字符命名，与 dashboard glob 一致）。"""
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    path = telemetry.record(
        _sample_event(), tele_dir=tmp_path, when=when
    )
    assert path == tmp_path / "events-202608.jsonl"
    assert path.is_file()
    # 文件内一行 JSON
    content = path.read_text(encoding="utf-8").splitlines()
    assert len(content) == 1
    parsed = json.loads(content[0])
    assert parsed["task_id"] == "T-W2-019"


def test_record_appends_multiple_events(tmp_path: Path):
    """多次 record 追加到同一文件，不覆盖既有内容。"""
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    for tid in ("T-W2-019", "T-W2-020", "T-W2-021"):
        telemetry.record(_sample_event(tid), tele_dir=tmp_path, when=when)
    path = tmp_path / "events-202608.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    ids = [json.loads(l)["task_id"] for l in lines]
    assert ids == ["T-W2-019", "T-W2-020", "T-W2-021"]


def test_record_creates_tele_dir_if_missing(tmp_path: Path):
    """tele_dir 不存在时 record() 自动创建（mkdir parents=True）。"""
    nested = tmp_path / "nested" / "tele"
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    telemetry.record(_sample_event(), tele_dir=nested, when=when)
    assert (nested / "events-202608.jsonl").is_file()


def test_record_splits_by_month(tmp_path: Path):
    """不同月份的事件写到不同文件。"""
    telemetry.record(
        _sample_event("T-W2-001"),
        tele_dir=tmp_path,
        when=datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc),
    )
    telemetry.record(
        _sample_event("T-W2-002"),
        tele_dir=tmp_path,
        when=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
    )
    assert (tmp_path / "events-202607.jsonl").is_file()
    assert (tmp_path / "events-202608.jsonl").is_file()


def test_record_uses_default_tele_dir_when_none(tmp_path: Path, monkeypatch):
    """tele_dir=None 时落到 TELEMETRY_DIR；测试 monkeypatch 到 tmp_path。"""
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path)
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    path = telemetry.record(_sample_event(), when=when)
    assert path == tmp_path / "events-202608.jsonl"


# ────────────────────────────────────────────────────────────────────
# 验收 #4a：读取
# ────────────────────────────────────────────────────────────────────

def test_read_events_empty_when_dir_missing(tmp_path: Path):
    """目录不存在时返回空列表，不抛异常。"""
    assert telemetry.read_events(tele_dir=tmp_path / "nope") == []


def test_read_events_returns_all_rows_sorted_by_filename(tmp_path: Path):
    """read_events 按 events-*.jsonl 文件名升序合并事件。"""
    # 7 月文件 + 8 月文件，文件名升序 = 7 月先于 8 月
    (tmp_path / "events-202607.jsonl").write_text(
        json.dumps({"task_id": "T-W2-001"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "events-202608.jsonl").write_text(
        json.dumps({"task_id": "T-W2-002"}) + "\n"
        + json.dumps({"task_id": "T-W2-003"}) + "\n",
        encoding="utf-8",
    )
    rows = telemetry.read_events(tele_dir=tmp_path)
    assert [r["task_id"] for r in rows] == ["T-W2-001", "T-W2-002", "T-W2-003"]


def test_read_events_ignores_blank_lines(tmp_path: Path):
    """空行（含尾行换行）不应导致 json.loads 抛错。"""
    (tmp_path / "events-202608.jsonl").write_text(
        json.dumps({"task_id": "T-W2-001"}) + "\n\n"
        + json.dumps({"task_id": "T-W2-002"}) + "\n",
        encoding="utf-8",
    )
    rows = telemetry.read_events(tele_dir=tmp_path)
    assert len(rows) == 2


def test_read_events_ignores_non_matching_files(tmp_path: Path):
    """只读 events-*.jsonl，不读 README / schema / 其它前缀文件。"""
    (tmp_path / "events-202608.jsonl").write_text(
        json.dumps({"task_id": "T-W2-001"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# not telemetry", encoding="utf-8")
    (tmp_path / "task_event.schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events_old.jsonl").write_text(
        json.dumps({"task_id": "OLD"}) + "\n", encoding="utf-8"
    )
    rows = telemetry.read_events(tele_dir=tmp_path)
    assert [r["task_id"] for r in rows] == ["T-W2-001"]


# ────────────────────────────────────────────────────────────────────
# 验收 #4b：聚合
# ────────────────────────────────────────────────────────────────────

def test_aggregate_empty_returns_zeros():
    """空输入返回全零聚合结果。"""
    agg = telemetry.aggregate([])
    assert agg["total"] == 0
    assert agg["merged"] == 0
    assert agg["total_cost_cny"] == 0.0
    assert agg["unit_cost_cny"] == 0.0
    assert agg["first_pass_rate"] == 0.0
    assert agg["by_model_task_type"] == {}
    assert agg["misreport_count"] == 0


def test_aggregate_counts_total_and_merged():
    """total=事件总数；merged=merged=true 的事件数。"""
    rows = [
        _sample_event("T-1", merged=True),
        _sample_event("T-2", merged=False),
        _sample_event("T-3", merged=True),
    ]
    agg = telemetry.aggregate(rows)
    assert agg["total"] == 3
    assert agg["merged"] == 2


def test_aggregate_sums_total_cost():
    """total_cost_cny = sum(cost_cny)。"""
    rows = [
        _sample_event("T-1", cost_cny=1.5),
        _sample_event("T-2", cost_cny=2.5),
        _sample_event("T-3", cost_cny=0.0),
    ]
    agg = telemetry.aggregate(rows)
    assert agg["total_cost_cny"] == pytest.approx(4.0)


def test_aggregate_unit_cost_divides_by_merged():
    """unit_cost_cny = total_cost / merged_count。"""
    rows = [
        _sample_event("T-1", cost_cny=3.0, merged=True),
        _sample_event("T-2", cost_cny=1.0, merged=True),
        _sample_event("T-3", cost_cny=6.0, merged=False),  # 不计入分母
    ]
    agg = telemetry.aggregate(rows)
    # total = 3+1+6 = 10；merged = 2；unit = 10/2 = 5
    assert agg["total_cost_cny"] == pytest.approx(10.0)
    assert agg["unit_cost_cny"] == pytest.approx(5.0)


def test_aggregate_unit_cost_zero_when_no_merged():
    """无合入时 unit_cost=0（避免除零）。"""
    rows = [_sample_event("T-1", cost_cny=10.0, merged=False)]
    agg = telemetry.aggregate(rows)
    assert agg["unit_cost_cny"] == 0.0


def test_aggregate_first_pass_rate():
    """一次通过率 = (merged && attempts=1) / merged。"""
    rows = [
        _sample_event("T-1", merged=True, attempts=1),   # 一次通过
        _sample_event("T-2", merged=True, attempts=3),   # 重试后通过
        _sample_event("T-3", merged=True, attempts=1),   # 一次通过
        _sample_event("T-4", merged=False, attempts=1),  # 未合入不计入
    ]
    agg = telemetry.aggregate(rows)
    # merged=3，first_pass=2，rate=2/3
    assert agg["first_pass_rate"] == pytest.approx(2 / 3)


def test_aggregate_first_pass_rate_zero_when_no_merged():
    """无合入时 first_pass_rate=0（避免除零）。"""
    rows = [_sample_event(merged=False)]
    agg = telemetry.aggregate(rows)
    assert agg["first_pass_rate"] == 0.0


def test_aggregate_groups_by_model_task_type():
    """按 (model, task_type) 分组聚合 n / pass / pass_rate。"""
    rows = [
        _sample_event(model="deepseek/chat", task_type="impl",
                      verifier_verdict="PASS"),
        _sample_event(model="deepseek/chat", task_type="impl",
                      verifier_verdict="FAIL"),
        _sample_event(model="zhipu/glm-4", task_type="docs",
                      verifier_verdict="PASS"),
    ]
    agg = telemetry.aggregate(rows)
    mt = agg["by_model_task_type"]
    assert mt["deepseek/chat|impl"]["n"] == 2
    assert mt["deepseek/chat|impl"]["pass"] == 1
    assert mt["deepseek/chat|impl"]["pass_rate"] == pytest.approx(0.5)
    assert mt["zhipu/glm-4|docs"]["n"] == 1
    assert mt["zhipu/glm-4|docs"]["pass_rate"] == pytest.approx(1.0)


def test_aggregate_demote_when_n_ge_20_and_pass_rate_lt_60():
    """n=20 且 pass_rate=50% (<60%) → demote=True。"""
    rows = []
    for i in range(20):
        rows.append(_sample_event(
            model="deepseek/chat", task_type="impl",
            verifier_verdict="PASS" if i < 10 else "FAIL",
        ))
    agg = telemetry.aggregate(rows)
    mt = agg["by_model_task_type"]["deepseek/chat|impl"]
    assert mt["n"] == 20
    assert mt["pass_rate"] == pytest.approx(0.5)
    assert mt["demote"] is True


def test_aggregate_no_demote_when_n_lt_20():
    """n=19（<20）即使 pass_rate<60% 也不降权（样本量过小）。"""
    rows = []
    for i in range(19):
        rows.append(_sample_event(
            model="deepseek/chat", task_type="impl",
            verifier_verdict="PASS" if i < 9 else "FAIL",
        ))
    agg = telemetry.aggregate(rows)
    mt = agg["by_model_task_type"]["deepseek/chat|impl"]
    assert mt["n"] == 19
    assert mt["pass_rate"] < 0.6
    assert mt["demote"] is False


def test_aggregate_no_demote_when_pass_rate_ge_60():
    """n>=20 但 pass_rate=70%（>=60%）不降权。"""
    rows = []
    for i in range(20):
        rows.append(_sample_event(
            model="deepseek/chat", task_type="impl",
            verifier_verdict="PASS" if i < 14 else "FAIL",
        ))
    agg = telemetry.aggregate(rows)
    mt = agg["by_model_task_type"]["deepseek/chat|impl"]
    assert mt["pass_rate"] == pytest.approx(0.7)
    assert mt["demote"] is False


def test_aggregate_counts_misreport():
    """misreport_count = misreport=true 的事件数。"""
    rows = [
        _sample_event("T-1", misreport=False),
        _sample_event("T-2", misreport=True),
        _sample_event("T-3", misreport=True),
    ]
    agg = telemetry.aggregate(rows)
    assert agg["misreport_count"] == 2


# ────────────────────────────────────────────────────────────────────
# 验收 #3：render_markdown 产出 markdown 产能报告
# ────────────────────────────────────────────────────────────────────

def test_render_markdown_empty_returns_no_data_message():
    """无数据时返回 '暂无遥测数据'。"""
    md = telemetry.render_markdown([])
    assert "暂无遥测数据" in md
    assert md.startswith("# 产能报告")


def test_render_markdown_has_total_section_and_metrics():
    """markdown 包含 # 产能报告 标题、## 总览 段落、三大量产指标。"""
    rows = [
        _sample_event("T-1", merged=True, attempts=1, cost_cny=2.0),
        _sample_event("T-2", merged=True, attempts=2, cost_cny=4.0),
    ]
    md = telemetry.render_markdown(rows)
    assert "# 产能报告" in md
    assert "## 总览" in md
    # 吞吐
    assert "任务总数: 2" in md
    assert "已合入: 2" in md
    # 单位成本
    assert "累计成本: ¥6.00" in md
    assert "单位合入成本: ¥3.00" in md
    # 一次通过率（1/2=50%）
    assert "一次通过率: 50.0%" in md


def test_render_markdown_has_model_task_type_table():
    """markdown 含 ## 模型×任务类型通过率 段落与表格。"""
    rows = [
        _sample_event(model="deepseek/chat", task_type="impl",
                      verifier_verdict="PASS"),
        _sample_event(model="deepseek/chat", task_type="impl",
                      verifier_verdict="FAIL"),
    ]
    md = telemetry.render_markdown(rows)
    assert "## 模型×任务类型通过率" in md
    # 表格行：| 模型 | 任务类型 | n | 通过 | 通过率 | 降权 |
    assert "| deepseek/chat | impl | 2 | 1 | 50% |" in md


def test_render_markdown_shows_demote_flag():
    """n>=20 且 pass_rate<60% 时 markdown 显示 ⛔。"""
    rows = []
    for i in range(20):
        rows.append(_sample_event(
            model="deepseek/chat", task_type="impl",
            verifier_verdict="PASS" if i < 10 else "FAIL",
        ))
    md = telemetry.render_markdown(rows)
    assert "⛔" in md


def test_render_markdown_no_demote_flag_when_high_pass_rate():
    """通过率 >=60% 时 markdown 不含 ⛔。"""
    rows = []
    for i in range(20):
        rows.append(_sample_event(
            model="deepseek/chat", task_type="impl",
            verifier_verdict="PASS" if i < 14 else "FAIL",
        ))
    md = telemetry.render_markdown(rows)
    assert "⛔" not in md


# ────────────────────────────────────────────────────────────────────
# 端到端：record → read_events → aggregate → render_markdown 闭环
# ────────────────────────────────────────────────────────────────────

def test_end_to_end_record_read_aggregate_render(tmp_path: Path):
    """完整闭环：record 写入 → read_events 读取 → aggregate 聚合 → render_markdown 渲染。"""
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    # 写 3 条事件：2 个一次通过、1 个失败
    telemetry.record(
        _sample_event("T-W2-019", merged=True, attempts=1, cost_cny=1.5),
        tele_dir=tmp_path, when=when,
    )
    telemetry.record(
        _sample_event("T-W2-020", merged=True, attempts=1, cost_cny=2.5),
        tele_dir=tmp_path, when=when,
    )
    telemetry.record(
        _sample_event("T-W2-099", merged=False, attempts=3,
                      verifier_verdict="FAIL", cost_cny=5.0),
        tele_dir=tmp_path, when=when,
    )

    rows = telemetry.read_events(tele_dir=tmp_path)
    assert len(rows) == 3

    agg = telemetry.aggregate(rows)
    assert agg["total"] == 3
    assert agg["merged"] == 2
    assert agg["total_cost_cny"] == pytest.approx(9.0)
    assert agg["unit_cost_cny"] == pytest.approx(4.5)  # 9/2
    assert agg["first_pass_rate"] == pytest.approx(1.0)  # 2/2

    md = telemetry.render_markdown(rows)
    assert "任务总数: 3" in md
    assert "已合入: 2" in md
    assert "累计成本: ¥9.00" in md
    assert "单位合入成本: ¥4.50" in md
    assert "一次通过率: 100.0%" in md
