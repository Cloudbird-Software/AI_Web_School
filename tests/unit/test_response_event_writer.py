"""T-W1-005 response_event 写入服务单元测试.

验收标准 #5 覆盖：
(a) 写入一条事件后可 SELECT 回读
(b) scene 三值（practice/diagnosis/measurement）均可写入
(c) JSONB 字段（raw_payload/scoring_trace/error_inferences）可写入与回读结构一致
(d) UPDATE 或 DELETE 在 DB 层被拒绝（失败测试）

宪法 D1 三本账只增不改：append-only 由迁移 0003 的 BEFORE UPDATE OR DELETE
触发器物理强制。本测试文件验证应用层 record_event() 与 DB 层触发器协同正确。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.events.writer import record_event


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture：每测试前 TRUNCATE response_event
# ────────────────────────────────────────────────────────────────────
# 为什么用 TRUNCATE 而非 DELETE：DELETE 会被 append-only 触发器拒绝；TRUNCATE
# 是 DDL 类操作，不触发 BEFORE UPDATE/DELETE 触发器，可安全用于测试清理。
# 为什么 CASCADE：response_event 当前无被引用 FK，CASCADE 是面向未来的兜底。
@pytest_asyncio.fixture(autouse=True)
async def _truncate_response_event(async_session: AsyncSession):
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield
    # 测试后不清理——便于调试；下一个测试的 setup 会 TRUNCATE


# ────────────────────────────────────────────────────────────────────
# 辅助构造函数
# ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    """当前 UTC 时间（落入当前月分区，避免 INSERT 找不到分区）."""
    return datetime.now(timezone.utc)


def _make_scoring_trace(scorer_id: str = "exact_match") -> dict:
    """契约 §3 scoring_trace 结构（评分轨迹）."""
    return {
        "scorer_id": scorer_id,
        "scorer_version": "1.0.0+sha256:abc123",
        "process": {"note": "命中点判定"},
        "confidence": {
            "recognition": 0.0,
            "scoring": 1.0,
            "note": "识别与评分两层",
        },
    }


def _make_error_inferences(empty: bool = False) -> list[dict]:
    """契约 §4 error_inferences 结构（错误推断数组，可为空数组）."""
    if empty:
        return []
    return [
        {
            "error_type_id": "math.decimal.digits_more_is_larger",
            "confidence": 0.85,
            "rule_version": "1.2.0",
            "evidence": {"selected_option": "B"},
        }
    ]


# ────────────────────────────────────────────────────────────────────
# (a) 写入一条事件后可 SELECT 回读
# ────────────────────────────────────────────────────────────────────

async def test_insert_and_readback(async_session: AsyncSession):
    """验收 #5(a)：写入一条事件后可 SELECT 回读全部字段."""
    eid = uuid4()
    sid = uuid4()
    sess_id = uuid4()
    created_at = _now_utc()

    returned = await record_event(
        async_session,
        event_id=eid,
        student_alias_id=sid,
        item_version_id="sha256:item-v1",
        scene="practice",
        raw_payload={"selected_option": "A"},
        scoring_trace=_make_scoring_trace(),
        error_inferences=_make_error_inferences(),
        created_at=created_at,
        duration_ms=12345,
        testlet_id="testlet-001",
        session_id=sess_id,
        audio_play_events=[{"play_count": 2}],
        source_ref={"assembly_run_id": "run-abc"},
    )
    assert returned == eid

    # 回读全部字段
    result = await async_session.execute(
        text(
            """
            SELECT event_id, student_alias_id, item_version_id, scene,
                   raw_payload, duration_ms, scoring_trace, error_inferences,
                   testlet_id, session_id, audio_play_events, source_ref, created_at
            FROM response_event
            WHERE event_id = :eid
            """
        ),
        {"eid": eid},
    )
    row = result.one()
    assert row[0] == eid
    assert row[1] == sid
    assert row[2] == "sha256:item-v1"
    assert row[3] == "practice"
    assert row[4] == {"selected_option": "A"}
    assert row[5] == 12345
    assert row[6]["scorer_id"] == "exact_match"
    assert row[7][0]["error_type_id"] == "math.decimal.digits_more_is_larger"
    assert row[8] == "testlet-001"
    assert row[9] == sess_id
    assert row[10] == [{"play_count": 2}]
    assert row[11] == {"assembly_run_id": "run-abc"}


