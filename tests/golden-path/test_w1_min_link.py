"""W1 最小链路烟测（T-W01-T04 第 12 步 / T-W1-008 出口）.

端到端四步：
① 创建 GateCertificate（gate 三表，主键 cert_id）
② publish_item_version 发布一个 status='published' 的 item_version（持门证书，
   验证 §6.3 触发器将 item.current_version_id 前移）
③ record_event 写一条 response_event（append-only 事件账）
④ 对该事件 UPDATE 必须被 DB 触发器拒绝（D1 三本账只增不改）

与验证卡参考代码的差异（按实际 API 适配）：
- GateCertificate 主键是 cert_id（非 id）；cert_type 受 DB CHECK 约束
  ck_gc_cert_type_domain 限制，仅 'publish'/'retire'，故用 'publish'。
- record_event 签名为 (session, *, event_id, ...)，session 位置参数在前。
- content 内嵌入 ULID 保证每次运行内容寻址 id 唯一（D3：同 content 必产生
  同 item_version_id，重复运行同一 content 会 PK 冲突），本测试因此可重复执行。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import ulid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.content.writer import publish_item_version
from src.core.events.writer import record_event
from src.core.gate.models import GateCertificate
from src.core.models.item import Item


async def test_w1_min_content_publish_and_event(async_session: AsyncSession):
    """W1 端到端最小组合链路：门证书 → 发布 → 事件 → append-only 拒绝 UPDATE."""
    run_uid = str(ulid.new())

    # ── Step 1: 创建门证书（append-only，主键 cert_id）──
    cert_id = str(ulid.new())
    cert = GateCertificate(
        cert_id=cert_id,
        artifact_ref="w1-min-link",
        cert_type="publish",
        policy_version="1.0",
        issued_by="w1-exit",
    )
    async_session.add(cert)
    await async_session.commit()

    # ── Step 2: 发布一个 published 的 item_version（持门证书）──
    version_data = {
        "pack_id": "platform",
        "tier": "C",
        "status": "published",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "smoke.test"}],
            "kp_set_mode": "single",
            "cognitive_level": "remember",
            "gradeband": "L",
            "graph_release": "2026.1",
        },
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        # content 嵌入 run_uid：内容寻址（D3）下保证可重复运行不撞 PK
        "content": {"blocks": [{"type": "text", "value": f"W1出口烟测题 {run_uid}"}]},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"correct_answer": "A"}},
        "error_bindings": [],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "w1-exit", "version": "1.0"},
            "signed_by": "smoke",
            "signed_at": "2026-07-26T00:00:00Z",
        },
    }
    pub_result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        gate_certificate_id=cert_id,
        db=async_session,
    )
    assert pub_result["item_id"] is not None
    assert pub_result["item_version_id"] is not None

    # 验证 §6.3 触发器已前移 current_version_id（refresh 兜底：
    # 触发器在 DB 侧 UPDATE，ORM 身份映射中的对象可能是旧值）
    item = await async_session.get(Item, pub_result["item_id"])
    await async_session.refresh(item)
    assert item.current_version_id == pub_result["item_version_id"]

    # ── Step 3: 写入一条作答事件（append-only 事件账）──
    event_id = uuid.uuid4()
    eid = await record_event(
        async_session,
        event_id=event_id,
        student_alias_id=uuid.uuid4(),
        item_version_id=pub_result["item_version_id"],
        scene="practice",
        raw_payload={"answer": "A"},
        scoring_trace={
            "scorer_id": "exact_match",
            "scorer_version": "1.0",
            "confidence": {"scoring": 1.0},
        },
        error_inferences=[],
        created_at=datetime.now(timezone.utc),
    )
    assert eid == event_id

    # ── Step 4: append-only——对该事件 UPDATE 必须被触发器拒绝（D1）──
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("UPDATE response_event SET raw_payload = '{}' WHERE event_id = :eid"),
            {"eid": event_id},
        )
        await async_session.commit()
    await async_session.rollback()
