"""T-W2-014 数学 3-4 年级图谱种子数据单元测试.

对照 T-W2-014 任务卡验收标准：
1. content/seeds/math_kp_3-4.yaml 包含 ≥80 个 kp_node 与 ≥50 条 kp_edge。
2. seed_loader.load() 可幂等导入并返回统计信息。
3. 所有节点 dimension=kp，code 符合 math.{domain}.{topic}.{subtopic} 命名。
4. 单元测试验证无孤立先修节点、无重复 code。

测试隔离：autouse fixture 在每测试前 TRUNCATE 知识图谱五表 CASCADE。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.knowledge.seed_loader import (
    DEFAULT_SEED_PATH,
    SeedFile,
    load,
    parse_seed_file,
)
from src.core.models.kp_edge import KpEdge
from src.core.models.kp_node import KpNode
from src.core.models.relation_type import RelationType


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture
# ────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _truncate_kp_tables(async_session: AsyncSession):
    """每测试前 TRUNCATE 知识图谱五表（含闭包/版本表）CASCADE."""
    await async_session.execute(
        text(
            "TRUNCATE TABLE "
            "kp_closure, kp_edge, kp_node, relation_type, graph_release "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


# code 命名规范：math.{domain}.{topic}.{subtopic}
_CODE_PATTERN = re.compile(r"^math\.[a-z_]+\.[a-z_]+\.[a-z_]+$")


# ────────────────────────────────────────────────────────────────────
# §1 文件结构验收（无需 DB）
# ────────────────────────────────────────────────────────────────────

def test_seed_file_exists_at_default_path():
    """验收 #1：默认路径存在 math_kp_3-4.yaml."""
    assert DEFAULT_SEED_PATH.is_file(), f"种子文件不存在: {DEFAULT_SEED_PATH}"


def test_seed_file_parses_with_pydantic():
    """验收 #1：YAML 通过 Pydantic schema 校验."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    assert isinstance(seed, SeedFile)
    assert seed.pack_id == "subject-math"
    assert seed.version
    assert seed.graph_release_id


def test_seed_has_at_least_80_nodes():
    """验收 #1：节点数 ≥80."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    assert len(seed.nodes) >= 80, f"节点数 {len(seed.nodes)} < 80"


def test_seed_has_at_least_50_edges():
    """验收 #1：边数 ≥50."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    assert len(seed.edges) >= 50, f"边数 {len(seed.edges)} < 50"


def test_all_node_codes_match_naming_convention():
    """验收 #3：所有 code 符合 math.{domain}.{topic}.{subtopic}."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    violations = [
        n.code for n in seed.nodes if not _CODE_PATTERN.match(n.code)
    ]
    assert not violations, f"code 命名违规: {violations[:5]}"


def test_no_duplicate_node_codes_in_file():
    """验收 #4：种子文件内无重复 code."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    codes = [n.code for n in seed.nodes]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"文件内重复 code: {duplicates}"


def test_no_orphan_prerequisite_edges_in_file():
    """验收 #4：每条 prerequisite 边的 src 与 dst code 必须在 nodes 中定义."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    defined = {n.code for n in seed.nodes}
    orphans = [
        e for e in seed.edges
        if e.rel_type == "prerequisite"
        and (e.src not in defined or e.dst not in defined)
    ]
    assert not orphans, f"prerequisite 边引用未定义节点: {orphans[:3]}"


def test_all_edge_rel_types_declared_in_relation_types():
    """边引用的 rel_type 必须在 relation_types 段定义或为种子已声明的."""
    seed = parse_seed_file(DEFAULT_SEED_PATH)
    declared = {rt.rel_type for rt in seed.relation_types}
    used = {e.rel_type for e in seed.edges}
    undeclared = used - declared
    # 允许种子文件不重复声明已存在的 rel_type（如 prerequisite 已由他处入库）
    # 但首次种子应包含全部声明——这里要求 declared ⊇ used
    assert not undeclared, f"边用了未在 relation_types 段声明的类型: {undeclared}"


# ────────────────────────────────────────────────────────────────────
# §2 加载器幂等导入验收（需 DB）
# ────────────────────────────────────────────────────────────────────

