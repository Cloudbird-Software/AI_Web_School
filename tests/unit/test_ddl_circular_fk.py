"""T-W1-002 循环外键可操作性 + 触发器前移测试.

对照 T-W01-T02 验证卡 §8：循环外键 DEFERRABLE 是否真的允许互引插入；
§6.3：item_version INSERT 且 status='published' 时触发器自动前移
item.current_version_id。

为什么必须实际插入：仅查 pg_constraint.condeferrable 不够——可能存在
约束被错误地设为 DEFERRABLE 但 INITIALLY IMMEDIATE 的情形，单事务内的
互引插入依然会失败。物理插入是最强证据。

为什么用 bindparam(type_=JSONB)：asyncpg 不会自动把 Python dict 序列化为
JSONB；显式声明类型让 SQLAlchemy 在 bind 阶段调用 JSONB 的 bind_processor
做 json.dumps，再由 asyncpg 以 str 形式送入 PG，PG 自动 text→jsonb 转换。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB


def _six_blocks() -> dict:
    """构造一份最小可用的六大块 JSONB（满足 NOT NULL 约束即可）。"""
    return {
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.test"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": "L",
            "graph_release": "2026.1",
        },
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "content": {"blocks": []},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {}},
        "error_bindings": {},
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "test", "version": "1.0.0"},
            "signed_by": "tester",
            "signed_at": "2026-07-26T00:00:00Z",
        },
    }


# JSONB bind 参数列表（item_version INSERT 共用）
_JSONB_BINDS = [
    bindparam("obj", type_=JSONB),
    bindparam("ir", type_=JSONB),
    bindparam("ct", type_=JSONB),
    bindparam("sr", type_=JSONB),
    bindparam("eb", type_=JSONB),
    bindparam("ln", type_=JSONB),
]


def _item_version_insert_sql(with_rendered: bool = False, with_publish: bool = False) -> str:
    """构造 item_version INSERT SQL，按需包含 rendered_snapshot / published 字段。"""
    cols = [
        "item_version_id", "item_id", "status",
        "objective", "interaction_ref", "content", "scoring_ref",
        "error_bindings", "lineage",
    ]
    placeholders = [":vid", ":iid", ":status", ":obj", ":ir", ":ct", ":sr", ":eb", ":ln"]
    if with_rendered:
        cols.append("rendered_snapshot")
        placeholders.append(":rs")
    if with_publish:
        cols.append("gate_certificate_id")
        cols.append("published_at")
        placeholders.append(":gcid")
        placeholders.append(":pts")
    cols.append("created_at")
    placeholders.append(":ts")
    col_list = ", ".join(cols)
    val_list = ", ".join(placeholders)
    return f"INSERT INTO item_version ({col_list}) VALUES ({val_list})"


async def _insert_item(async_session, item_id: str, ts: datetime) -> None:
    """插一行 item（current_version_id 留空）。"""
    await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier, created_at) "
            "VALUES (:id, :pack, 'C', :ts)"
        ),
        {"id": item_id, "pack": "subject-test", "ts": ts},
    )


async def test_can_insert_item_and_version_together(async_session):
    """§6.1 循环外键可操作性：插 item + draft item_version 不报错。

    draft 状态不触发 current_version_id 前移触发器；本用例仅验证
    item_version.item_id → item.item_id 这一向外键可用。
    """
    now = datetime.now(timezone.utc)
    item_id = "test-cfk-item-1"

    await _insert_item(async_session, item_id, now)

    blocks = _six_blocks()
    sql = text(_item_version_insert_sql()).bindparams(*_JSONB_BINDS)
    await async_session.execute(
        sql,
        {
            "vid": "test-cfk-iv-1", "iid": item_id, "status": "draft",
            "obj": blocks["objective"], "ir": blocks["interaction_ref"],
            "ct": blocks["content"], "sr": blocks["scoring_ref"],
            "eb": blocks["error_bindings"], "ln": blocks["lineage"],
            "ts": now,
        },
    )
    await async_session.commit()


async def test_publish_trigger_advances_current_version_id(async_session):
    """§6.3 触发器前移：插 published item_version 后 item.current_version_id 自动更新。

    为什么允许 published_at+gate_certificate_id 同时非空：§4 规则 1 要求
    published_at 非空必伴随 gate_certificate_id 非空；本测试同时填两个字段
    以满足 CHECK 约束 ck_iv_published_requires_gate_cert。
    """
    now = datetime.now(timezone.utc)
    item_id = "test-trg-item-1"
    item_version_id = "test-trg-iv-1"

    await _insert_item(async_session, item_id, now)

    blocks = _six_blocks()
    binds = _JSONB_BINDS + [bindparam("rs", type_=JSONB)]
    sql = text(_item_version_insert_sql(with_rendered=True, with_publish=True)).bindparams(*binds)
    await async_session.execute(
        sql,
        {
            "vid": item_version_id, "iid": item_id, "status": "published",
            "obj": blocks["objective"], "ir": blocks["interaction_ref"],
            "ct": blocks["content"], "sr": blocks["scoring_ref"],
            "eb": blocks["error_bindings"], "ln": blocks["lineage"],
            "rs": {"rendered": "test"},
            "gcid": "test-gate-cert-1", "pts": now, "ts": now,
        },
    )
    await async_session.commit()

    # 验证触发器已前移 item.current_version_id
    result = await async_session.execute(
        text("SELECT current_version_id FROM item WHERE item_id = :id"),
        {"id": item_id},
    )
    row = result.fetchone()
    assert row is not None, "item 行不存在"
    assert row[0] == item_version_id, (
        f"触发器未前移 current_version_id：期望 {item_version_id}，实际 {row[0]}"
    )


async def test_published_at_without_gate_cert_rejected(async_session):
    """§4 规则 1 / §6.4：published_at 非空但 gate_certificate_id 为空必须被 DB 拒绝。"""
    now = datetime.now(timezone.utc)
    item_id = "test-ck-item-1"
    item_version_id = "test-ck-iv-1"

    await _insert_item(async_session, item_id, now)

    blocks = _six_blocks()
    binds = _JSONB_BINDS + [bindparam("rs", type_=JSONB)]
    # 用 status='published' + rendered_snapshot + published_at 但缺 gate_certificate_id
    sql_text = _item_version_insert_sql(with_rendered=True, with_publish=False)
    # 手动追加 published_at 字段（不走 with_publish 路径，故意不填 gate_certificate_id）
    sql_text = sql_text.replace(
        ", created_at",
        ", published_at, created_at",
    ).replace(":ts)", ":pts, :ts)")
    sql = text(sql_text).bindparams(*binds)

    with pytest.raises(Exception) as exc_info:
        await async_session.execute(
            sql,
            {
                "vid": item_version_id, "iid": item_id, "status": "published",
                "obj": blocks["objective"], "ir": blocks["interaction_ref"],
                "ct": blocks["content"], "sr": blocks["scoring_ref"],
                "eb": blocks["error_bindings"], "ln": blocks["lineage"],
                "rs": {"rendered": "test"},
                "pts": now, "ts": now,
            },
        )
        await async_session.commit()
    await async_session.rollback()
    # PG 错误信息应包含约束名或 CheckViolation 标志
    err_msg = str(exc_info.value).lower()
    assert "ck_iv_published_requires_gate_cert" in err_msg or \
           "check" in err_msg and "violation" in err_msg, (
        f"应被 CHECK 约束拒绝，实际异常：{exc_info.value}"
    )


async def test_quarantine_requires_rendered_snapshot(async_session):
    """§2.2：进入 quarantined 前必填 rendered_snapshot（CHECK 兜底）。"""
    now = datetime.now(timezone.utc)
    item_id = "test-q-item-1"
    item_version_id = "test-q-iv-1"

    await _insert_item(async_session, item_id, now)

    blocks = _six_blocks()
    # quarantined 状态但不填 rendered_snapshot —— 应被 CHECK 拒绝
    sql = text(_item_version_insert_sql(with_rendered=False)).bindparams(*_JSONB_BINDS)
    with pytest.raises(Exception) as exc_info:
        await async_session.execute(
            sql,
            {
                "vid": item_version_id, "iid": item_id, "status": "quarantined",
                "obj": blocks["objective"], "ir": blocks["interaction_ref"],
                "ct": blocks["content"], "sr": blocks["scoring_ref"],
                "eb": blocks["error_bindings"], "ln": blocks["lineage"],
                "ts": now,
            },
        )
        await async_session.commit()
    await async_session.rollback()
    err_msg = str(exc_info.value).lower()
    assert "ck_iv_quarantine_requires_rendered" in err_msg or \
           "check" in err_msg and "violation" in err_msg, (
        f"应被 CHECK 约束拒绝，实际异常：{exc_info.value}"
    )


async def test_item_group_max_six_items(async_session):
    """§2.5 R-Z-06：题组 ≤6 题（DB 层 CHECK）。"""
    now = datetime.now(timezone.utc)
    # 7 个 item_version_id 应被拒绝
    seven_ids = [f"iv-{i}" for i in range(7)]
    with pytest.raises(Exception) as exc_info:
        await async_session.execute(
            text(
                """
                INSERT INTO item_group (item_group_id, item_version_ids, created_at)
                VALUES (:gid, :ids, :ts)
                """
            ),
            {"gid": "ig-1", "ids": seven_ids, "ts": now},
        )
        await async_session.commit()
    await async_session.rollback()
    err_msg = str(exc_info.value).lower()
    assert "ck_ig_max_six_items" in err_msg or \
           "check" in err_msg and "violation" in err_msg, (
        f"应被 CHECK 约束拒绝，实际异常：{exc_info.value}"
    )

    # 6 个应通过
    six_ids = [f"iv-{i}" for i in range(6)]
    await async_session.execute(
        text(
            """
            INSERT INTO item_group (item_group_id, item_version_ids, created_at)
            VALUES (:gid, :ids, :ts)
            """
        ),
        {"gid": "ig-2", "ids": six_ids, "ts": now},
    )
    await async_session.commit()