# ────────────────────────────────────────────────────────────────────
# (b) scene 三值均可写入
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("scene", ["practice", "diagnosis", "measurement"])
async def test_scene_three_values_writable(async_session: AsyncSession, scene: str):
    """验收 #5(b)：D5 scene 三值（practice/diagnosis/measurement）均可写入."""
    eid = uuid4()
    await record_event(
        async_session,
        event_id=eid,
        student_alias_id=uuid4(),
        item_version_id="sha256:item-v1",
        scene=scene,
        raw_payload={"answer": "x"},
        scoring_trace=_make_scoring_trace(),
        error_inferences=_make_error_inferences(),
        created_at=_now_utc(),
    )
    result = await async_session.execute(
        text("SELECT scene FROM response_event WHERE event_id = :eid"),
        {"eid": eid},
    )
    assert result.scalar_one() == scene


async def test_scene_invalid_value_rejected(async_session: AsyncSession):
    """D5：scene 仅三值，其他值在 DB 层被 enum 拒绝（负向校验）."""
    eid = uuid4()
    # 直接走裸 SQL 写入非法 scene 值，验证 enum 物理约束
    with pytest.raises(Exception):
        await async_session.execute(
            text(
                """
                INSERT INTO response_event (
                    event_id, student_alias_id, item_version_id, scene,
                    raw_payload, scoring_trace, error_inferences, created_at
                ) VALUES (
                    :eid, :sid, :iv, 'exam',
                    '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, :ts
                )
                """
            ),
            {
                "eid": eid,
                "sid": uuid4(),
                "iv": "sha256:item-v1",
                "ts": _now_utc(),
            },
        )
        await async_session.commit()
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# (c) JSONB 字段写入与回读结构一致
# ────────────────────────────────────────────────────────────────────

async def test_jsonb_roundtrip(async_session: AsyncSession):
    """验收 #5(c)：raw_payload/scoring_trace/error_inferences 写入与回读结构一致.

    覆盖：
    - 嵌套 dict/list 结构
    - 中文/Unicode（ensure_ascii=False 保证 UTF-8 字节直接落库）
    - 空数组 error_inferences=[]
    """
    eid = uuid4()
    raw_payload = {
        "blocks": [
            {"type": "text", "value": "题干：1+1=?"},
            {"type": "options", "value": ["A.1", "B.2", "C.3"]},
        ],
        "selected": ["B.2"],
    }
    scoring_trace = {
        "scorer_id": "keypoint_hit",
        "scorer_version": "2.1.0+sha256:def",
        "process": {"hit_steps": ["s1"], "missed": []},
        "confidence": {"recognition": 0.95, "scoring": 0.88},
    }
    error_inferences = _make_error_inferences(empty=True)

    await record_event(
        async_session,
        event_id=eid,
        student_alias_id=uuid4(),
        item_version_id="sha256:item-v2",
        scene="diagnosis",
        raw_payload=raw_payload,
        scoring_trace=scoring_trace,
        error_inferences=error_inferences,
        created_at=_now_utc(),
    )

    result = await async_session.execute(
        text(
            """
            SELECT raw_payload, scoring_trace, error_inferences
            FROM response_event WHERE event_id = :eid
            """
        ),
        {"eid": eid},
    )
    row = result.one()
    # raw_payload 嵌套结构与中文 Unicode 完整保留
    assert row[0] == raw_payload
    assert row[0]["blocks"][0]["value"] == "题干：1+1=?"
    # scoring_trace 嵌套 dict 保留
    assert row[1] == scoring_trace
    assert row[1]["process"]["hit_steps"] == ["s1"]
    # error_inferences 空数组保留
    assert row[2] == []


async def test_optional_fields_nullable(async_session: AsyncSession):
    """契约 v1.1：duration_ms/session_id/testlet_id/audio_play_events/source_ref 均可空.

    纸卷回录 S2 场景：无真实耗时、无会话、无音频——NULL=未知，禁止伪造。
    """
    eid = uuid4()
    await record_event(
        async_session,
        event_id=eid,
        student_alias_id=uuid4(),
        item_version_id="sha256:item-paper",
        scene="measurement",
        raw_payload={"filled": "B"},
        scoring_trace=_make_scoring_trace(),
        error_inferences=_make_error_inferences(),
        created_at=_now_utc(),
        # 全部 optional 字段保持 None
    )
    result = await async_session.execute(
        text(
            """
            SELECT duration_ms, testlet_id, session_id, audio_play_events, source_ref
            FROM response_event WHERE event_id = :eid
            """
        ),
        {"eid": eid},
    )
    row = result.one()
    assert row[0] is None  # duration_ms
    assert row[1] is None  # testlet_id
    assert row[2] is None  # session_id
    assert row[3] is None  # audio_play_events
    assert row[4] is None  # source_ref


# ────────────────────────────────────────────────────────────────────
# (d) UPDATE / DELETE 在 DB 层被拒绝
# ────────────────────────────────────────────────────────────────────