async def test_load_returns_stats_and_inserts_all(async_session: AsyncSession):
    """验收 #2：首次 load 返回统计且全部新增."""
    stats = await load(db=async_session)

    assert stats["pack_id"] == "subject-math"
    assert stats["nodes_added"] >= 80, f"nodes_added={stats['nodes_added']}"
    assert stats["edges_added"] >= 50, f"edges_added={stats['edges_added']}"
    assert stats["relation_types_added"] >= 3  # prerequisite/confusable/composes

    # 首次导入无 skip（除文件内重复外）
    assert stats["nodes_skipped"] == 0
    assert stats["edges_missing_node"] == 0


async def test_load_is_idempotent(async_session: AsyncSession):
    """验收 #2：二次 load 全部 skip，不产生重复行."""
    s1 = await load(db=async_session)
    s2 = await load(db=async_session)

    # 二次：节点全 skip
    assert s2["nodes_added"] == 0
    assert s2["nodes_skipped"] == s1["nodes_added"]
    assert s2["edges_added"] == 0
    assert s2["edges_skipped"] == s1["edges_added"]
    assert s2["relation_types_added"] == 0
    assert s2["relation_types_skipped"] == s1["relation_types_added"]

    # DB 行数不变
    node_count = (await async_session.scalar(
        select(func.count()).select_from(KpNode)
    ))
    edge_count = (await async_session.scalar(
        select(func.count()).select_from(KpEdge)
    ))
    rel_count = (await async_session.scalar(
        select(func.count()).select_from(RelationType)
    ))
    assert node_count == s1["nodes_added"]
    assert edge_count == s1["edges_added"]
    assert rel_count == s1["relation_types_added"]


async def test_all_inserted_nodes_have_dimension_kp(async_session: AsyncSession):
    """验收 #3：DB 中所有种子节点 dimension=kp."""
    await load(db=async_session)
    result = await async_session.execute(
        select(KpNode.dimension, KpNode.code).where(KpNode.pack_id == "subject-math")
    )
    rows = result.fetchall()
    assert rows, "应有种子节点"
    for dimension, code in rows:
        assert dimension == "kp", f"节点 {code} dimension={dimension} 非 'kp'"
        assert _CODE_PATTERN.match(code), f"节点 code 命名违规: {code}"


async def test_no_duplicate_codes_in_db(async_session: AsyncSession):
    """验收 #4：DB 中按 (pack_id, dimension, code) 无重复（唯一约束兜底）."""
    await load(db=async_session)
    result = await async_session.execute(
        text(
            """
            SELECT code, COUNT(*) AS cnt
            FROM kp_node
            WHERE pack_id = 'subject-math' AND dimension = 'kp'
            GROUP BY code
            HAVING COUNT(*) > 1
            """
        )
    )
    dups = result.fetchall()
    assert not dups, f"DB 中重复 code: {dups[:5]}"


async def test_no_orphan_prerequisite_edges_in_db(async_session: AsyncSession):
    """验收 #4：DB 中每条 prerequisite 边的 src/dst 都存在于 kp_node."""
    await load(db=async_session)
    result = await async_session.execute(
        text(
            """
            SELECT e.src_node_id, e.dst_node_id
            FROM kp_edge e
            JOIN kp_node n ON n.node_id = e.src_node_id
            WHERE e.rel_type = 'prerequisite'
              AND NOT EXISTS (
                SELECT 1 FROM kp_node n2 WHERE n2.node_id = e.dst_node_id
              )
            """
        )
    )
    orphans = result.fetchall()
    assert not orphans, f"prerequisite 边 dst 不存在于 kp_node: {orphans[:3]}"


async def test_load_with_custom_path(async_session: AsyncSession, tmp_path: Path):
    """load(path=...) 支持自定义种子文件."""
    custom = tmp_path / "custom_seed.yaml"
    custom.write_text(
        """
version: "1.0"
pack_id: subject-test
graph_release_id: "2026.test"
relation_types:
  - rel_type: prerequisite_test
    directed: true
    transitive: true
    acyclic: true
    symmetric: false
nodes:
  - {code: math.test.topic.node_a, title: A}
  - {code: math.test.topic.node_b, title: B}
edges:
  - {src: math.test.topic.node_a, dst: math.test.topic.node_b, rel_type: prerequisite_test}
""",
        encoding="utf-8",
    )
    stats = await load(path=custom, db=async_session)
    assert stats["pack_id"] == "subject-test"
    assert stats["nodes_added"] == 2
    assert stats["edges_added"] == 1


async def test_load_raises_on_missing_db():
    """db=None 抛 ValueError."""
    with pytest.raises(ValueError, match="db"):
        await load(db=None)
