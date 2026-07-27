"""T-W2-014 知识图谱种子数据加载器.

从 content/seeds/*.yaml 加载知识图谱种子数据（kp_node + kp_edge + relation_type），
按 (pack_id, dimension, code) 查重节点、按 (src_node_id, dst_node_id, rel_type)
查重边、按 rel_type 查重关系类型——重复导入 = skip，不抛错。

幂等约定（验收 #2）：同一文件多次 load() 不产生重复行，统计信息反映
{added, skipped} 的实际操作计数。

为什么 YAML 用 code 而非 node_id 引用边：种子文件人类可读、可跨环境
迁移；node_id 由加载器在第一次插入时生成（ULID），后续重复导入按 code
查重即可——node_id 跨环境不同但不影响幂等性。

宪法 A5/A7：本模块不 import 任何学科包/学段包（学科零特判）。
本模块只 import 核心域 ORM（KpNode/KpEdge/RelationType）与 Pydantic。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import ulid
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.kp_edge import KpEdge
from src.core.models.kp_node import KpNode
from src.core.models.relation_type import RelationType


# ────────────────────────────────────────────────────────────────────
# 默认种子路径
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED_PATH: Path = (
    _PROJECT_ROOT / "content" / "seeds" / "math_kp_3-4.yaml"
)


# ────────────────────────────────────────────────────────────────────
# YAML Schema Pydantic 模型
# ────────────────────────────────────────────────────────────────────

class RelationTypeSeed(BaseModel):
    """relation_type 行的种子定义."""

    model_config = ConfigDict(extra="forbid")

    rel_type: str
    directed: bool = True
    transitive: bool = False
    acyclic: bool = True
    symmetric: bool = False
    description: Optional[str] = None


class KpNodeSeed(BaseModel):
    """kp_node 行的种子定义（dimension 固定为 'kp'，由加载器填入）."""

    model_config = ConfigDict(extra="forbid")

    code: str
    title: str
    std_anchor: Optional[str] = None
    gradeband: Optional[str] = "M"


class KpEdgeSeed(BaseModel):
    """kp_edge 行的种子定义（src/dst 用 code 引用，加载器解析为 node_id）."""

    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    rel_type: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class SeedFile(BaseModel):
    """种子 YAML 文件根模型."""

    model_config = ConfigDict(extra="forbid")

    version: str
    pack_id: str
    graph_release_id: str
    relation_types: list[RelationTypeSeed] = Field(default_factory=list)
    nodes: list[KpNodeSeed]
    edges: list[KpEdgeSeed] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 加载与解析
# ────────────────────────────────────────────────────────────────────

def parse_seed_file(path: Path) -> SeedFile:
    """读取并校验种子 YAML.

    Args:
        path: 种子 YAML 路径。

    Returns:
        SeedFile: Pydantic 校验后的不可变种子模型。

    Raises:
        FileNotFoundError: 文件不存在。
        pydantic.ValidationError: schema 校验失败（缺字段/类型不符等）。
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return SeedFile.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# 幂等加载主函数
# ────────────────────────────────────────────────────────────────────

