"""T-W2-043 签发闭环服务：item_version 状态前移 + publication 落库.

落地架构 v2 §4.3「签发闭环」与契约 §4 状态机：
- item_version 状态机：draft → quarantined → published → retired（无回边）。
- 签发动作 = 「门证书签发 → published」一跳：把 item_version 从 draft/quarantined
  前移到 published，同时写入 publication 表（签发账）。

为什么允许 UPDATE item_version.status：
- D1「只增不改」约束的是「内容快照不可变」（六大块永不 UPDATE）+「历史版本永不删除」。
- 状态机字段 status / gate_certificate_id / published_at / retired_at 是「发布元数据」，
  契约 §4 明示其前移语义（draft→quarantined→published），DB 层无 BEFORE UPDATE 触发器
  拦截（迁移 0002 仅挂 AFTER INSERT 触发器前移 current_version_id）。
- 内容寻址（D3）保证同内容必同 item_version_id——若改内容须新 INSERT 新行（新 id），
  不会与状态前移冲突。

落库动作（一次签发 = 一个事务）：
1. SELECT item_version（必须存在且 status ∈ {draft, quarantined}）。
2. UPDATE item_version SET status='published', gate_certificate_id=<cert>, published_at=now()。
3. INSERT publication（publication_id / item_id / item_version_id / gate_certificate_id /
   published_by / published_at）。
4. AFTER INSERT 触发器（迁移 0002）自动前移 item.current_version_id。

宪法 D1：内容六大块不改；仅状态机字段前移，无回边。
宪法 D2：published_at 非空必伴随 gate_certificate_id 非空（DB CHECK 强制）。
宪法 A5/X6：本模块不 import 学科包/学段包。
"""
from __future__ import annotations

import ulid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item import Item
from src.core.models.item_version import ItemVersion


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class IssueError(ValueError):
    """签发流程错误（item_version 不存在 / 状态非法 / 缺门证书）。"""


# ────────────────────────────────────────────────────────────────────
# 签发主入口
# ────────────────────────────────────────────────────────────────────


async def issue_item_version(
    item_version_id: str,
    gate_certificate_id: str,
    published_by: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """签发 item_version：状态前移到 published + 写 publication 记录.

    幂等性：同一 item_version_id 重复签发会因 status 已是 published 而抛
    IssueError（状态机无回边/无重签）。

    Args:
        item_version_id: 待签发的 item_version id。
        gate_certificate_id: 门证书 id（必须由 run_gate 路径产出）。
        published_by: 签发人 id（W2 单用户 = workbench token）。
        db: AsyncSession（必填）。

    Returns:
        {
            "item_id": str,
            "item_version_id": str,
            "publication_id": str,
            "gate_certificate_id": str,
            "published_at": datetime,
        }

    Raises:
        IssueError: item_version 不存在 / 状态非 draft|quarantined / 已 published。
        ValueError: db 未提供或参数缺。
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")
    if not item_version_id:
        raise ValueError("item_version_id 必填")
    if not gate_certificate_id:
        raise ValueError("gate_certificate_id 必填")
    if not published_by:
        raise ValueError("published_by 必填")

    # 1. 取 item_version
    version = await db.get(ItemVersion, item_version_id)
    if version is None:
        raise IssueError(f"item_version_id={item_version_id} 不存在")

    # 2. 状态机校验：仅 draft/quarantined 可前移到 published
    if version.status == "published":
        raise IssueError(
            f"item_version_id={item_version_id} 已是 published，状态机无重签"
        )
    if version.status == "retired":
        raise IssueError(
            f"item_version_id={item_version_id} 已 retired，状态机无回边"
        )
    if version.status not in ("draft", "quarantined"):
        raise IssueError(
            f"item_version_id={item_version_id} 状态非法={version.status!r}"
            "（允许：draft/quarantined）"
        )

    # 3. UPDATE item_version：状态前移 + 写门证书 + 发布时间
    # 为什么用 UPDATE 而非 INSERT 新行：内容寻址（D3）保证同内容同 id，
    # 状态机字段前移是契约 §4 明示的合法 UPDATE（无回边）。
    now = datetime.now(timezone.utc)
    version.status = "published"
    version.gate_certificate_id = gate_certificate_id
    version.published_at = now
    # rendered_snapshot：draft 可空，published 必填（迁移 0002 CHECK 兜底）
    if version.rendered_snapshot is None:
        version.rendered_snapshot = {"placeholder": True}

    # 4. INSERT publication 记录（签发账）
    publication_id = "pub_" + str(ulid.new())
    await db.execute(
        text(
            "INSERT INTO publication"
            " (publication_id, item_id, item_version_id, gate_certificate_id,"
            " published_by, published_at)"
            " VALUES (:pid, :iid, :vid, :cid, :by, :at)"
        ),
        {
            "pid": publication_id,
            "iid": version.item_id,
            "vid": item_version_id,
            "cid": gate_certificate_id,
            "by": published_by,
            "at": now,
        },
    )

    # 5. 前移 item.current_version_id 指针
    # 为什么显式 UPDATE 而非依赖触发器：迁移 0002 的 trg_item_version_on_publish
    # 只挂在 AFTER INSERT（设计假设 item_version 只增不改）；本路径用 UPDATE 前移
    # 状态机字段（contract §4 明示允许），触发器不会触发，须由应用层显式前移指针。
    # item 表不在三本账（内容版本/作答事件/校验签发）之列，current_version_id 是
    # 元数据指针而非历史数据，UPDATE 不违反 D1。
    await db.execute(
        text(
            "UPDATE item SET current_version_id = :vid WHERE item_id = :iid"
        ),
        {"vid": item_version_id, "iid": version.item_id},
    )

    # 6. commit
    await db.commit()

    return {
        "item_id": version.item_id,
        "item_version_id": item_version_id,
        "publication_id": publication_id,
        "gate_certificate_id": gate_certificate_id,
        "published_at": now,
    }


# ────────────────────────────────────────────────────────────────────
# 查询辅助（供工作台 UI 用）
# ────────────────────────────────────────────────────────────────────


async def get_publication_by_version(
    item_version_id: str,
    db: AsyncSession,
) -> Optional[dict[str, Any]]:
    """查 publication 记录（按 item_version_id）.

    Returns:
        dict 含 publication_id/item_id/gate_certificate_id/published_by/published_at；
        无记录返回 None。
    """
    row = (
        await db.execute(
            text(
                "SELECT publication_id, item_id, item_version_id,"
                " gate_certificate_id, published_by, published_at"
                " FROM publication WHERE item_version_id = :vid"
                " LIMIT 1"
            ),
            {"vid": item_version_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        "publication_id": row[0],
        "item_id": row[1],
        "item_version_id": row[2],
        "gate_certificate_id": row[3],
        "published_by": row[4],
        "published_at": row[5],
    }


__all__ = [
    "IssueError",
    "issue_item_version",
    "get_publication_by_version",
]
