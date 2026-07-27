"""T-W2-013 知识图谱闭包计算单元测试.

对照 T-W2-013 任务卡验收标准：
1. kp_closure 含 graph_release/src/dst/depth/path_count。
2. compute_closure(graph_release_id) 从 kp_edge 计算闭包并写入
   （对 transitive 边展开，非 transitive 边 depth=1）。
3. graph_release 表含 release_id/status/valid_from/valid_to/superseded_by。
4. 单元测试覆盖先修链、易混淆非传递、版本切换三种场景。

测试隔离：autouse fixture 在每测试前 TRUNCATE 知识图谱五表 CASCADE。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.knowledge.closure import compute_closure
from src.core.models.graph_release import GraphRelease
from src.core.models.kp_closure import KpClosure
from src.core.models.kp_edge import KpEdge
from src.core.models.kp_node import KpNode
from src.core.models.relation_type import RelationType


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture
# ────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _truncate_kp_tables(async_session: AsyncSession):
    """每测试前 TRUNCATE 知识图谱五表（含闭包/版本表）CASCADE。"""
    await async_session.execute(
        text(
            "TRUNCATE TABLE "
            "kp_closure, kp_edge, kp_node, relation_type, graph_release "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


def _uid(prefix: str = "node") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ────────────────────────────────────────────────────────────────────
# 共享 fixture：种子节点 + 关系类型 + 图谱版本
# ────────────────────────────────────────────────────────────────────

async def _seed_prerequisite_chain(async_session: AsyncSession) -> tuple[list[str], str]:
    """种 A→B→C 的先修链（transitive=True，应展开 A→C depth=2）。

    Returns: ([A_id, B_id, C_id], graph_release_id)
    """
    # 节点
    a = KpNode(node_id=_uid(), pack_id="subject-math", dimension="kp",
               code="math.A", title="A")
    b = KpNode(node_id=_uid(), pack_id="subject-math", dimension="kp",
               code="math.B", title="B")
    c = KpNode(node_id=_uid(), pack_id="subject-math", dimension="kp",
               code="math.C", title="C")
    async_session.add_all([a, b, c])
    await async_session.flush()

    # 关系类型：prerequisite transitive=True
    rt = RelationType(
        rel_type="prerequisite",
        directed=True, transitive=True, acyclic=True, symmetric=False,
        description="先修关系（传递）",
    )
    async_session.add(rt)
    await async_session.flush()

    # 边：A→B，B→C
    async_session.add_all([
        KpEdge(src_node_id=a.node_id, dst_node_id=b.node_id, rel_type="prerequisite"),
        KpEdge(src_node_id=b.node_id, dst_node_id=c.node_id, rel_type="prerequisite"),
    ])

    # 图谱版本
    gr = GraphRelease(release_id="2026.1.test", status="active")
    async_session.add(gr)
    await async_session.commit()

    return [a.node_id, b.node_id, c.node_id], "2026.1.test"


async def _seed_confusable_pair(async_session: AsyncSession) -> tuple[list[str], str]:
    """种 A↔B 易混淆对（transitive=False，应仅 depth=1）。

    Returns: ([A_id, B_id], graph_release_id)
    """
    a = KpNode(node_id=_uid(), pack_id="subject-chinese", dimension="kp",
               code="ch.A", title="A")
    b = KpNode(node_id=_uid(), pack_id="subject-chinese", dimension="kp",
               code="ch.B", title="B")
    async_session.add_all([a, b])
    await async_session.flush()

    # confusable: symmetric=True, transitive=False（非传递）
    rt = RelationType(
        rel_type="confusable",
        directed=False, transitive=False, acyclic=False, symmetric=True,
        description="易混淆关系（非传递）",
    )
    async_session.add(rt)
    await async_session.flush()

    # 边：A→B（symmetric 语义由应用层解释，DB 只存一条）
    async_session.add(KpEdge(src_node_id=a.node_id, dst_node_id=b.node_id, rel_type="confusable"))

    gr = GraphRelease(release_id="2026.1.confusable", status="active")
    async_session.add(gr)
    await async_session.commit()

    return [a.node_id, b.node_id], "2026.1.confusable"


# ────────────────────────────────────────────────────────────────────
# §1 表结构与字段对齐
# ────────────────────────────────────────────────────────────────────

async def test_kp_closure_columns_match_contract(async_session: AsyncSession):
    """验收 #1：kp_closure 含 graph_release/src/dst/depth/path_count."""
    result = await async_session.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'kp_closure'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    required = {"graph_release_id", "src_node_id", "dst_node_id", "depth", "path_count"}
    missing = required - cols
    assert not missing, f"kp_closure 缺字段: {missing}"


