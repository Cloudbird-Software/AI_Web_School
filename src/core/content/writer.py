"""内容版本写入服务（T-W1-007）.

入库唯一路径：创建 item（如新建）、写入 item_version、记录 lineage、
更新 current_version_id（由 §6.3 触发器自动前移）。

门强制规则（契约 §4 规则 1 / §6.4）：
- status='published' 必须提供合法 gate_certificate_id，否则抛 ValueError。
- status='draft' 或 'quarantined' 不要求门证书。

宪法 D1：item_version 只增不改——多次发布产生多个 version，旧版永不删除。
宪法 A5/A7：本模块不 import 任何学科包/学段包。

为什么 item_version_id 用内容寻址：D3 可复现要求同一内容产生同一 id；
C/D 级用 §3 公式二 compute_canonical_item_version_id。
A/B 级（template_version_id 非空）用 §3 公式一 compute_instance_id，
但需要 template_version_digest / pack_digest / engine_digest / corpus_digests
等参数，这些由调用方在 version_data 中提供（本函数仅做拼装）。
为兼容骨架测试，A/B 级缺参数时退化为 UUID（标注 TODO 待生产线接入后补全）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import ulid
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.content_addressing import (
    compute_canonical_item_version_id,
    compute_material_version_id,
)
from src.core.models.item import Item
from src.core.models.item_version import ItemVersion
from src.core.models.material import Material
from src.core.models.material_version import MaterialVersion
from src.core.models.corpus_asset import CorpusAsset
from src.core.models.corpus_version import CorpusVersion


# ────────────────────────────────────────────────────────────────────
# 门强制异常
# ────────────────────────────────────────────────────────────────────

class GateEnforcementError(ValueError):
    """门强制失败：published 状态未提供合法 gate_certificate_id。"""


# ────────────────────────────────────────────────────────────────────
# 辅助函数
# ────────────────────────────────────────────────────────────────────

def _new_id(prefix: str = "") -> str:
    """生成 ULID（ulid-py）.

    契约规定 C/D 级 item_id/material_id 用 ULID（W1 修复包 P7 落地）：
    ULID 128 位、词典序可排序、26 字符 Crockford base32 文本形式，
    与 ORM text 列兼容。
    """
    return prefix + str(ulid.new())


def _compute_item_version_id(version_data: dict) -> str:
    """根据 version_data 计算 item_version_id.

    C/D 级（无 template_version_id）：用 §3 公式二内容寻址。
    A/B 级（有 template_version_id）：需要公式一的全部参数；
    version_data 中应提供 template_version_digest / pack_digest /
    engine_digest / corpus_digests / locale，否则退化为 UUID（TODO）。
    """
    locale = version_data.get("locale", "zh-CN")
    tier = version_data.get("lineage", {}).get("tier", "C")

    if tier in ("A", "B") and version_data.get("template_version_id"):
        # A/B 级：公式一需要完整参数
        from src.core.models.content_addressing import compute_instance_id
        try:
            return compute_instance_id(
                template_version_digest=version_data["template_version_digest"],
                normalized_params=version_data.get("normalized_params", {}),
                pack_digest=version_data["pack_digest"],
                engine_digest=version_data["engine_digest"],
                corpus_digests=version_data.get("corpus_digests", []),
                locale=locale,
            )
        except KeyError:
            # 参数不全，退化为 UUID（TODO：生产线接入后补全）
            return "sha256:" + uuid.uuid4().hex

    # C/D 级：公式二内容寻址
    return compute_canonical_item_version_id(
        objective=version_data["objective"],
        interaction_ref=version_data["interaction_ref"],
        content=version_data["content"],
        scoring_ref=version_data["scoring_ref"],
        error_bindings=version_data.get("error_bindings", {}),
        locale=locale,
    )


# ────────────────────────────────────────────────────────────────────
# publish_item_version
# ────────────────────────────────────────────────────────────────────

async def publish_item_version(
    item_id: Optional[str],
    version_data: dict,
    gate_certificate_id: Optional[str] = None,
    db: AsyncSession = None,
) -> dict:
    """入库唯一路径：创建 item（如新建）+ 写入 item_version + 门强制校验.

    Args:
        item_id: 已有 item 的 id；None 表示新建 item（从 version_data 取 pack_id/tier）。
        version_data: 含 pack_id / tier / status / 六大块 / 可选 locale。
        gate_certificate_id: 门证书 id；status='published' 时必填。
        db: AsyncSession（必填）。

    Returns:
        {"item_id": ..., "item_version_id": ...}

    Raises:
        GateEnforcementError: status='published' 但 gate_certificate_id 为 None。
        ValueError: 缺少必填字段或 db 未提供。
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")

    status = version_data.get("status", "draft")

    # ── 门强制：published 必须有 gate_certificate_id ──
    if status == "published" and not gate_certificate_id:
        raise GateEnforcementError(
            "门强制失败：status='published' 必须提供合法 gate_certificate_id"
            "（契约 §4 规则 1 / §6.4）"
        )

    # ── 创建或复用 item ──
    if item_id is None:
        item_id = _new_id()
        item = Item(
            item_id=item_id,
            pack_id=version_data["pack_id"],
            tier=version_data["tier"],
            template_version_id=version_data.get("template_version_id"),
        )
        db.add(item)
        await db.flush()  # 让 item 先落库，满足 item_version.item_id FK
    else:
        # 复用已有 item——验证存在
        existing = await db.get(Item, item_id)
        if existing is None:
            raise ValueError(f"item_id={item_id} 不存在")

    # ── 计算 item_version_id ──
    item_version_id = _compute_item_version_id(version_data)

    # ── 构造 item_version 行 ──
    now = datetime.now(timezone.utc)
    version_kwargs: dict[str, Any] = {
        "item_version_id": item_version_id,
        "item_id": item_id,
        "status": status,
        "objective": version_data["objective"],
        "interaction_ref": version_data["interaction_ref"],
        "content": version_data["content"],
        "scoring_ref": version_data["scoring_ref"],
        "error_bindings": version_data.get("error_bindings", {}),
        "lineage": version_data["lineage"],
    }

    # published 状态：写入 gate_certificate_id + published_at
    # draft/quarantined：gate_certificate_id 留空
    if status == "published":
        version_kwargs["gate_certificate_id"] = gate_certificate_id
        version_kwargs["published_at"] = now

    # 非 draft 状态必须 rendered_snapshot（迁移 0002 CHECK 约束
    # ck_iv_quarantine_requires_rendered: status='draft' OR rendered_snapshot IS NOT NULL）
    # 契约 §2.2：进入 quarantined 前必填；published 由 quarantined 升级而来，必然已有快照。
    # 调用方未提供时自动补占位（仅 W1 骨架；生产线由渲染器写入真实快照）。
    if status != "draft":
        version_kwargs["rendered_snapshot"] = version_data.get(
            "rendered_snapshot", {"placeholder": True}
        )

    version = ItemVersion(**version_kwargs)
    db.add(version)
    await db.commit()

    return {"item_id": item_id, "item_version_id": item_version_id}


