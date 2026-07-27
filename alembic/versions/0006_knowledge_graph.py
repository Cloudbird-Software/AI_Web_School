"""T-W2-012 知识图谱底座：kp_node / kp_edge / relation_type 三表.

按 ADR/母题与知识体系-工程架构设计方案v2.md §4.2 与附录 A 落地：
- kp_node(node_id, pack_id, dimension, code, title, std_anchor, gradeband,
  status, valid_from, valid_to, supersedes_id)
- kp_edge(edge_id, src_node_id, dst_node_id, rel_type, attrs, provenance,
  valid_from, valid_to)
- relation_type(rel_type, directed, transitive, acyclic, symmetric, description)

为什么不用图数据库：架构 v2 §4.2 明确——千级节点、万级边、1–3 跳浅遍历、
十年运维成本不值；PostgreSQL 单库足以承载（递归 CTE + 闭包扁平表 kp_closure
在 T-W2-013 落地）。

演进纪律（架构 v2 §4.2）：
- code 一经发布冻结语义；修订=新编码+旧编码 deprecated+supersede 链。
- 节点/边带 valid_from/valid_to 有效期；查询支持"按当时图谱/映射到当前图谱"
  双模式（R-K-05）。supersedes_id 自引用指向前版本节点。
- relation_type 元数据承载图算法语义：directed（方向性）、transitive（传递性
  触发闭包展开）、acyclic（无环约束）、symmetric（对称边双向自动存在）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 枚举类型
# ────────────────────────────────────────────────────────────────────
# 为什么单建 kp_node_status_enum：知识节点状态机与 item_version 状态机语义不同
# （draft→active→deprecated/superseded，无 quarantined/published/retired），
# 不复用 item_version_status_enum 以免概念混淆。
ENUM_DEFINITIONS = [
    # 架构 v2 §4.2 演进纪律：draft→active；废弃走 deprecated；被新编码取代走 superseded
    ("kp_node_status_enum", ("draft", "active", "deprecated", "superseded")),
]


def _create_enums() -> None:
    """创建所有枚举类型（幂等：已存在则跳过）.

    为什么不用 sa.Enum 的自动创建：与 0002 同样问题——显式 checkfirst 更可控。
    """
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

def _create_relation_type() -> None:
    """relation_type 元数据表：定义边类型的图算法语义.

    架构 v2 §4.2：directed/transitive/acyclic/symmetric 四元元数据承载
    图算法语义——传递性触发 kp_closure 展开（T-W2-013），无环约束可在
    闭包计算时校验，对称边自动反向存在。
    """
    op.create_table(
        "relation_type",
        sa.Column("rel_type", sa.Text(), primary_key=True),
        # pack_id 为 NULL 表示平台级通用关系类型（如 prerequisite）
        sa.Column("pack_id", sa.Text(), nullable=True),
        # 默认 directed=true：知识图谱绝大多数关系有方向（先修、组成）
        sa.Column("directed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # transitive=false：默认非传递，仅 prerequisite/composes 等少数传递
        sa.Column("transitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # acyclic=true：默认无环，先修/组成必须无环（防止循环依赖）
        sa.Column("acyclic", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # symmetric=false：默认非对称，confusable 等少数对称
        sa.Column("symmetric", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def _create_kp_node() -> None:
    """kp_node 知识节点表：多维图谱的节点实体.

    架构 v2 §4.2：
    - dimension 一等公民（'kp' 知识点 / 'error_type' 错误类型 / 'literacy' 核心素养 等）
    - code 冻结语义：一经发布不可改义，修订走 deprecated+supersede
    - gradeband 学段标签：'L'(低 1-2) / 'M'(中 3-4) / 'H'(高 5-6)，可多学段
    - supersedes_id 自引用：本节点取代的前版本节点
    """
    op.create_table(
        "kp_node",
        sa.Column("node_id", sa.Text(), primary_key=True),  # ULID
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("std_anchor", sa.Text(), nullable=True),  # 课标锚点（如 '课标2022.nal.3-4.3'）
        # gradeband：单学段 'L'/'M'/'H'；多学段 'L,M'；NULL = 跨学段通用
        sa.Column("gradeband", sa.Text(), nullable=True),
        sa.Column(
            "status",
            PG_ENUM("draft", "active", "deprecated", "superseded", name="kp_node_status_enum", create_type=False),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        # supersedes_id 自引用 FK 后加（循环外键，DEFERRABLE）
        sa.Column("supersedes_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # code 冻结语义：(pack_id, dimension, code) 全局唯一——同 pack 同维度不可重复 code
        sa.UniqueConstraint(
            "pack_id", "dimension", "code", name="uq_kp_node_pack_dim_code",
        ),
    )
    # 循环外键：supersedes_id → kp_node.node_id（DEFERRABLE）
    op.create_foreign_key(
        "fk_kp_node_supersedes",
        "kp_node",
        "kp_node",
        ["supersedes_id"],
        ["node_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_kp_edge() -> None:
    """kp_edge 知识边表：节点间类型化关系.

    架构 v2 §4.2：kp_edge(src, dst, rel_type, attrs, provenance)
    - src/dst 节点引用（FK→kp_node）
    - rel_type 关系类型（FK→relation_type）
    - attrs 关系属性 JSONB（如先修强度、组成权重）
    - provenance 来源 JSONB（课标/教研/AI/外部数据）
    - 有效期 valid_from/valid_to：支持演进"按当时图谱"双模式查询
    """
    op.create_table(
        "kp_edge",
        sa.Column("edge_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("src_node_id", sa.Text(), nullable=False),
        sa.Column("dst_node_id", sa.Text(), nullable=False),
        sa.Column("rel_type", sa.Text(), nullable=False),
        sa.Column("attrs", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("provenance", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["src_node_id"], ["kp_node.node_id"], name="fk_kpe_src"),
        sa.ForeignKeyConstraint(["dst_node_id"], ["kp_node.node_id"], name="fk_kpe_dst"),
        sa.ForeignKeyConstraint(["rel_type"], ["relation_type.rel_type"], name="fk_kpe_rel_type"),
        # 同源同宿同关系类型唯一（防重复边；不同 attrs 视为重复，需新建 rel_type）
        sa.UniqueConstraint(
            "src_node_id", "dst_node_id", "rel_type", name="uq_kp_edge_src_dst_rel",
        ),
        # 自环禁止：知识图谱不允许 src=dst 的自引用边
        sa.CheckConstraint("src_node_id <> dst_node_id", name="ck_kp_edge_no_self_loop"),
    )


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 relation_type / kp_node / kp_edge 三表 + 枚举."""
    _create_enums()
    _create_relation_type()
    _create_kp_node()
    _create_kp_edge()


def downgrade() -> None:
    """回滚：按依赖反序删表 + 删枚举.

    为什么先删 kp_edge：kp_edge 依赖 kp_node 与 relation_type（FK）；
    kp_node 的 supersedes_id 自引用 FK 在删表前需先解除。
    """
    op.drop_table("kp_edge")
    op.drop_constraint("fk_kp_node_supersedes", "kp_node", type_="foreignkey")
    op.drop_table("kp_node")
    op.drop_table("relation_type")
    _drop_enums()