async def test_graph_release_columns_match_contract(async_session: AsyncSession):
    """验收 #3：graph_release 含 release_id/status/valid_from/valid_to/superseded_by."""
    result = await async_session.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'graph_release'
            """
        )
    )
    cols = {row[0] for row in result.fetchall()}
    required = {"release_id", "status", "valid_from", "valid_to", "superseded_by"}
    missing = required - cols
    assert not missing, f"graph_release 缺字段: {missing}"


# ────────────────────────────────────────────────────────────────────
# §2 compute_closure：先修链传递展开
# ────────────────────────────────────────────────────────────────────

async def test_compute_closure_prerequisite_chain(async_session: AsyncSession):
    """验收 #4 场景 1：A→B→C 先修链应展开 A→C depth=2."""
    [a_id, b_id, c_id], gr_id = await _seed_prerequisite_chain(async_session)

    result = await compute_closure(gr_id, async_session)

    assert result["graph_release_id"] == gr_id
    assert result["closure_rows"] >= 3, f"应有 ≥3 条闭包（A→B,B→C,A→C），实际 {result['closure_rows']}"
    assert "prerequisite" in result["transitive_rel_types"]

    # 验证 A→C depth=2 存在（先修链传递展开）
    row = await async_session.execute(
        text(
            """
            SELECT depth, path_count FROM kp_closure
            WHERE graph_release_id = :grid
              AND src_node_id = :src AND dst_node_id = :dst
              AND rel_type = 'prerequisite'
            """
        ),
        {"grid": gr_id, "src": a_id, "dst": c_id},
    )
    ac_rows = row.fetchall()
    assert len(ac_rows) == 1, f"A→C 应有 1 条闭包条目，实际 {len(ac_rows)}"
    assert ac_rows[0][0] == 2, f"A→C depth 应为 2，实际 {ac_rows[0][0]}"
    assert ac_rows[0][1] == 1, f"A→C path_count 应为 1（单一路径），实际 {ac_rows[0][1]}"

    # 验证直接边 depth=1
    direct = await async_session.execute(
        text(
            """
            SELECT depth FROM kp_closure
            WHERE graph_release_id = :grid
              AND src_node_id = :src AND dst_node_id = :dst
              AND rel_type = 'prerequisite'
            """
        ),
        {"grid": gr_id, "src": a_id, "dst": b_id},
    )
    ab_rows = direct.fetchall()
    assert len(ab_rows) == 1
    assert ab_rows[0][0] == 1


# ────────────────────────────────────────────────────────────────────
# §3 compute_closure：易混淆非传递
# ────────────────────────────────────────────────────────────────────

async def test_compute_closure_confusable_non_transitive(async_session: AsyncSession):
    """验收 #4 场景 2：confusable transitive=False 仅 depth=1，无 A→A 自环."""
    [a_id, b_id], gr_id = await _seed_confusable_pair(async_session)

    result = await compute_closure(gr_id, async_session)

    assert "confusable" in result["non_transitive_rel_types"]
    assert result["closure_rows"] == 1, f"confusable 非传递应仅 1 条直接边，实际 {result['closure_rows']}"

    # 验证 A→B depth=1 存在
    row = await async_session.execute(
        text(
            """
            SELECT depth FROM kp_closure
            WHERE graph_release_id = :grid
              AND src_node_id = :src AND dst_node_id = :dst
              AND rel_type = 'confusable'
            """
        ),
        {"grid": gr_id, "src": a_id, "dst": b_id},
    )
    rows = row.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1

    # 验证无 A→A 自环（depth>=2 的传递展开应不存在）
    self_loops = await async_session.execute(
        text(
            """
            SELECT COUNT(*) FROM kp_closure
            WHERE graph_release_id = :grid AND src_node_id = dst_node_id
            """
        ),
        {"grid": gr_id},
    )
    assert self_loops.scalar() == 0, "闭包不应有自环"


# ────────────────────────────────────────────────────────────────────
# §4 compute_closure：版本切换
# ────────────────────────────────────────────────────────────────────

