"""T-W2-012 知识图谱底座三表（kp_node / kp_edge / relation_type）单元测试.

对照 T-W2-012 任务卡验收标准：
1. 迁移 0006 创建 kp_node/kp_edge/relation_type 三表，字段与契约一致。
2. kp_node 含 pack_id/dimension/code/title/std_anchor/gradeband/status/valid_from/valid_to/supersedes_id。
3. relation_type 含 directed/transitive/acyclic/symmetric 布尔元数据。
4. make migrate-check 全绿（由 alembic 命令直接验证，本文件覆盖 1-3）。

测试隔离：autouse fixture 在每测试前 TRUNCATE 三表 CASCADE。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.kp_edge import KpEdge
from src.core.models.kp_node import KpNode
from src.core.models.relation_type import RelationType


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture
# ────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _truncate_kp_tables(async_session: AsyncSession):
    """每测试前 TRUNCATE 三表 CASCADE，避免跨测试数据累积。"""
    await async_session.execute(
        text(
            "TRUNCATE TABLE kp_edge, kp_node, relation_type "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


def _uid(prefix: str = "node") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ────────────────────────────────────────────────────────────────────
# §1 三表存在与字段对齐
# ────────────────────────────────────────────────────────────────────

async def test_three_kp_tables_exist(async_session: AsyncSession):
    """验收 #1：kp_node / kp_edge / relation_type 三表存在。"""
    result = await async_session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    existing = {row[0] for row in result.fetchall()}
    missing = {"kp_node", "kp_edge", "relation_type"} - existing
    assert not missing, f"缺失知识图谱表: {missing}"


async def test_kp_node_columns_match_contract(async_session: AsyncSession):
    """验收 #2：kp_node 含契约规定的全部字段。"""
    result = await async_session.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'kp_node'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    required = {
        "node_id", "pack_id", "dimension", "code", "title",
        "std_anchor", "gradeband", "status",
        "valid_from", "valid_to", "supersedes_id", "created_at",
    }
    missing = required - cols
    assert not missing, f"kp_node 缺字段: {missing}"


async def test_kp_edge_columns_match_contract(async_session: AsyncSession):
    """kp_edge 含契约规定的全部字段。"""
    result = await async_session.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'kp_edge'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    required = {
        "edge_id", "src_node_id", "dst_node_id", "rel_type",
        "attrs", "provenance", "valid_from", "valid_to", "created_at",
    }
    missing = required - cols
    assert not missing, f"kp_edge 缺字段: {missing}"


