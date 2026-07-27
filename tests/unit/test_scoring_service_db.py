"""W3-S4 score_and_record 落 response_event 单元测试（DB 回读验证）.

覆盖：
- 事件字段完整：契约 §1 全要素（event_id/student_alias_id/item_version_id/
  scene/raw_payload/duration_ms/scoring_trace/error_inferences/session_id/
  source_ref/created_at）。
- scoring_trace 契约 §3 结构 + 四层置信度之评分层。
- error_inferences 契约 §4 结构（评分器自报 + 选择题选项映射合并）。
- math_equivalence 学科桶接入（scorer_version 含 subject-math）。
- 未注册评分器 → ScorerNotRegisteredError。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.scoring.service import (
    ScorerNotRegisteredError,
    score_and_record,
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


def _choice_iv() -> dict:
    """单选题：正解 B，干扰项 A 绑错误类型（模仿 test_api_readonly 夹具形态）."""
    return {
        "item_version_id": "sha256:sc-iv-001",
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ],
    }


async def _readback(db: AsyncSession, event_id) -> dict:
    row = (
        await db.execute(
            text(
                """
                SELECT event_id, student_alias_id, item_version_id, scene,
                       raw_payload, duration_ms, scoring_trace, error_inferences,
                       testlet_id, session_id, source_ref, created_at
                FROM response_event WHERE event_id = :eid
                """
            ),
            {"eid": event_id},
        )
    ).one()
    return dict(row._mapping)


# ════════════════════════════════════════════════════════════════════
# 落账字段完整
# ════════════════════════════════════════════════════════════════════

async def test_record_wrong_choice_full_fields(async_session: AsyncSession):
    """选错干扰项：评分 + 选项映射推断，事件全要素落账可回读."""
    student = uuid4()
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    outcome = await score_and_record(
        async_session,
        item_version=_choice_iv(),
        response={"selected": "A"},
        student_alias_id=student,
        scene="practice",
        duration_ms=5320,
        session_id=session_id,
        source_ref={"paper_id": "paper-1", "placement_token": "q3"},
        now=now,
    )

    assert outcome.correct is False
    assert outcome.dimension_scores["correct"] == 0.0
    assert len(outcome.error_inferences) == 1

    row = await _readback(async_session, outcome.event_id)
    assert row["student_alias_id"] == student
    assert row["item_version_id"] == "sha256:sc-iv-001"
    assert row["scene"] == "practice"
    assert row["raw_payload"] == {"selected": "A"}
    assert row["duration_ms"] == 5320
    assert row["session_id"] == session_id
    assert row["source_ref"] == {"paper_id": "paper-1", "placement_token": "q3"}

    # scoring_trace 契约 §3
    trace = row["scoring_trace"]
    assert trace["scorer_id"] == "exact_match"
    assert trace["scorer_version"] == "1.0.0+platform"
    assert trace["confidence"]["scoring"] == 1.0
    assert "note" in trace["confidence"]  # 四层分离说明
    assert "process" in trace

    # error_inferences 契约 §4（选项映射：rule_version=item_version_id）
    infs = row["error_inferences"]
    assert len(infs) == 1
    assert infs[0]["error_type_id"] == "math.decimal.digits_more_is_larger"
    assert infs[0]["rule_version"] == "sha256:sc-iv-001"
    assert infs[0]["evidence"]["selected_option"] == "A"


async def test_record_correct_answer_empty_inferences(async_session: AsyncSession):
    """答对：error_inferences 为空数组（契约允许），dimension_scores.correct=1."""
    outcome = await score_and_record(
        async_session,
        item_version=_choice_iv(),
        response={"selected": "B"},
        student_alias_id=uuid4(),
        scene="diagnosis",
        now=datetime.now(timezone.utc),
    )
    assert outcome.correct is True
    row = await _readback(async_session, outcome.event_id)
    assert row["scene"] == "diagnosis"
    assert row["error_inferences"] == []
    # duration_ms 未提供 → NULL=未知（禁止填 0 冒充）
    assert row["duration_ms"] is None
    # 无会话场景 → session_id NULL
    assert row["session_id"] is None


async def test_math_equivalence_via_subject_bucket(async_session: AsyncSession):
    """math_equivalence 经 subject-math 桶接入作答链路."""
    iv = {
        "item_version_id": "sha256:me-iv-001",
        "interaction_ref": {"interaction_id": "numeric_blank", "interaction_params": {}},
        "scoring_ref": {
            "scorer_id": "math_equivalence",
            "scorer_params": {"answer_expr": "1/2"},
        },
        "error_bindings": [],
    }
    outcome = await score_and_record(
        async_session,
        item_version=iv,
        response={"blanks": {"b1": {"value": "0.5"}}},
        student_alias_id=uuid4(),
        scene="practice",
        pack_id="subject-math",
        now=datetime.now(timezone.utc),
    )
    assert outcome.correct is True
    row = await _readback(async_session, outcome.event_id)
    assert row["scoring_trace"]["scorer_id"] == "math_equivalence"
    assert "subject-math" in row["scoring_trace"]["scorer_version"]


async def test_math_equivalence_wrong_unit_inference(async_session: AsyncSession):
    """math_equivalence 自报错误推断（off_by_one）落 error_inferences."""
    iv = {
        "item_version_id": "sha256:me-iv-002",
        "interaction_ref": {"interaction_id": "numeric_blank", "interaction_params": {}},
        "scoring_ref": {
            "scorer_id": "math_equivalence",
            "scorer_params": {"answer_expr": "8"},
        },
        "error_bindings": [],
    }
    outcome = await score_and_record(
        async_session,
        item_version=iv,
        response={"blanks": {"b1": {"value": "9"}}},
        student_alias_id=uuid4(),
        scene="practice",
        pack_id="subject-math",
        now=datetime.now(timezone.utc),
    )
    assert outcome.correct is False
    row = await _readback(async_session, outcome.event_id)
    infs = row["error_inferences"]
    assert len(infs) == 1
    assert infs[0]["error_type_id"] == "off_by_one"
    assert infs[0]["rule_version"]  # 非空


async def test_unregistered_scorer_raises(async_session: AsyncSession):
    """未注册评分器 → ScorerNotRegisteredError（不落账）."""
    iv = {
        "item_version_id": "sha256:bad-iv",
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "ai_rubric", "scorer_params": {}},
        "error_bindings": [],
    }
    with pytest.raises(ScorerNotRegisteredError):
        await score_and_record(
            async_session,
            item_version=iv,
            response={"selected": "A"},
            student_alias_id=uuid4(),
            scene="practice",
            now=datetime.now(timezone.utc),
        )
