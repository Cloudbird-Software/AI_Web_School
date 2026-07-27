"""S4→S8 数据飞轮联通测试：score_and_record 落账事件必须对 CTT 可见.

背景：CTT 标定的正确性信号取数位置是
scoring_trace->'dimension_scores'->>'correct'（src/core/data/ctt.py）。
W3 出口集成时发现 score_and_record 装配的 scoring_trace 缺 dimension_scores，
在线作答事件会被 CTT 取数 SQL 静默过滤（correct 为 NULL 不参与估计），
「作答 → 参数标定」飞轮断链。本测试钉住该联通契约，防回归。
"""
from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.data.ctt import CTT_SOURCE, run_ctt_calibration
from src.core.scoring.registry import ScoreResult
from src.core.scoring.service import build_scoring_trace, score_and_record


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.execute(text("TRUNCATE TABLE item_param CASCADE"))
    await async_session.commit()
    yield


def _choice_iv(item_version_id: str) -> dict:
    """单选题版本快照：正解 B，干扰项 A 绑错误类型."""
    return {
        "item_version_id": item_version_id,
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ],
    }


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
    """插入最小 item + item_version（满足 item_param FK；与 test_ctt 同手法）."""
    item_id = f"item-for-{item_version_id[-8:]}"
    await db.execute(
        text("INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"),
        {"iid": item_id},
    )
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage)"
            " VALUES (:vid, :iid, 'draft', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
        ),
        {"vid": item_version_id, "iid": item_id},
    )
    await db.commit()


def test_build_scoring_trace_carries_dimension_scores() -> None:
    """scoring_trace 必须含 dimension_scores（CTT 取数键，契约 §3 可扩展对象）."""
    result = ScoreResult(
        dimension_scores={"correct": 1.0},
        error_inferences=[],
        confidence={"scoring": 1.0},
        evidence={"note": "判定明细"},
        scorer_version="1.0.0+platform",
    )
    trace = build_scoring_trace("exact_match", result)
    assert trace["dimension_scores"] == {"correct": 1.0}
    # 既有结构不回退
    assert trace["scorer_id"] == "exact_match"
    assert trace["confidence"]["scoring"] == 1.0
    assert "process" in trace


async def test_score_and_record_events_feed_ctt(async_session: AsyncSession) -> None:
    """在线评分落账的事件经 run_ctt_calibration 产出 measured_ctt 参数行."""
    iv_id = "sha256:ctt-link-iv-001"
    iv = _choice_iv(iv_id)
    await _insert_item_version(async_session, iv_id)
    # 两个学生：一对一错 → difficulty = 0.5
    await score_and_record(
        async_session,
        item_version=iv,
        response={"selected": "B"},
        student_alias_id=uuid4(),
        scene="practice",
        pack_id="subject-math",
    )
    await score_and_record(
        async_session,
        item_version=iv,
        response={"selected": "A"},
        student_alias_id=uuid4(),
        scene="practice",
        pack_id="subject-math",
    )

    written = await run_ctt_calibration(async_session, purpose_scope="practice")

    assert len(written) == 1
    row = written[0]
    assert row.item_version_id == iv_id
    assert row.source == CTT_SOURCE == "measured_ctt"
    assert row.purpose_scope == "practice"
    assert row.sample_size == 2
    assert row.params["difficulty"] == pytest.approx(0.5)


async def test_ctt_scopes_isolated(async_session: AsyncSession) -> None:
    """D5 分场景隔离：practice 事件不进入 diagnosis 估计."""
    iv_id = "sha256:ctt-link-iv-002"
    iv = _choice_iv(iv_id)
    await _insert_item_version(async_session, iv_id)
    await score_and_record(
        async_session,
        item_version=iv,
        response={"selected": "B"},
        student_alias_id=uuid4(),
        scene="practice",
        pack_id="subject-math",
    )

    d_rows = await run_ctt_calibration(async_session, purpose_scope="diagnosis")
    p_rows = await run_ctt_calibration(async_session, purpose_scope="practice")

    assert d_rows == []
    assert len(p_rows) == 1
