"""T-W2-013 知识图谱闭包与版本机制：kp_closure + graph_release.

按 ADR/母题与知识体系-工程架构设计方案v2.md §4.2 与附录 A 落地：
- graph_release(release_id, status, valid_from, valid_to, superseded_by)
  图谱版本：每次图谱演进产生新 release，旧 release 被 supersede；
  支持"按当时图谱/映射到当前图谱"双模式查询（R-K-05）。
- kp_closure(graph_release_id, src_node_id, dst_node_id, rel_type, depth, path_count)
  传递闭包扁平表：热路径只读扁平表（架构 v2 §4.2 明示不引入图数据库，
  递归 CTE 仅管理查询；闭包预计算后查询走 O(1) 表扫描）。

为什么 closure 单独建表：递归 CTE 在千级节点万级边的图上 1-3 跳查询
代价较高；按 graph_release 版本缓存闭包后，热路径查询退化为单表过滤。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 枚举类型
# ────────────────────────────────────────────────────────────────────
# 为什么单建 graph_release_status_enum：图谱版本状态机与 kp_node/item_version
# 状态机语义不同（draft→active→frozen→superseded，frozen 表示历史可查不可改）。
ENUM_DEFINITIONS = [
    ("graph_release_status_enum", ("draft", "active", "frozen", "superseded")),
]


def _create_enums() -> None:
    """创建所有枚举类型（幂等：已存在则跳过）."""
    binding = op.get_bind()
    for name, values in ENUM_DEFINITIONS:
        exists = binding.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": name},
        ).scalar()
        if exists:
            continue
        values_sql = ", ".join(f"'{v}'" for v in values)
        binding.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({values_sql})"))


def _drop_enums() -> None:
    """删除所有枚举类型（反序，幂等）。"""
    binding = op.get_bind()
    for name, _ in reversed(ENUM_DEFINITIONS):
        binding.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))


# ────────────────────────────────────────────────────────────────────
# 表创建
# ────────────────────────────────────────────────────────────────────

def _create_graph_release() -> None:
    """graph_release 图谱版本表：每次图谱演进产生新版本.

    架构 v2 §4.2：图谱版本化缓存，支持"按当时图谱/映射到当前图谱"双模式查询。
    演进纪律：release_id 冻结语义；修订=新 release+旧 release deprecated+supersede。
    - draft：编辑中（可增删节点/边）
    - active：当前生效版本
    - frozen：历史冻结版本（可查不可改）
    - superseded：被新版本取代
    """
    op.create_table(
        "graph_release",
        sa.Column("release_id", sa.Text(), primary_key=True),  # 如 '2026.1'
        sa.Column(
            "status",
            PG_ENUM("draft", "active", "frozen", "superseded", name="graph_release_status_enum", create_type=False),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        # superseded_by 自引用 FK 后加（循环外键，DEFERRABLE）
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_foreign_key(
        "fk_graph_release_superseded_by",
        "graph_release",
        "graph_release",
        ["superseded_by"],
        ["release_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_kp_closure() -> None:
    """kp_closure 传递闭包扁平表：按 graph_release 版本缓存闭包.

    架构 v2 §4.2：热路径只读扁平表，递归 CTE 仅管理查询。
    闭包计算规则（T-W2-013 验收 #2）：
    - 对 transitive 边展开（先修链多跳可达）
    - 非 transitive 边 depth=1（直接边，不传递）
    - path_count：同 src→dst 不同路径数（深度相同的路径聚合）

    为什么 (graph_release_id, src, dst, rel_type, depth) 唯一：
    闭包表是物化视图性质——同图同源同宿同关系同深度应只有一条，
    path_count 字段承载多路径信息。
    """
    op.create_table(
        "kp_closure",
        sa.Column("closure_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("graph_release_id", sa.Text(), nullable=False),
        sa.Column("src_node_id", sa.Text(), nullable=False),
        sa.Column("dst_node_id", sa.Text(), nullable=False),
        sa.Column("rel_type", sa.Text(), nullable=False),
        # depth：1=直接边；>1=经传递展开的多跳可达
        sa.Column("depth", sa.Integer(), nullable=False),
        # path_count：同 (src, dst, rel_type, depth) 的不同路径数
        sa.Column("path_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["graph_release_id"], ["graph_release.release_id"], name="fk_kpc_graph_release"),
        sa.ForeignKeyConstraint(["src_node_id"], ["kp_node.node_id"], name="fk_kpc_src"),
        sa.ForeignKeyConstraint(["dst_node_id"], ["kp_node.node_id"], name="fk_kpc_dst"),
        sa.ForeignKeyConstraint(["rel_type"], ["relation_type.rel_type"], name="fk_kpc_rel_type"),
        # 唯一约束：同图同源同宿同关系同深度唯一（path_count 承载多路径）
        sa.UniqueConstraint(
            "graph_release_id", "src_node_id", "dst_node_id", "rel_type", "depth",
            name="uq_kpc_release_src_dst_rel_depth",
        ),
        # depth >= 1：闭包深度至少为 1（直接边）
        sa.CheckConstraint("depth >= 1", name="ck_kpc_depth_positive"),
        # path_count >= 1：路径数至少为 1
        sa.CheckConstraint("path_count >= 1", name="ck_kpc_path_count_positive"),
        # 闭包条目 src != dst（防止传递展开中出现自环）
        sa.CheckConstraint("src_node_id <> dst_node_id", name="ck_kpc_no_self_loop"),
    )
    # 热路径查询索引：按 graph_release + src 查 dst（先修链查询典型模式）
    op.create_index(
        "ix_kpc_release_src",
        "kp_closure",
        ["graph_release_id", "src_node_id", "rel_type"],
    )


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 graph_release + kp_closure 两表 + 枚举."""
    _create_enums()
    _create_graph_release()
    _create_kp_closure()


def downgrade() -> None:
    """回滚：按依赖反序删表 + 删枚举."""
    op.drop_index("ix_kpc_release_src", table_name="kp_closure")
    op.drop_table("kp_closure")
    op.drop_constraint("fk_graph_release_superseded_by", "graph_release", type_="foreignkey")
    op.drop_table("graph_release")
    _drop_enums()
