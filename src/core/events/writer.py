"""T-W1-005 作答事件 append-only 写入服务.

按 specs/contracts/events/response_event.md v1.0.0 落地 record_event()。
record_event() 仅执行 INSERT——D1 三本账只增不改，UPDATE/DELETE 由 DB 触发器
（迁移 0003 raise_append_only_error）物理强制拒绝。

契约承载：
- §1 全要素字段：身份（event_id/student_alias_id/item_version_id）+ 场景（scene）
  + 载荷（raw_payload）+ 耗时（duration_ms，可空 NULL=未知）+ 评分轨迹
  （scoring_trace）+ 错误推断（error_inferences）+ 题组/会话/音频/来源追溯。
- §2.1 append-only：本函数仅 INSERT；任何 UPDATE/DELETE 在 DB 层失败。
- §2.4 场景不可为空、不可混估：scene 必填，由 enum 物理约束三值。
- D5 参数分场景：scene 字段是下游分场景估计器的取数键，写入时即定型。

为什么用裸 SQL 而非 ORM：response_event 是分区表，ORM 映射分区表的语义边界
（特别是 PK 含分区键）易踩坑；本表只写不读不更不删，裸 SQL INSERT 最直接，
参数与契约 §1 字段表逐字对照，便于审阅。

为什么 JSONB 字段用 CAST(:x AS jsonb) 而非 :x::jsonb：asyncpg 驱动把 :jsonb
解释为另一个 bind parameter（PG ::type 语法与 SQLAlchemy :name 参数语法冲突），
导致 syntax error；CAST(...) 是 SQL 标准语法，无歧义。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# §1 scene 枚举三值（D5 分场景独立统计禁止混估）
Scene = Literal["practice", "diagnosis", "measurement"]


_INSERT_SQL = """
INSERT INTO response_event (
    event_id, student_alias_id, item_version_id, scene,
    raw_payload, duration_ms, scoring_trace, error_inferences,
    testlet_id, session_id, audio_play_events, source_ref, created_at
) VALUES (
    :event_id, :student_alias_id, :item_version_id, :scene,
    CAST(:raw_payload AS jsonb), :duration_ms,
    CAST(:scoring_trace AS jsonb), CAST(:error_inferences AS jsonb),
    :testlet_id, :session_id,
    CAST(:audio_play_events AS jsonb), CAST(:source_ref AS jsonb),
    :created_at
)
"""


async def record_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    student_alias_id: UUID,
    item_version_id: str,
    scene: Scene,
    raw_payload: dict[str, Any],
    scoring_trace: dict[str, Any],
    error_inferences: list[dict[str, Any]],
    created_at: datetime,
    duration_ms: Optional[int] = None,
    testlet_id: Optional[str] = None,
    session_id: Optional[UUID] = None,
    audio_play_events: Optional[list[dict[str, Any]]] = None,
    source_ref: Optional[dict[str, Any]] = None,
    auto_commit: bool = False,
) -> UUID:
    """Append-only 写入一条作答事件，返回 event_id.

    Args:
        session: 异步 SQLAlchemy 会话。
        event_id: 事件唯一 id（应用层 ULID 生成——ulid.new() 的 128 位值可无损
            转 UUID 入本列；全局唯一性由应用层保证，
            契约 §2 实现注记——分区表 PK 为 (event_id, created_at)）。
        student_alias_id: 匿名学生 id（D7 PII 只在保险库 schema，本表只存 alias）。
        item_version_id: 作答题目版本（A/B 级实例=内容寻址哈希，D3）。
        scene: 场景三值之一（practice/diagnosis/measurement，D5 禁止混估）。
        raw_payload: 原始作答载荷（作答内容本身，非仅存对错，R-D-01）。
        scoring_trace: 评分轨迹，结构见契约 §3。
        error_inferences: 错误类型推断数组，结构见契约 §4（可为空数组）。
        created_at: 事件时间戳（UTC），分区键。
        duration_ms: 作答耗时（毫秒）；NULL=未知（纸卷回录 S2 无真实耗时，
            禁止填 0 冒充——耗时是健康度监控维度）。契约 v1.1 可空。
        testlet_id: 题组/testlet id（题组内相关性统计用，R-Z-06）。
        session_id: 作答会话 id；NULL=无会话（纸卷录入场景；S2 批次标识放
            source_ref.batch_id，勿伪造会话）。契约 v1.1 可空。
        audio_play_events: 音频播放行为（音频题必填）。
        source_ref: 来源追溯 {paper_id, placement_token} 或 {assembly_run_id}（A4 入水口）。
        auto_commit: 是否在写入后立即 commit；默认 False，由上层事务边界统一 commit。

    Returns:
        event_id（与入参一致，便于调用方链式引用）。

    Notes:
        - 内部仅执行 INSERT；UPDATE/DELETE 由 DB 触发器物理强制拒绝（D1）。
        - 调用方负责保证 event_id 全局唯一；重复 event_id + 相同 created_at 会
          因 PK 冲突失败（这是预期行为，应用层应保证幂等键）。
        - 默认仅 flush 不 commit，避免上层调用（如 submit_answer）导致双 commit。
    """
    await session.execute(
        text(_INSERT_SQL),
        {
            "event_id": event_id,
            "student_alias_id": student_alias_id,
            "item_version_id": item_version_id,
            "scene": scene,
            # JSONB 字段：以 json 字符串 + CAST(... AS jsonb) 显式类型转换，避免
            # asyncpg 参数推断歧义（裸 SQL 无列类型元信息，必须显式 cast）。
            "raw_payload": json.dumps(raw_payload, ensure_ascii=False),
            "duration_ms": duration_ms,
            "scoring_trace": json.dumps(scoring_trace, ensure_ascii=False),
            "error_inferences": json.dumps(error_inferences, ensure_ascii=False),
            "testlet_id": testlet_id,
            "session_id": session_id,
            "audio_play_events": (
                json.dumps(audio_play_events, ensure_ascii=False)
                if audio_play_events is not None
                else None
            ),
            "source_ref": (
                json.dumps(source_ref, ensure_ascii=False)
                if source_ref is not None
                else None
            ),
            "created_at": created_at,
        },
    )
    await session.flush()
    if auto_commit:
        await session.commit()
    return event_id