async def _seed_one_event(async_session: AsyncSession) -> None:
    """写入一条事件供 UPDATE/DELETE 测试使用."""
    await record_event(
        async_session,
        event_id=uuid4(),
        student_alias_id=uuid4(),
        item_version_id="sha256:item-v1",
        scene="practice",
        raw_payload={"selected_option": "A"},
        scoring_trace=_make_scoring_trace(),
        error_inferences=_make_error_inferences(),
        created_at=_now_utc(),
    )


async def test_update_rejected_by_trigger(async_session: AsyncSession):
    """验收 #5(d)：UPDATE 在 DB 层被 append-only 触发器拒绝（D1 物理强制）."""
    await _seed_one_event(async_session)

    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("UPDATE response_event SET duration_ms = 0")
        )
        await async_session.commit()
    await async_session.rollback()


async def test_delete_rejected_by_trigger(async_session: AsyncSession):
    """验收 #5(d)：DELETE 在 DB 层被 append-only 触发器拒绝（D1 物理强制）."""
    await _seed_one_event(async_session)

    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("DELETE FROM response_event")
        )
        await async_session.commit()
    await async_session.rollback()


async def test_trigger_is_statement_level(async_session: AsyncSession):
    """验收 #3：触发器为 BEFORE UPDATE OR DELETE FOR EACH STATEMENT（非 ROW）.

    通过 pg_catalog 检查触发器定义，确认 FOR EACH STATEMENT 而非 FOR EACH ROW。
    PG 输出顺序为 `BEFORE DELETE OR UPDATE`（DELETE 在前），故分别检查两个动作。
    """
    result = await async_session.execute(
        text(
            """
            SELECT pg_get_triggerdef(oid)
            FROM pg_trigger
            WHERE tgname = 'trg_response_event_append_only'
            """
        )
    )
    trigger_def = result.scalar_one()
    assert "BEFORE" in trigger_def
    assert "DELETE" in trigger_def
    assert "UPDATE" in trigger_def
    assert "FOR EACH STATEMENT" in trigger_def


# ────────────────────────────────────────────────────────────────────
# DDL 契约对照：分区 + PK + 字段
# ────────────────────────────────────────────────────────────────────

async def test_partitioned_by_created_at(async_session: AsyncSession):
    """验收 #2：按月分区（以 created_at 为分区键）.

    partstrat 为 char(1)：'r' = RANGE。asyncpg 返回 bytes，需解码或 bytes 比较。
    """
    result = await async_session.execute(
        text(
            """
            SELECT partstrat
            FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = 'response_event'
            """
        )
    )
    strat = result.scalar_one()
    # 兼容 bytes（asyncpg）与 str（psycopg）两种返回
    strat_str = strat.decode() if isinstance(strat, (bytes, bytearray)) else strat
    assert strat_str == "r", f"应为 RANGE 分区（'r'），实际 {strat_str!r}"


async def test_primary_key_includes_created_at(async_session: AsyncSession):
    """验收 #2：PK 含分区键 (event_id, created_at)（契约 §2 实现注记）."""
    result = await async_session.execute(
        text(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'response_event'::regclass
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """
        )
    )
    pk_cols = [row[0] for row in result.fetchall()]
    assert pk_cols == ["event_id", "created_at"], (
        f"PK 应为 (event_id, created_at)，实际 {pk_cols}"
    )


async def test_initial_partitions_exist(async_session: AsyncSession):
    """验收 #2：至少创建初始分区（当前月 + 接下来 3 个月 = 至少 4 个分区）."""
    result = await async_session.execute(
        text(
            """
            SELECT count(*)
            FROM pg_inherits
            WHERE inhparent = 'response_event'::regclass
            """
        )
    )
    assert result.scalar_one() >= 4, "至少应创建当前月 + 接下来 3 个月共 4 个分区"


async def test_scene_enum_values(async_session: AsyncSession):
    """§1 scene 枚举含三值：practice/diagnosis/measurement（D5）."""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'response_event_scene_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert values == ["practice", "diagnosis", "measurement"]


async def test_all_contract_fields_present(async_session: AsyncSession):
    """验收 #1：契约 §1 全要素字段（13 列）均存在."""
    result = await async_session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'response_event'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    expected = {
        "event_id", "student_alias_id", "item_version_id", "scene",
        "raw_payload", "duration_ms", "scoring_trace", "error_inferences",
        "testlet_id", "session_id", "audio_play_events", "source_ref",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"response_event 缺字段: {missing}"


async def test_no_pii_fields(async_session: AsyncSession):
    """D7：事件表只允许 student_alias_id，禁止直接标识字段."""
    result = await async_session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'response_event'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    for forbidden in ("student_name", "real_name", "phone", "id_card", "email"):
        assert forbidden not in cols, f"response_event 出现疑似 PII 字段 {forbidden}"