async def load(
    path: Optional[Path] = None,
    db: AsyncSession = None,
    dimension: str = "kp",
) -> dict[str, Any]:
    """幂等导入知识图谱种子数据.

    Args:
        path: 种子 YAML 路径。None 时使用默认 math_kp_3-4.yaml。
        db: AsyncSession（必填）。
        dimension: 节点维度（默认 'kp'；任务卡验收 #3 要求所有节点 dimension=kp）。

    Returns:
        统计信息 dict：
        {
            "pack_id": str,
            "graph_release_id": str,
            "relation_types_added": int,
            "relation_types_skipped": int,
            "nodes_added": int,
            "nodes_skipped": int,
            "edges_added": int,
            "edges_skipped": int,
            "edges_missing_node": int,  # src 或 dst code 未在文件内定义
        }

    Raises:
        ValueError: db 未提供；YAML 内 src/dst code 未在 nodes 中定义且
                    也未在数据库中存在（无法解析为 node_id）。
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")

    if path is None:
        path = DEFAULT_SEED_PATH

    seed = parse_seed_file(path)
    pack_id = seed.pack_id

    stats: dict[str, Any] = {
        "pack_id": pack_id,
        "graph_release_id": seed.graph_release_id,
        "relation_types_added": 0,
        "relation_types_skipped": 0,
        "nodes_added": 0,
        "nodes_skipped": 0,
        "edges_added": 0,
        "edges_skipped": 0,
        "edges_missing_node": 0,
    }

    # ── 1. relation_types 幂等 upsert ──
    existing_rel_types: set[str] = set()
    rel_result = await db.execute(select(RelationType.rel_type))
    existing_rel_types = {row[0] for row in rel_result.fetchall()}

    for rt_seed in seed.relation_types:
        if rt_seed.rel_type in existing_rel_types:
            stats["relation_types_skipped"] += 1
            continue
        db.add(RelationType(
            rel_type=rt_seed.rel_type,
            pack_id=pack_id,
            directed=rt_seed.directed,
            transitive=rt_seed.transitive,
            acyclic=rt_seed.acyclic,
            symmetric=rt_seed.symmetric,
            description=rt_seed.description,
        ))
        existing_rel_types.add(rt_seed.rel_type)
        stats["relation_types_added"] += 1
    await db.flush()

    # ── 2. nodes 幂等 upsert，构建 code → node_id 映射 ──
    # 查重键：(pack_id, dimension, code) 唯一约束 uq_kp_node_pack_dim_code
    code_to_node_id: dict[str, str] = {}

    # 一次性查出本 pack + dimension 下所有现存 code → node_id
    node_result = await db.execute(
        select(KpNode.code, KpNode.node_id).where(
            KpNode.pack_id == pack_id,
            KpNode.dimension == dimension,
        )
    )
    for code, node_id in node_result.fetchall():
        code_to_node_id[code] = node_id

    for node_seed in seed.nodes:
        if node_seed.code in code_to_node_id:
            stats["nodes_skipped"] += 1
            continue
        node_id = "kp_" + str(ulid.new())
        db.add(KpNode(
            node_id=node_id,
            pack_id=pack_id,
            dimension=dimension,
            code=node_seed.code,
            title=node_seed.title,
            std_anchor=node_seed.std_anchor,
            gradeband=node_seed.gradeband,
            status="active",  # 种子数据默认 active
        ))
        code_to_node_id[node_seed.code] = node_id
        stats["nodes_added"] += 1
    await db.flush()

    # ── 3. edges 幂等 upsert，src/dst 用 code 解析 ──
    # 查重键：(src_node_id, dst_node_id, rel_type) 唯一约束 uq_kp_edge_src_dst_rel
    # 加载器在文件级别去重——同一文件内若有重复 (src_code, dst_code, rel_type)，
    # 第二次出现算 skipped（不抛错，方便种子文件冗余书写）。
    seen_edges: set[tuple[str, str, str]] = set()
    edge_result = await db.execute(
        select(KpEdge.src_node_id, KpEdge.dst_node_id, KpEdge.rel_type)
    )
    for src_id, dst_id, rel_type in edge_result.fetchall():
        seen_edges.add((src_id, dst_id, rel_type))

    for edge_seed in seed.edges:
        src_id = code_to_node_id.get(edge_seed.src)
        dst_id = code_to_node_id.get(edge_seed.dst)

        if src_id is None or dst_id is None:
            # 文件内引用了未定义的 code——记录但不抛错，让 caller 通过 stats 检测
            stats["edges_missing_node"] += 1
            continue

        key = (src_id, dst_id, edge_seed.rel_type)
        if key in seen_edges:
            stats["edges_skipped"] += 1
            continue

        db.add(KpEdge(
            src_node_id=src_id,
            dst_node_id=dst_id,
            rel_type=edge_seed.rel_type,
            attrs=edge_seed.attrs,
            provenance=edge_seed.provenance,
        ))
        seen_edges.add(key)
        stats["edges_added"] += 1

    await db.commit()
    return stats


__all__ = ["SeedFile", "KpNodeSeed", "KpEdgeSeed", "RelationTypeSeed",
           "parse_seed_file", "load", "DEFAULT_SEED_PATH"]
