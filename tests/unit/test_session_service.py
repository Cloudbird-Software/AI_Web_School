"""W3-S3 在线作答会话服务单元测试.

覆盖（对应 S3 验收点）：
- 会话全流程：开始练习（实例池序列/静态卷）→ 取下一题 → 提交作答
  → 即时反馈（含按错误类型展示的解析）→ 错题回测 → 完成。
- 时长保护：L≤15 分钟、M/H≤60 分钟（注入时钟，不 sleep）；超时返回
  休息提示（RestRequiredError），休息确认（resume）后继续。
- 门纪律：未发布（draft）题目拒绝开会话。
- 序列纪律：只能作答当前应答题（OutOfSequenceError）。
- 事件字段：作答经 score_and_record 落 response_event，session_id /
  source_ref（静态卷 {paper_id, placement_token}）齐全。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.content.writer import publish_item_version
from src.core.models.paper import Paper
from src.core.models.paper_item import PaperItem
from src.core.session.service import (
    OutOfSequenceError,
    RestRequiredError,
    SessionCompletedError,
    SessionNotFoundError,
    UnpublishedItemError,
    abandon_session,
    get_next_item,
    get_session_state,
    resume_session,
    start_session,
    submit_answer,
)

# ────────────────────────────────────────────────────────────────────
# 数学包评分器注册 + response_event 清理
# ────────────────────────────────────────────────────────────────────

_MATH_SCORERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "packs" / "subject-math" / "scorers" / "__init__.py"
)


def _register_math_scorers() -> None:
    spec = importlib.util.spec_from_file_location(
        "subject_math_scorers_pkg", _MATH_SCORERS_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subject_math_scorers_pkg"] = mod
    spec.loader.exec_module(mod)
    mod.register_math_scorers()


_register_math_scorers()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_response_event(async_session: AsyncSession):
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 已发布题目构造（publish_item_version 是入库唯一路径）
# ────────────────────────────────────────────────────────────────────

# 时间锚点必须动态：response_event 按月分区（0003 迁移只建当月+未来 3 个月），
# 固定历史日期在跨月后的新库中无对应分区（no partition found，2026-08-19 CI 实证）。
# 全部用例仅使用 _T0 的相对偏移，锚定当前时刻不改变任何断言语义。
_T0 = datetime.now(timezone.utc).replace(microsecond=0)


def _version_data(
    stem: str,
    *,
    interaction_id: str = "single_choice",
    scorer_id: str = "exact_match",
    scorer_params: dict | None = None,
    error_bindings: list | None = None,
    gradeband: str = "M",
    status: str = "published",
    pack_id: str = "subject-math",
    extra_blocks: list | None = None,
) -> dict:
    blocks = [{"kind": "stem", "template": stem, "rendered": stem}]
    if extra_blocks:
        blocks.extend(extra_blocks)
    return {
        "pack_id": pack_id,
        "tier": "A",
        "status": status,
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": gradeband,
            "graph_release": "graph-v1",
        },
        "interaction_ref": {"interaction_id": interaction_id, "interaction_params": {}},
        "content": {"blocks": blocks},
        "scoring_ref": {
            "scorer_id": scorer_id,
            "scorer_params": scorer_params or {"answer": "B"},
        },
        "error_bindings": error_bindings if error_bindings is not None else [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ],
        "lineage": {
            "tier": "A",
            "pipeline": {"id": "test-pipeline", "version": "1.0"},
            "signed_by": "test-author",
            "signed_at": "2026-07-27T00:00:00Z",
        },
    }


async def _publish(
    db: AsyncSession, stem: str, *, status: str = "published", **kwargs
) -> str:
    """写一条 item_version（默认 published），返回 item_version_id."""
    result = await publish_item_version(
        item_id=None,
        version_data=_version_data(stem, status=status, **kwargs),
        # published 必须有证书 id（门强制由 writer 承载；证书表无 FK 约束）
        gate_certificate_id="cert-test-w3" if status == "published" else None,
        db=db,
    )
    return result["item_version_id"]


async def _make_paper_with_items(
    db: AsyncSession, item_version_ids: list[str], *, gradeband: str = "M"
) -> str:
    """造一份静态卷（paper + paper_item），返回 paper_id."""
    paper_id = "paper-test-" + uuid4().hex[:8]
    db.add(Paper(
        paper_id=paper_id,
        paper_code="P" + uuid4().hex[:10],
        paper_spec_id="spec-" + uuid4().hex[:10],
        paper_title="W3 会话测试卷",
        gradeband=gradeband,
        subject_pack_id="subject-math",
        kp_snapshot_ref="kp-snap-test",
        seed=42,
        created_by="test",
    ))
    await db.flush()
    for i, vid in enumerate(item_version_ids):
        db.add(PaperItem(
            paper_item_id=f"pi-{paper_id}-{i}",
            paper_id=paper_id,
            item_version_id=vid,
            placement_token=f"q{i + 1}",
            item_number=i + 1,
            item_short_code="SC" + uuid4().hex[:10],
        ))
    await db.commit()
    return paper_id


# ════════════════════════════════════════════════════════════════════
# 开始练习
# ════════════════════════════════════════════════════════════════════

class TestStartSession:
    async def test_pool_sequence_snapshot(self, async_session: AsyncSession):
        """实例池序列：快照固化 + L 段阈值 900 秒."""
        v1 = await _publish(async_session, "题一")
        v2 = await _publish(async_session, "题二")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="L",
            item_version_ids=[v1, v2],
            now=_T0,
        )
        assert s.status == "active"
        assert s.time_limit_sec == 900  # 低段 ≤15 分钟
        assert [e["item_version_id"] for e in s.item_sequence] == [v1, v2]
        assert [e["item_number"] for e in s.item_sequence] == [1, 2]

    async def test_gradeband_thresholds(self, async_session: AsyncSession):
        """时长保护阈值：L=900，M/H=3600."""
        v = await _publish(async_session, "题")
        for gb, expect in (("L", 900), ("M", 3600), ("H", 3600)):
            s = await start_session(
                async_session,
                student_alias_id=uuid4(),
                gradeband=gb,
                item_version_ids=[v],
                now=_T0,
            )
            assert s.time_limit_sec == expect, gb

    async def test_paper_bound_uses_paper_gradeband(self, async_session: AsyncSession):
        """静态卷会话：题序取 paper_item.item_number，学段缺省取 paper."""
        v1 = await _publish(async_session, "卷题一")
        v2 = await _publish(async_session, "卷题二")
        paper_id = await _make_paper_with_items(
            async_session, [v2, v1], gradeband="H"
        )
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            paper_id=paper_id,
            now=_T0,
        )
        assert s.gradeband == "H"
        # item_number 顺序 = 入卷顺序 [v2, v1]
        assert [e["item_version_id"] for e in s.item_sequence] == [v2, v1]
        assert s.item_sequence[0]["placement_token"] == "q1"

    async def test_rejects_mutual_exclusive_sources(self, async_session: AsyncSession):
        v = await _publish(async_session, "题")
        with pytest.raises(ValueError):
            await start_session(
                async_session, student_alias_id=uuid4(), gradeband="M", now=_T0
            )
        with pytest.raises(ValueError):
            await start_session(
                async_session,
                student_alias_id=uuid4(),
                gradeband="M",
                paper_id="p1",
                item_version_ids=[v],
                now=_T0,
            )

    async def test_rejects_unpublished_item(self, async_session: AsyncSession):
        """门纪律：draft 题目禁止入会话."""
        draft = await _publish(async_session, "草稿题", status="draft")
        with pytest.raises(UnpublishedItemError):
            await start_session(
                async_session,
                student_alias_id=uuid4(),
                gradeband="M",
                item_version_ids=[draft],
                now=_T0,
            )

    async def test_rejects_empty_sequence_and_bad_scene(
        self, async_session: AsyncSession
    ):
        with pytest.raises(ValueError):
            await start_session(
                async_session,
                student_alias_id=uuid4(),
                gradeband="M",
                item_version_ids=[],
                now=_T0,
            )
        v = await _publish(async_session, "题")
        with pytest.raises(ValueError):
            await start_session(
                async_session,
                student_alias_id=uuid4(),
                gradeband="M",
                scene="measurement",  # type: ignore[arg-type]
                item_version_ids=[v],
                now=_T0,
            )


# ════════════════════════════════════════════════════════════════════
# 全流程：取题 → 作答 → 反馈 → 完成
# ════════════════════════════════════════════════════════════════════

class TestFullFlow:
    async def test_full_flow_mixed_scorers(self, async_session: AsyncSession):
        """三题会话：单选(exact_match) + 数值(math_equivalence) + 单选。

        走通「取下一题→提交→即时评分→反馈→下一题…→完成」全链路。
        """
        v_choice = await _publish(async_session, "0.3 和 0.4 哪个大？")
        v_math = await _publish(
            async_session,
            "1/2 + 1/2 = ?",
            interaction_id="numeric_blank",
            scorer_id="math_equivalence",
            scorer_params={"answer_expr": "1"},
            error_bindings=[],
        )
        v_choice2 = await _publish(async_session, "0.5 和 0.6 哪个大？")

        student = uuid4()
        s = await start_session(
            async_session,
            student_alias_id=student,
            gradeband="M",
            item_version_ids=[v_choice, v_math, v_choice2],
            now=_T0,
        )

        # 题 1：答错（选干扰项 A）
        nxt = await get_next_item(async_session, s.session_id, now=_T0)
        assert nxt is not None and nxt.round == "main" and nxt.position == 1
        assert nxt.item_version_id == v_choice
        assert nxt.interaction_id == "single_choice"
        # 选项合成：正解 B + 干扰项 A，确定性乱序
        assert nxt.options is not None
        assert {o["id"] for o in nxt.options} == {"A", "B"}
        fb = await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v_choice,
            response={"selected": "A"},
            duration_ms=4000,
            now=_T0 + timedelta(seconds=30),
        )
        assert fb.correct is False
        # 按错误类型展示的反馈：error_type + 干扰项设计 label
        assert fb.error_feedback[0]["error_type_id"] == (
            "math.decimal.digits_more_is_larger"
        )
        assert fb.error_feedback[0]["label"] == "位数多的小数更大"
        assert fb.progress["main_answered"] == 1

        # 题 2：math_equivalence 答对（0.5+0.5 ≡ 1）
        nxt = await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(seconds=40)
        )
        assert nxt is not None and nxt.item_version_id == v_math
        fb2 = await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v_math,
            response={"blanks": {"b1": {"value": "1.0"}}},
            now=_T0 + timedelta(seconds=60),
        )
        assert fb2.correct is True

        # 题 3：答对
        nxt = await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(seconds=70)
        )
        assert nxt is not None and nxt.item_version_id == v_choice2
        fb3 = await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v_choice2,
            response={"selected": "B"},
            now=_T0 + timedelta(seconds=90),
        )
        assert fb3.correct is True
        assert fb3.session_status == "completed"

        # 完成后再取题 → None；再作答 → SessionCompletedError
        assert await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(seconds=100)
        ) is None
        with pytest.raises(SessionCompletedError):
            await submit_answer(
                async_session,
                s.session_id,
                item_version_id=v_choice2,
                response={"selected": "B"},
                now=_T0 + timedelta(seconds=110),
            )

        # 会话状态：进度与正确计数
        state = await get_session_state(
            async_session, s.session_id, now=_T0 + timedelta(seconds=120)
        )
        assert state.status == "completed"
        assert state.total == 3
        assert state.answered_count == 3
        assert state.correct_count == 2
        assert state.wrong_count == 1
        assert state.completed_at is not None

        # response_event：3 条事件，session_id 关联
        rows = (
            await async_session.execute(
                text(
                    "SELECT item_version_id, session_id, scene, source_ref"
                    " FROM response_event WHERE session_id = :sid"
                    " ORDER BY created_at"
                ),
                {"sid": s.session_id},
            )
        ).all()
        assert len(rows) == 3
        assert all(r.session_id == s.session_id for r in rows)
        assert all(r.scene == "practice" for r in rows)

    async def test_paper_bound_source_ref(self, async_session: AsyncSession):
        """静态卷会话：事件 source_ref 含 {paper_id, placement_token}（A4 入水口）."""
        v = await _publish(async_session, "卷题")
        paper_id = await _make_paper_with_items(async_session, [v])
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            paper_id=paper_id,
            now=_T0,
        )
        await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v,
            response={"selected": "B"},
            now=_T0 + timedelta(seconds=10),
        )
        row = (
            await async_session.execute(
                text(
                    "SELECT source_ref FROM response_event WHERE session_id = :sid"
                ),
                {"sid": s.session_id},
            )
        ).one()
        assert row.source_ref == {"paper_id": paper_id, "placement_token": "q1"}

    async def test_out_of_sequence_rejected(self, async_session: AsyncSession):
        """序列纪律：只能作答当前应答题."""
        v1 = await _publish(async_session, "题一")
        v2 = await _publish(async_session, "题二")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v1, v2],
            now=_T0,
        )
        with pytest.raises(OutOfSequenceError):
            await submit_answer(
                async_session,
                s.session_id,
                item_version_id=v2,  # 跳答第二题
                response={"selected": "B"},
                now=_T0,
            )

    async def test_explanation_in_feedback(self, async_session: AsyncSession):
        """反馈含解析（content 中 explanation 块）."""
        v = await _publish(
            async_session,
            "比较题",
            extra_blocks=[
                {"kind": "explanation", "rendered": "十分位相同比百分位。"}
            ],
        )
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v],
            now=_T0,
        )
        fb = await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v,
            response={"selected": "A"},
            now=_T0,
        )
        assert fb.explanation == ["十分位相同比百分位。"]

    async def test_unknown_session_raises(self, async_session: AsyncSession):
        with pytest.raises(SessionNotFoundError):
            await get_session_state(async_session, uuid4())


# ════════════════════════════════════════════════════════════════════
# 错题回测
# ════════════════════════════════════════════════════════════════════

class TestWrongRetest:
    async def test_retest_round_after_main(self, async_session: AsyncSession):
        """主序列走完后错题进入回测轮；回测答对 → passed → 会话完成."""
        v1 = await _publish(async_session, "易错题")
        v2 = await _publish(async_session, "普通题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v1, v2],
            retest_wrong=True,
            now=_T0,
        )
        t = _T0
        # 题 1 答错 → 错题标记
        await submit_answer(
            async_session, s.session_id,
            item_version_id=v1, response={"selected": "A"},
            now=t + timedelta(seconds=10),
        )
        state = await get_session_state(
            async_session, s.session_id, now=t + timedelta(seconds=11)
        )
        assert state.wrong_count == 1
        assert state.retest_pending == 1

        # 题 2 答对 → 主序列走完，但回测未完成，会话仍 active
        fb = await submit_answer(
            async_session, s.session_id,
            item_version_id=v2, response={"selected": "B"},
            now=t + timedelta(seconds=20),
        )
        assert fb.session_status == "active"

        # 取下一题 → 回测轮出示题 1
        nxt = await get_next_item(
            async_session, s.session_id, now=t + timedelta(seconds=30)
        )
        assert nxt is not None
        assert nxt.round == "retest"
        assert nxt.item_version_id == v1

        # 回测答对 → 标记 passed → 会话完成
        fb2 = await submit_answer(
            async_session, s.session_id,
            item_version_id=v1, response={"selected": "B"},
            now=t + timedelta(seconds=40),
        )
        assert fb2.correct is True
        assert fb2.session_status == "completed"
        assert await get_next_item(
            async_session, s.session_id, now=t + timedelta(seconds=50)
        ) is None

    async def test_retest_wrong_again_marks_failed(self, async_session: AsyncSession):
        """回测再答错 → failed；回测轮只走一遍（随后会话完成）."""
        v = await _publish(async_session, "易错题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v],
            retest_wrong=True,
            now=_T0,
        )
        await submit_answer(
            async_session, s.session_id,
            item_version_id=v, response={"selected": "A"}, now=_T0,
        )
        nxt = await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(seconds=10)
        )
        assert nxt is not None and nxt.round == "retest"
        fb = await submit_answer(
            async_session, s.session_id,
            item_version_id=v, response={"selected": "A"},
            now=_T0 + timedelta(seconds=20),
        )
        assert fb.correct is False
        assert fb.session_status == "completed"  # 回测轮只走一遍

    async def test_wrong_mark_without_retest(self, async_session: AsyncSession):
        """未开启回测：错题仅标记（retest_status=off），主序列走完即完成."""
        v = await _publish(async_session, "题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v],
            retest_wrong=False,
            now=_T0,
        )
        fb = await submit_answer(
            async_session, s.session_id,
            item_version_id=v, response={"selected": "A"}, now=_T0,
        )
        assert fb.session_status == "completed"
        state = await get_session_state(async_session, s.session_id, now=_T0)
        assert state.wrong_count == 1
        assert state.retest_pending == 0


# ════════════════════════════════════════════════════════════════════
# 时长保护
# ════════════════════════════════════════════════════════════════════

class TestTimeProtection:
    async def test_low_grade_15min_rest_prompt(self, async_session: AsyncSession):
        """L 段：连续作答超 15 分钟 → 取题返回休息提示；resume 后继续."""
        v = await _publish(async_session, "题一")
        v2 = await _publish(async_session, "题二")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="L",
            item_version_ids=[v, v2],
            now=_T0,
        )
        # 第 16 分钟取题 → 休息提示
        with pytest.raises(RestRequiredError) as exc_info:
            await get_next_item(
                async_session, s.session_id, now=_T0 + timedelta(minutes=16)
            )
        assert "休息" in exc_info.value.message
        assert exc_info.value.time_limit_sec == 900

        # 状态置 rest_prompted；提交同样被保护拦截
        state = await get_session_state(
            async_session, s.session_id, now=_T0 + timedelta(minutes=16)
        )
        assert state.status == "rest_prompted"
        with pytest.raises(RestRequiredError):
            await submit_answer(
                async_session,
                s.session_id,
                item_version_id=v,
                response={"selected": "B"},
                now=_T0 + timedelta(minutes=16),
            )

        # 休息确认（第 20 分钟）→ 重置计时锚点，继续作答
        state2 = await resume_session(
            async_session, s.session_id, now=_T0 + timedelta(minutes=20)
        )
        assert state2.status == "active"
        assert state2.elapsed_active_sec == 0
        nxt = await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(minutes=21)
        )
        assert nxt is not None and nxt.item_version_id == v

    async def test_mid_grade_60min_threshold(self, async_session: AsyncSession):
        """M 段：60 分钟内正常，第 61 分钟触发保护."""
        v = await _publish(async_session, "题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v],
            now=_T0,
        )
        nxt = await get_next_item(
            async_session, s.session_id, now=_T0 + timedelta(minutes=59)
        )
        assert nxt is not None
        with pytest.raises(RestRequiredError):
            await get_next_item(
                async_session, s.session_id, now=_T0 + timedelta(minutes=61)
            )

    async def test_submit_blocked_after_limit(self, async_session: AsyncSession):
        """学生停留超阈值后提交：被拦截（不评分不落账），resume 后可重新提交."""
        v = await _publish(async_session, "题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="L",
            item_version_ids=[v],
            now=_T0,
        )
        await get_next_item(async_session, s.session_id, now=_T0)
        with pytest.raises(RestRequiredError):
            await submit_answer(
                async_session,
                s.session_id,
                item_version_id=v,
                response={"selected": "B"},
                now=_T0 + timedelta(minutes=16),
            )
        # 未落账
        cnt = (
            await async_session.execute(
                text("SELECT count(*) FROM response_event WHERE session_id = :sid"),
                {"sid": s.session_id},
            )
        ).scalar()
        assert cnt == 0

        await resume_session(
            async_session, s.session_id, now=_T0 + timedelta(minutes=20)
        )
        fb = await submit_answer(
            async_session,
            s.session_id,
            item_version_id=v,
            response={"selected": "B"},
            now=_T0 + timedelta(minutes=21),
        )
        assert fb.correct is True

    async def test_state_reports_remaining(self, async_session: AsyncSession):
        """会话状态含已用时长与保护余量."""
        v = await _publish(async_session, "题")
        s = await start_session(
            async_session,
            student_alias_id=uuid4(),
            gradeband="M",
            item_version_ids=[v],
            now=_T0,
        )
        state = await get_session_state(
            async_session, s.session_id, now=_T0 + timedelta(minutes=10)
        )
        assert state.elapsed_active_sec == 600
        assert state.remaining_sec == 3600 - 600


# ════════════════════════════════════════════════════════════════════
# 放弃会话
# ════════════════════════════════════════════════════════════════════

async def test_abandon_session(async_session: AsyncSession):
    v = await _publish(async_session, "题")
    s = await start_session(
        async_session,
        student_alias_id=uuid4(),
        gradeband="M",
        item_version_ids=[v],
        now=_T0,
    )
    state = await abandon_session(async_session, s.session_id, now=_T0)
    assert state.status == "abandoned"
    from src.core.session.service import SessionStateError

    with pytest.raises(SessionStateError):
        await get_next_item(async_session, s.session_id, now=_T0)
