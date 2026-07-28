"""T-W4-046 年度全量重放首演报告生成器单元测试.

覆盖任务卡验收 §1-§5：
  §1 annual_replay_report.py 读取全部历史 response_event，用当前活跃估计器重算，输出报告。
  §2 报告含：重算参数分布、与旧参数差异统计、一致性率、异常项列表、
     ActiveModelPointer 版本映射。
  §3 新旧参数并存验证：切换前后报告分别引用各自版本的参数（数据库查询实证）。
  §4 make accept TASK=T-W4-046 全绿（本文件即单元测试主体）。
  §5 不 import 任何学科包/学段包.

宪法 D6 估计器可替换 + R-D-05 可重判：replay_all 写平行 score_run，
原 response_event.scoring_trace 永不改动；新旧参数并存于 item_param 表.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.data.active_model_pointer import ActiveModelPointer
from src.core.data.replay import replay_all
from src.core.scoring.service import score_and_record
from scripts.jobs.annual_replay_report import (
    AnnualReplayReport,
    ParamCoexistence,
    VersionMapping,
    generate_annual_replay_report,
    render_report_markdown,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：清理表 + 插入 item/item_version + 事件落账 + 估计器版本登记
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_all(async_session: AsyncSession):
    """每测试前清空相关表（response_event append-only，TRUNCATE CASCADE 清外键依赖）."""
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.execute(text("TRUNCATE TABLE score_run RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE estimator_run CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item_param RESTART IDENTITY CASCADE"))
    await async_session.commit()
    yield


def _choice_iv(item_version_id: str) -> dict:
    """单选题：正解 B，干扰项 A 绑错误类型."""
    return {
        "item_version_id": item_version_id,
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {"option_value": "A", "label": "常见错误",
             "error_type_id": "test.et.a", "collision": False, "corpus_ref": None},
        ],
    }


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
    """插入最小 item + item_version（满足 score_run FK + run_scorer 调度需要）."""
    item_id = f"item-for-{item_version_id[-8:]}"
    await db.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"
            " ON CONFLICT (item_id) DO NOTHING"
        ),
        {"iid": item_id},
    )
    scoring_ref = json.dumps(
        {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        ensure_ascii=False,
    )
    interaction_ref = json.dumps(
        {"interaction_id": "single_choice", "interaction_params": {}},
        ensure_ascii=False,
    )
    error_bindings = json.dumps(
        [{"option_value": "A", "label": "常见错误",
          "error_type_id": "test.et.a", "collision": False, "corpus_ref": None}],
        ensure_ascii=False,
    )
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage)"
            " VALUES (:vid, :iid, 'draft', '{}'::jsonb,"
            " CAST(:iref AS jsonb), '{}'::jsonb,"
            " CAST(:sref AS jsonb), CAST(:ebs AS jsonb), '{}'::jsonb)"
            " ON CONFLICT (item_version_id) DO NOTHING"
        ),
        {
            "vid": item_version_id, "iid": item_id,
            "iref": interaction_ref, "sref": scoring_ref, "ebs": error_bindings,
        },
    )
    await db.commit()


async def _record_event(
    db: AsyncSession,
    item_version_id: str,
    *,
    selected: str,
    scene: str = "practice",
    now: datetime | None = None,
) -> None:
    """落账一条作答事件（用真实 score_and_record 落 response_event）."""
    await _insert_item_version(db, item_version_id)
    await score_and_record(
        db,
        item_version=_choice_iv(item_version_id),
        response={"selected": selected},
        student_alias_id=uuid4(),
        scene=scene,
        pack_id="platform",
        now=now or datetime.now(timezone.utc),
    )


async def _set_active_estimator(
    db: AsyncSession,
    *,
    scope: str,
    model_version: str,
    activated_at: datetime,
) -> None:
    """登记活跃估计器版本（ActiveModelPointer.set_active）."""
    ptr = ActiveModelPointer(db)
    await ptr.set_active(
        scope,
        model_version,
        code_digest=f"sha256:code-{model_version}",
        input_snapshot_id=f"snap-{model_version}",
        graph_release_id=f"graph-{model_version}",
        activated_at=activated_at,
    )


async def _insert_item_param(
    db: AsyncSession,
    *,
    item_version_id: str,
    scope: str,
    method_version: str,
    params: dict,
    as_of: datetime,
    sample_size: int = 100,
) -> None:
    """插入一条 item_param 行（实证新旧参数并存）."""
    param_id = f"param-{item_version_id[-8:]}-{scope}-{method_version}-{as_of.strftime('%Y%m%d')}"
    await db.execute(
        text(
            "INSERT INTO item_param"
            " (param_id, item_version_id, purpose_scope, source, params,"
            "  sample_size, method_version, as_of)"
            " VALUES (:pid, :iv, :scope, 'measured_ctt', CAST(:p AS jsonb),"
            "  :n, :mv, :as_of)"
            " ON CONFLICT (item_version_id, purpose_scope, source, method_version, as_of)"
            " DO NOTHING"
        ),
        {
            "pid": param_id, "iv": item_version_id, "scope": scope,
            "p": json.dumps(params, ensure_ascii=False),
            "n": sample_size, "mv": method_version, "as_of": as_of,
        },
    )
    await db.commit()


# ════════════════════════════════════════════════════════════════════
# §1：generate_annual_replay_report 读取历史事件 + 重算 + 输出报告
# ════════════════════════════════════════════════════════════════════


class TestGenerateReport:
    """generate_annual_replay_report 完整链路（验收 #1）."""

    async def test_generates_report_with_all_sections(self, async_session: AsyncSession):
        """§1：报告含全部段落（replay / version_mapping / anomalies / param_coexistence / summary）."""
        # 准备：1 个活跃估计器 + 2 个事件
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-a", selected="B")
        await _record_event(async_session, "sha256:rpt-iv-b", selected="A")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice", run_label="test-rpt-001",
        )

        # 验收 #1：报告含五段
        assert isinstance(report, AnnualReplayReport)
        assert report.scope == "practice"
        assert report.run_label == "test-rpt-001"
        assert "rescored_count" in report.replay
        assert "current_active" in report.version_mapping
        assert isinstance(report.anomalies, list)
        assert "total_param_rows" in report.param_coexistence
        assert "total_events" in report.summary

    async def test_replay_reads_all_historical_events(self, async_session: AsyncSession):
        """§1：replay_all 读取该场景全部历史事件（rescored_count = 事件数）."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        # 3 个 practice 事件
        await _record_event(async_session, "sha256:rpt-iv-1", selected="B")
        await _record_event(async_session, "sha256:rpt-iv-2", selected="A")
        await _record_event(async_session, "sha256:rpt-iv-3", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        # 验收 #1：重算数 = 历史事件数（3）
        assert report.replay["rescored_count"] == 3
        assert report.summary["total_events"] == 3
        assert report.summary["rescored"] == 3
        assert report.summary["failed"] == 0

    async def test_replay_uses_current_active_estimator(self, async_session: AsyncSession):
        """§1：replay 引用当前活跃估计器版本（version_mapping.current_active）."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-active", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        # 验收 #1：当前活跃版本是 ctt-v1
        assert report.version_mapping["current_active"] == "ctt-v1"
        assert report.summary["active_version"] == "ctt-v1"