async def test_compute_closure_version_switch(async_session: AsyncSession):
    """验收 #4 场景 3：版本切换——同一图不同 release 应产生独立闭包。

    gr1 有 A→B→C（应产生 A→C depth=2）
    gr2 只有 A→B（应仅 depth=1，无 A→C）
    两个 release 的闭包互不影响。
    """
    # ── gr1: 完整链 A→B→C ──
    [a_id, b_id, c_id], gr1_id = await _seed_prerequisite_chain(async_session)

    # ── gr2: 只有 A→B，不含 B→C ──
    # 复用节点 a, b, c；为 gr2 设边有效期为不同时间窗
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=10)
    future = now + timedelta(days=10)

    # gr2 valid_from 设为 past（10 天前），此时只有 A→B 边有效（B→C 在未来才生效）
    gr2 = GraphRelease(
        release_id="2026.0.past",
        status="frozen",
        valid_from=past,
    )
    async_session.add(gr2)

    # 给 B→C 边加 valid_from=future（gr2 视角下不可见）
    await async_session.execute(
        text(
            "UPDATE kp_edge SET valid_from = :vf "
            "WHERE src_node_id = :src AND dst_node_id = :dst"
        ),
        {"vf": future, "src": b_id, "dst": c_id},
    )
    await async_session.commit()

    # 计算 gr1 闭包（应该有 A→C depth=2）
    r1 = await compute_closure(gr1_id, async_session)
    assert r1["closure_rows"] >= 3

    # 计算 gr2 闭包（应只有 A→B depth=1，无 A→C 因为 B→C 在 future）
    r2 = await compute_closure(gr2.release_id, async_session)
    assert r2["closure_rows"] == 1, (
        f"gr2 应仅 1 条闭包（A→B），实际 {r2['closure_rows']}"
    )

    # 验证 gr2 无 A→C 闭包条目
    ac_count = await async_session.execute(
        text(
            """
            SELECT COUNT(*) FROM kp_closure
            WHERE graph_release_id = :grid
              AND src_node_id = :src AND dst_node_id = :dst
            """
        ),
        {"grid": gr2.release_id, "src": a_id, "dst": c_id},
    )
    assert ac_count.scalar() == 0, "gr2 不应有 A→C 闭包条目"

    # 验证 gr1 仍有 A→C（版本切换不互相影响）
    gr1_ac = await async_session.execute(
        text(
            """
            SELECT depth FROM kp_closure
            WHERE graph_release_id = :grid
              AND src_node_id = :src AND dst_node_id = :dst
            """
        ),
        {"grid": gr1_id, "src": a_id, "dst": c_id},
    )
    assert gr1_ac.fetchone() is not None, "gr1 应仍有 A→C depth=2 闭包"


# ────────────────────────────────────────────────────────────────────
# §5 compute_closure：幂等性
# ────────────────────────────────────────────────────────────────────

async def test_compute_closure_idempotent(async_session: AsyncSession):
    """compute_closure 幂等：重复调用不产生重复条目."""
    [a_id, b_id, c_id], gr_id = await _seed_prerequisite_chain(async_session)

    r1 = await compute_closure(gr_id, async_session)
    r2 = await compute_closure(gr_id, async_session)

    assert r1["closure_rows"] == r2["closure_rows"], (
        f"幂等性失败：两次调用写入行数不同 {r1['closure_rows']} != {r2['closure_rows']}"
    )

    # 总条目数应等于一次调用（无重复）
    total = await async_session.execute(
        text(
            "SELECT COUNT(*) FROM kp_closure WHERE graph_release_id = :grid"
        ),
        {"grid": gr_id},
    )
    assert total.scalar() == r1["closure_rows"]


# ────────────────────────────────────────────────────────────────────
# §6 错误路径
# ────────────────────────────────────────────────────────────────────

async def test_compute_closure_invalid_release_raises(async_session: AsyncSession):
    """不存在的 graph_release_id 应抛 ValueError."""
    with pytest.raises(ValueError, match="不存在"):
        await compute_closure("nonexistent-release", async_session)


async def test_compute_closure_invalid_max_depth(async_session: AsyncSession):
    """max_depth < 1 应抛 ValueError."""
    [a_id, b_id, c_id], gr_id = await _seed_prerequisite_chain(async_session)
    with pytest.raises(ValueError, match="max_depth"):
        await compute_closure(gr_id, async_session, max_depth=0)


# ────────────────────────────────────────────────────────────────────
# §7 学科零特判
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_knowledge():
    """宪法 A5/X6：src/core/knowledge/ 不 import 任何学科包/学段包."""
    import os
    import re

    knowledge_dir = os.path.join("src", "core", "knowledge")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations = []
    for fname in os.listdir(knowledge_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(knowledge_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if pattern.findall(content):
            violations.append(fname)
    assert not violations, f"src/core/knowledge/ 存在学科包 import：{violations}"