async def test_relation_type_has_boolean_metadata(async_session: AsyncSession):
    """验收 #3：relation_type 含 directed/transitive/acyclic/symmetric 布尔元数据。"""
    result = await async_session.execute(
        text(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'relation_type'
            """
        )
    )
    col_map = {row[0]: row[1] for row in result.fetchall()}
    for bool_col in ("directed", "transitive", "acyclic", "symmetric"):
        assert bool_col in col_map, f"relation_type 缺布尔元数据列: {bool_col}"
        assert col_map[bool_col] == "boolean", (
            f"relation_type.{bool_col} 应为 boolean，实际 {col_map[bool_col]}"
        )


# ────────────────────────────────────────────────────────────────────
# §2 约束验证
# ────────────────────────────────────────────────────────────────────

async def test_kp_node_code_unique_per_pack_dimension(async_session: AsyncSession):
    """kp_node (pack_id, dimension, code) 唯一约束生效。"""
    code = "math.nal.decimal.compare"
    n1 = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code=code, title="小数大小比较",
    )
    async_session.add(n1)
    await async_session.flush()

    n2 = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code=code, title="重复编码应失败",
    )
    async_session.add(n2)
    with pytest.raises(Exception, match="uq_kp_node_pack_dim_code|unique"):
        await async_session.flush()
    await async_session.rollback()


async def test_kp_edge_no_self_loop(async_session: AsyncSession):
    """kp_edge 自环禁止约束（ck_kp_edge_no_self_loop）生效。"""
    n = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code="math.test.selfloop", title="自环测试",
    )
    async_session.add(n)
    await async_session.flush()

    rt = RelationType(rel_type="prerequisite_test_self", description="测试用")
    async_session.add(rt)
    await async_session.flush()

    e = KpEdge(src_node_id=n.node_id, dst_node_id=n.node_id, rel_type=rt.rel_type)
    async_session.add(e)
    with pytest.raises(Exception, match="ck_kp_edge_no_self_loop|self_loop|violates"):
        await async_session.flush()
    await async_session.rollback()


async def test_kp_edge_unique_src_dst_rel(async_session: AsyncSession):
    """kp_edge (src, dst, rel_type) 唯一约束生效。"""
    n1 = KpNode(node_id=_uid(), pack_id="p", dimension="kp", code="c1", title="t1")
    n2 = KpNode(node_id=_uid(), pack_id="p", dimension="kp", code="c2", title="t2")
    async_session.add_all([n1, n2])
    await async_session.flush()

    rt = RelationType(rel_type="prerequisite_test_dup", description="测试用")
    async_session.add(rt)
    await async_session.flush()

    e1 = KpEdge(src_node_id=n1.node_id, dst_node_id=n2.node_id, rel_type=rt.rel_type)
    async_session.add(e1)
    await async_session.flush()

    e2 = KpEdge(src_node_id=n1.node_id, dst_node_id=n2.node_id, rel_type=rt.rel_type)
    async_session.add(e2)
    with pytest.raises(Exception, match="uq_kp_edge_src_dst_rel|unique"):
        await async_session.flush()
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §3 ORM 读写正常路径
# ────────────────────────────────────────────────────────────────────

async def test_orm_insert_query_relation_type(async_session: AsyncSession):
    """ORM 写入并查询 relation_type 行。"""
    rt = RelationType(
        rel_type="prerequisite",
        pack_id=None,  # 平台级
        directed=True,
        transitive=True,
        acyclic=True,
        symmetric=False,
        description="先修关系：src 是 dst 的先修知识点",
    )
    async_session.add(rt)
    await async_session.commit()

    fetched = await async_session.get(RelationType, "prerequisite")
    assert fetched is not None
    assert fetched.directed is True
    assert fetched.transitive is True
    assert fetched.acyclic is True
    assert fetched.symmetric is False


async def test_orm_insert_query_kp_node_with_supersede(async_session: AsyncSession):
    """ORM 写入 supersedes 链——新节点指向前节点。"""
    n1 = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code="math.nal.decimal.add", title="小数加法（旧）",
        status="active",
    )
    async_session.add(n1)
    await async_session.flush()

    n2 = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code="math.nal.decimal.addition", title="小数加法（新）",
        status="active", supersedes_id=n1.node_id,
    )
    async_session.add(n2)
    await async_session.commit()

    # 查询：n2.supersedes_id 指向 n1
    fetched = await async_session.get(KpNode, n2.node_id)
    assert fetched is not None
    assert fetched.supersedes_id == n1.node_id

    # n1 状态可置为 superseded（演进纪律）
    await async_session.execute(
        text("UPDATE kp_node SET status = 'superseded' WHERE node_id = :nid"),
        {"nid": n1.node_id},
    )
    await async_session.commit()


async def test_orm_insert_query_kp_edge(async_session: AsyncSession):
    """ORM 写入并查询 kp_edge：n1 ──prerequisite──> n2."""
    n1 = KpNode(node_id=_uid(), pack_id="subject-math", dimension="kp",
                code="math.nal.int.add", title="整数加法")
    n2 = KpNode(node_id=_uid(), pack_id="subject-math", dimension="kp",
                code="math.nal.decimal.add", title="小数加法")
    rt = RelationType(rel_type="prerequisite", transitive=True,
                      description="先修关系")
    async_session.add_all([n1, n2, rt])
    await async_session.flush()

    e = KpEdge(
        src_node_id=n1.node_id, dst_node_id=n2.node_id, rel_type="prerequisite",
        attrs={"strength": 0.9},
        provenance={"source": "课标2022", "reviewer": "教研组"},
    )
    async_session.add(e)
    await async_session.commit()

    # 查询
    result = await async_session.execute(
        select(KpEdge).where(KpEdge.rel_type == "prerequisite")
    )
    edges = result.scalars().all()
    assert len(edges) == 1
    assert edges[0].src_node_id == n1.node_id
    assert edges[0].dst_node_id == n2.node_id
    assert edges[0].attrs["strength"] == 0.9
    assert edges[0].provenance["source"] == "课标2022"


# ────────────────────────────────────────────────────────────────────
# §4 默认值验证
# ────────────────────────────────────────────────────────────────────

async def test_relation_type_defaults(async_session: AsyncSession):
    """relation_type 布尔元数据默认值：directed=true, transitive=false, acyclic=true, symmetric=false."""
    rt = RelationType(rel_type="default_test", description="测试默认值")
    async_session.add(rt)
    await async_session.commit()

    fetched = await async_session.get(RelationType, "default_test")
    assert fetched.directed is True
    assert fetched.transitive is False
    assert fetched.acyclic is True
    assert fetched.symmetric is False


async def test_kp_node_status_defaults_to_draft(async_session: AsyncSession):
    """kp_node.status 默认 'draft'（演进状态机入口）."""
    n = KpNode(
        node_id=_uid(), pack_id="subject-math", dimension="kp",
        code="math.test.default", title="默认状态测试",
    )
    async_session.add(n)
    await async_session.commit()

    fetched = await async_session.get(KpNode, n.node_id)
    assert fetched.status == "draft"


async def test_kp_node_status_enum_values(async_session: AsyncSession):
    """kp_node_status_enum 含 draft/active/deprecated/superseded 四态。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'kp_node_status_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert set(values) == {"draft", "active", "deprecated", "superseded"}, (
        f"kp_node_status_enum 值不符：{values}"
    )


# ────────────────────────────────────────────────────────────────────
# §5 学科零特判：核心域不 import 学科包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_kp_models():
    """宪法 A5/X6：src/core/models/kp_*.py 不 import 任何学科包/学段包."""
    import os
    import re

    models_dir = os.path.join("src", "core", "models")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations = []
    for fname in os.listdir(models_dir):
        if not (fname.startswith("kp_") or fname == "relation_type.py"):
            continue
        fpath = os.path.join(models_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if pattern.findall(content):
            violations.append(fname)
    assert not violations, f"知识图谱 ORM 存在学科包 import：{violations}"