# ════════════════════════════════════════════════════════════════════
# §2：报告含版本映射 + 异常项 + 参数差异 + 一致性率
# ════════════════════════════════════════════════════════════════════


class TestVersionMappingAndAnomalies:
    """验收 #2：版本映射 + 异常项列表 + 参数差异 + 一致性率."""

    async def test_version_mapping_shows_switch_history(self, async_session: AsyncSession):
        """§2：版本映射显示切换历史（旧版本 retired + 新版本 active）."""
        # 先登记 ctt-v1，再登记 ctt-v2（ctt-v1 自动退役）
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v2",
            activated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        vm = report.version_mapping
        # 当前活跃是 ctt-v2
        assert vm["current_active"] == "ctt-v2"
        assert vm["new_version"] == "ctt-v2"
        # 旧版本列表含 ctt-v1
        assert "ctt-v1" in vm["old_versions"]
        # 历史含 2 条登记
        assert len(vm["history"]) == 2
        # ctt-v1 is_active=False，ctt-v2 is_active=True
        active_flags = {h["model_version"]: h["is_active"] for h in vm["history"]}
        assert active_flags["ctt-v1"] is False
        assert active_flags["ctt-v2"] is True

    async def test_anomalies_extracted_from_failures(self, async_session: AsyncSession):
        """§2：异常项列表从 replay failures 提取（评分失败的事件）."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        # 1 个正常事件
        await _record_event(async_session, "sha256:rpt-iv-ok", selected="B")
        # 1 个会失败的事件：item_version 不存在（直接写 response_event 绕过 _insert_item_version）
        bad_eid = uuid4()
        await async_session.execute(
            text(
                "INSERT INTO response_event"
                " (event_id, student_alias_id, item_version_id, scene, raw_payload,"
                "  scoring_trace, error_inferences, created_at)"
                " VALUES (:eid, :sid, :iv, 'practice', '{}'::jsonb,"
                "  '{}'::jsonb, '[]'::jsonb, :ts)"
            ),
            {
                "eid": bad_eid, "sid": uuid4(),
                "iv": "sha256:rpt-iv-not-exist",
                "ts": datetime.now(timezone.utc),
            },
        )
        await async_session.commit()

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        # 验收 #2：异常项列表非空，含失败原因
        assert len(report.anomalies) >= 1
        anomaly = report.anomalies[0]
        assert "event_id" in anomaly
        assert "item_version_id" in anomaly
        assert "reason" in anomaly
        assert "not found" in anomaly["reason"] or "scorer" in anomaly["reason"]

    async def test_param_diff_distribution_present(self, async_session: AsyncSession):
        """§2：重算参数差异统计段（param_diff_distribution）存在."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-diff", selected="B")
        await _record_event(async_session, "sha256:rpt-iv-diff2", selected="A")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        # 验收 #2：参数差异统计段
        assert "old_param_summary" in report.replay
        assert "new_param_summary" in report.replay
        assert "param_diff_distribution" in report.replay
        # old_param_summary 含 scorer_versions 与 correct_distribution
        assert "scorer_versions" in report.replay["old_param_summary"]
        assert "correct_distribution" in report.replay["old_param_summary"]

    async def test_consistency_rate_computed(self, async_session: AsyncSession):
        """§2：一致性率字段存在且在 [0, 1] 区间."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-cons", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        assert 0.0 <= report.replay["consistency"] <= 1.0
        assert 0.0 <= report.summary["consistency"] <= 1.0


# ════════════════════════════════════════════════════════════════════
# §3：新旧参数并存验证
# ════════════════════════════════════════════════════════════════════


class TestParamCoexistence:
    """验收 #3：新旧参数并存于 item_param 表（DB 查询实证）."""

    async def test_coexistence_detects_multi_version_params(
        self, async_session: AsyncSession
    ):
        """§3：同题在 ctt-v1 + ctt-v2 都有参数行 → coexisting_items 含之."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v2",
            activated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        iv = "sha256:rpt-iv-coexist"
        await _insert_item_version(async_session, iv)
        # 旧版本参数（ctt-v1，as_of=2026-01-15）
        await _insert_item_param(
            async_session, item_version_id=iv, scope="practice",
            method_version="ctt-v1",
            params={"difficulty": 0.4, "discrimination": 1.2},
            as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        # 新版本参数（ctt-v2，as_of=2026-07-15）
        await _insert_item_param(
            async_session, item_version_id=iv, scope="practice",
            method_version="ctt-v2",
            params={"difficulty": 0.5, "discrimination": 1.5},
            as_of=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        pc = report.param_coexistence
        # 验收 #3：同题在 2 个版本下都有参数 → 并存
        assert iv in pc["coexisting_items"]
        assert len(pc["by_item"][iv]) == 2
        assert "ctt-v1" in pc["by_item"][iv]
        assert "ctt-v2" in pc["by_item"][iv]
        # 参数内容各版本不同（并存实证）
        old_params = pc["by_item"][iv]["ctt-v1"]["params"]
        new_params = pc["by_item"][iv]["ctt-v2"]["params"]
        assert old_params["difficulty"] == 0.4
        assert new_params["difficulty"] == 0.5

    async def test_coexistence_excludes_single_version_items(
        self, async_session: AsyncSession
    ):
        """§3：只有 1 个版本参数的题不进 coexisting_items."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        iv = "sha256:rpt-iv-single"
        await _insert_item_version(async_session, iv)
        await _insert_item_param(
            async_session, item_version_id=iv, scope="practice",
            method_version="ctt-v1",
            params={"difficulty": 0.3},
            as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        pc = report.param_coexistence
        # 单版本题不在并存列表
        assert iv not in pc["coexisting_items"]
        # 但 by_item 仍记录之（仅 1 个版本）
        assert len(pc["by_item"].get(iv, {})) == 1

    async def test_param_coexistence_total_rows(self, async_session: AsyncSession):
        """§3：total_param_rows = 该场景 item_param 总行数."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        # 插 3 行 item_param
        for i in range(3):
            iv = f"sha256:rpt-iv-count-{i}"
            await _insert_item_version(async_session, iv)
            await _insert_item_param(
                async_session, item_version_id=iv, scope="practice",
                method_version="ctt-v1",
                params={"difficulty": 0.3 + i * 0.1},
                as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
            )

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )

        assert report.param_coexistence["total_param_rows"] == 3


# ════════════════════════════════════════════════════════════════════
# Markdown 渲染
# ════════════════════════════════════════════════════════════════════


class TestMarkdownRendering:
    """render_report_markdown 渲染报告为 Markdown（基于模板）."""

    async def test_markdown_contains_all_sections(self, async_session: AsyncSession):
        """Markdown 含全部段落标题（与模板逐段对齐）."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-md", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice", run_label="md-test",
        )
        md = render_report_markdown(report)

        # 段落标题（与 docs/replay-report-template.md 对齐）
        assert "# 年度全量重放首演报告 — practice" in md
        assert "## 元信息" in md
        assert "## 摘要" in md
        assert "## ActiveModelPointer 版本映射" in md
        assert "## 重算参数分布与差异统计" in md
        assert "## 一致性率" in md
        assert "## 异常项列表" in md
        assert "## 新旧参数并存实证" in md

    async def test_markdown_includes_version_history_table(
        self, async_session: AsyncSession
    ):
        """Markdown 含版本历史表（run_id / model_version / activated_at / retired_at）."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v2",
            activated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-md2", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )
        md = render_report_markdown(report)

        # 表头
        assert "| run_id | model_version | activated_at | retired_at | is_active |" in md
        # 两个版本行
        assert "ctt-v1" in md
        assert "ctt-v2" in md

    async def test_markdown_empty_anomalies_note(self, async_session: AsyncSession):
        """无异常项时 Markdown 显示「无异常项」注释."""
        await _set_active_estimator(
            async_session, scope="practice", model_version="ctt-v1",
            activated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await _record_event(async_session, "sha256:rpt-iv-noanomaly", selected="B")

        report = await generate_annual_replay_report(
            async_session, purpose_scope="practice",
        )
        md = render_report_markdown(report)

        assert "无异常项" in md


# ════════════════════════════════════════════════════════════════════
# §5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


class TestNoPackImport:
    """验收 #5：不 import 学科包/学段包（A5 静态实证）."""

    def test_annual_replay_report_module_does_not_import_packs(self):
        """annual_replay_report.py 不 import 学科包/学段包."""
        src = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "jobs"
            / "annual_replay_report.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "packs.",
            "gradeband_low",
            "subject-math",
            "subject-chinese",
            "subject-english",
        )
        for needle in forbidden:
            assert needle not in src, f"annual_replay_report.py 含禁用 import: {needle!r}"

    def test_replay_report_template_does_not_reference_packs(self):
        """replay-report-template.md 不引用学科包/学段包."""
        src = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "replay-report-template.md"
        ).read_text(encoding="utf-8")
        forbidden = (
            "subject-math",
            "subject-chinese",
            "subject-english",
            "gradeband_low",
        )
        for needle in forbidden:
            assert needle not in src, f"replay-report-template.md 含禁用引用: {needle!r}"
