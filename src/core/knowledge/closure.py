"""T-W2-013 知识图谱传递闭包计算模块.

架构 v2 §4.2：闭包预计算后写入 kp_closure 扁平表，热路径查询退化为
单表过滤；递归 CTE 仅在闭包计算时使用（管理查询，非热路径）。

闭包计算规则（任务卡验收 #2）：
- 对 transitive 关系类型（如 prerequisite/composes）：递归展开多跳可达
- 对非 transitive 关系类型（如 confusable）：仅 depth=1 直接边
- path_count：同 (src, dst, rel_type, depth) 的不同路径数

为什么用递归 CTE 而非应用层 BFS：千级节点万级边的图，递归 CTE 在
PostgreSQL 内执行比应用层往返高效；同时利用 DB 端的查询优化器。
循环检测通过 visited 数组实现（防止 acyclic 约束被违反时无限递归）。

宪法 A5/A7：本模块不 import 任何学科包/学段包（学科零特判）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.graph_release import GraphRelease
from src.core.models.kp_closure import KpClosure
from src.core.models.relation_type import RelationType


# 闭包深度安全上限——超过此深度判定为环路（违反 acyclic 约束）
_MAX_DEPTH = 50


# 传递闭包递归 CTE：枚举所有路径，再按 (src, dst, depth) 聚合 path_count
# 为什么 visited 数组：避免 acyclic 约束被违反时无限递归（深度爆炸保护）
# 为什么 as_of IS NULL 时不做时间过滤：graph_release.valid_from=NULL 表示
#   "实时快照"（active 状态无时间约束），应包含所有边无论其时间窗如何。
#   仅当 release 携带 valid_from 时才按时间窗过滤（frozen/historical 快照）。
_TRANSITIVE_CLOSURE_SQL = """
WITH RECURSIVE paths AS (
    -- 基础：直接边（depth=1）
    SELECT
        e.src_node_id AS src_node_id,
        e.dst_node_id AS dst_node_id,
        e.rel_type AS rel_type,
        1 AS depth,
        ARRAY[e.src_node_id, e.dst_node_id] AS visited
    FROM kp_edge e
    WHERE e.rel_type = :rel_type
      AND (
        CAST(:as_of AS timestamptz) IS NULL
        OR ((e.valid_from IS NULL OR e.valid_from <= CAST(:as_of AS timestamptz))
            AND (e.valid_to IS NULL OR e.valid_to > CAST(:as_of AS timestamptz)))
      )

    UNION ALL

    -- 递归：在路径末端扩展一跳
    SELECT
        p.src_node_id,
        e.dst_node_id,
        p.rel_type,
        p.depth + 1,
        p.visited || e.dst_node_id
    FROM paths p
    JOIN kp_edge e
      ON e.src_node_id = p.dst_node_id
     AND e.rel_type = p.rel_type
    WHERE p.depth < :max_depth
      AND e.dst_node_id <> ALL(p.visited)  -- 循环检测
      AND (
        CAST(:as_of AS timestamptz) IS NULL
        OR ((e.valid_from IS NULL OR e.valid_from <= CAST(:as_of AS timestamptz))
            AND (e.valid_to IS NULL OR e.valid_to > CAST(:as_of AS timestamptz)))
      )
)
SELECT
    src_node_id,
    dst_node_id,
    rel_type,
    depth,
    COUNT(*) AS path_count
FROM paths
GROUP BY src_node_id, dst_node_id, rel_type, depth
"""


# 非传递关系：仅直接边（depth=1，path_count=1）
_NON_TRANSITIVE_DIRECT_SQL = """
SELECT
    e.src_node_id AS src_node_id,
    e.dst_node_id AS dst_node_id,
    e.rel_type AS rel_type,
    1 AS depth,
    1 AS path_count
FROM kp_edge e
WHERE e.rel_type = :rel_type
  AND (
    CAST(:as_of AS timestamptz) IS NULL
    OR ((e.valid_from IS NULL OR e.valid_from <= CAST(:as_of AS timestamptz))
        AND (e.valid_to IS NULL OR e.valid_to > CAST(:as_of AS timestamptz)))
  )
"""


# 写入闭包条目（按 graph_release_id 关联版本）
_INSERT_CLOSURE_SQL = """
INSERT INTO kp_closure
    (graph_release_id, src_node_id, dst_node_id, rel_type, depth, path_count)
VALUES
    (:graph_release_id, :src_node_id, :dst_node_id, :rel_type, :depth, :path_count)
"""


async def compute_closure(
    graph_release_id: str,
    db: AsyncSession,
    max_depth: int = _MAX_DEPTH,
) -> dict[str, Any]:
    """计算并写入 kp_closure（按 graph_release 版本缓存）.

    幂等：先删除该 graph_release 的既有闭包条目，再重新计算写入。

    Args:
        graph_release_id: 目标图谱版本 id（必须已存在于 graph_release 表）。
        db: AsyncSession（必填）。
        max_depth: 递归深度上限（默认 50，超过判定为环路）。

    Returns:
        统计信息 dict：
        {
            "graph_release_id": str,
            "as_of": datetime | None,
            "closure_rows": int,            # 写入条目总数
            "transitive_rel_types": list[str],
            "non_transitive_rel_types": list[str],
        }

    Raises:
        ValueError: graph_release_id 不存在；max_depth < 1。
    """
    if max_depth < 1:
        raise ValueError(f"max_depth 必须 >= 1，实际 {max_depth}")

    # ── 校验 graph_release 存在并取 valid_from 作为 as-of 时间 ──
    release = await db.get(GraphRelease, graph_release_id)
    if release is None:
        raise ValueError(f"graph_release_id={graph_release_id!r} 不存在")
    as_of = release.valid_from

    # ── 收集所有 relation_type 与其 transitive 标志 ──
    result = await db.execute(text("SELECT rel_type, transitive FROM relation_type"))
    rel_types: list[tuple[str, bool]] = [
        (row[0], bool(row[1])) for row in result.fetchall()
    ]

    # ── 幂等：先删既有闭包条目 ──
    await db.execute(
        text("DELETE FROM kp_closure WHERE graph_release_id = :grid"),
        {"grid": graph_release_id},
    )

    transitive_used: list[str] = []
    non_transitive_used: list[str] = []
    total_rows = 0

    params_common: dict[str, Any] = {
        "graph_release_id": graph_release_id,
        "as_of": as_of,
        "max_depth": max_depth,
    }

    for rel_type, is_transitive in rel_types:
        params = {**params_common, "rel_type": rel_type}
        if is_transitive:
            rows = (await db.execute(text(_TRANSITIVE_CLOSURE_SQL), params)).fetchall()
            transitive_used.append(rel_type)
        else:
            rows = (await db.execute(text(_NON_TRANSITIVE_DIRECT_SQL), params)).fetchall()
            non_transitive_used.append(rel_type)

        # 批量写入
        for row in rows:
            await db.execute(
                text(_INSERT_CLOSURE_SQL),
                {
                    "graph_release_id": graph_release_id,
                    "src_node_id": row[0],
                    "dst_node_id": row[1],
                    "rel_type": row[2],
                    "depth": int(row[3]),
                    "path_count": int(row[4]),
                },
            )
            total_rows += 1

    await db.commit()

    return {
        "graph_release_id": graph_release_id,
        "as_of": as_of,
        "closure_rows": total_rows,
        "transitive_rel_types": transitive_used,
        "non_transitive_rel_types": non_transitive_used,
    }


__all__ = ["compute_closure"]