# ────────────────────────────────────────────────────────────────────
# publish_material（骨架，两段式：material + material_version）
# ────────────────────────────────────────────────────────────────────

async def publish_material(
    material_data: dict,
    gate_certificate_id: Optional[str] = None,
    db: AsyncSession = None,
) -> dict:
    """素材入库：material（身份）+ material_version（版本）两段式.

    Args:
        material_data: 含 kind / pack_id / content_ref / license_id /
            status / lineage。
        gate_certificate_id: 门证书（status='published' 时必填）。
        db: AsyncSession。

    Returns:
        {"material_id": ..., "material_version_id": ...}
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")

    status = material_data.get("status", "draft")
    if status == "published" and not gate_certificate_id:
        raise GateEnforcementError(
            "门强制失败：material status='published' 必须提供 gate_certificate_id"
        )

    # ── 创建 material 身份 ──
    material_id = _new_id()
    material = Material(
        material_id=material_id,
        kind=material_data["kind"],
        pack_id=material_data.get("pack_id"),
    )
    db.add(material)
    await db.flush()

    # ── 创建 material_version ──
    material_version_id = compute_material_version_id(
        material_data["content_ref"]
    )
    now = datetime.now(timezone.utc)
    mv_kwargs: dict[str, Any] = {
        "material_version_id": material_version_id,
        "material_id": material_id,
        "content_ref": material_data["content_ref"],
        "license_id": material_data["license_id"],
        "status": status,
        "lineage": material_data["lineage"],
    }
    if status == "published":
        mv_kwargs["gate_certificate_id"] = gate_certificate_id
        mv_kwargs["published_at"] = now

    mv = MaterialVersion(**mv_kwargs)
    db.add(mv)
    await db.commit()

    return {"material_id": material_id, "material_version_id": material_version_id}


# ────────────────────────────────────────────────────────────────────
# publish_corpus_asset（骨架，两段式：corpus_asset + corpus_version）
# ────────────────────────────────────────────────────────────────────

async def publish_corpus_asset(
    corpus_data: dict,
    gate_certificate_id: Optional[str] = None,
    db: AsyncSession = None,
) -> dict:
    """语料库入库：corpus_asset（身份）+ corpus_version（版本）两段式.

    Args:
        corpus_data: 含 kind / pack_id / content_ref / license_id /
            status / lineage。
        gate_certificate_id: 门证书（status='published' 时必填）。
        db: AsyncSession。

    Returns:
        {"asset_id": ..., "version_id": ...}
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")

    status = corpus_data.get("status", "draft")
    if status == "published" and not gate_certificate_id:
        raise GateEnforcementError(
            "门强制失败：corpus status='published' 必须提供 gate_certificate_id"
        )

    # ── 创建 corpus_asset 身份 ──
    asset_id = _new_id()
    asset = CorpusAsset(
        asset_id=asset_id,
        kind=corpus_data["kind"],
        pack_id=corpus_data.get("pack_id"),
    )
    db.add(asset)
    await db.flush()

    # ── 创建 corpus_version ──
    version_id = compute_material_version_id(corpus_data["content_ref"])
    cv = CorpusVersion(
        version_id=version_id,
        asset_id=asset_id,
        content_ref=corpus_data["content_ref"],
        license_id=corpus_data["license_id"],
        lineage=corpus_data["lineage"],
        status=status,
    )
    db.add(cv)
    await db.commit()

    return {"asset_id": asset_id, "version_id": version_id}
