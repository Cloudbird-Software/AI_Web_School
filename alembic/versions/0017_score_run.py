"""T-W4-003 score_run 平行重判结果账（架构 v2 §4.7 / 宪法 D6）.

按 specs/contracts/events/response_event.md §3 重判规则落地独立 score_run 表：
- 新 scorer 版本重放历史事件时写平行 score_run 行，原 response_event.scoring_trace
  永不改动（D1 作答事件账只增不改；契约 §3 原文「原序列不动」）。
- score_run.rerun_of 指向原始事件（或上一级 score_run，支持链式重判）。
- score_run 是独立账（重判结果账），与 response_event 同样 append-only：
  BEFORE UPDATE OR DELETE 触发器物理强制（复用 0003 的 raise_append_only_error）。

为什么不挂到 response_event：契约 §3 实现注记明确「rerun_of 属于 score_run，
不属于 response_event」——本表是 response_event 的平行账，不污染原表。
为什么不是 D1 三本账：D1 三本账 = 内容版本 / 作答事件 / 校验签发。
score_run 是「重判结果账」——可重算的派生数据账，不在三本账正典中，
但因其作为可重算证据（D6），仍施 append-only 物理强制保真。

D5 分场景：purpose_scope 三值域，按场景独立重判（禁止跨场景混估）。
D6 可重放：每次重判绑定 scorer_version + run_label；同事件同版本同标签
幂等（uq_score_run_identity 唯一约束防重复写入）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器：score_run 只增不改（复用 0003 的 raise_append_only_error）
# ────────────────────────────────────────────────────────────────────
# 函数为 CREATE OR REPLACE，重复执行无副作用；本迁移只挂表级触发器。
_TRIGGER_SQL = """
CREATE TRIGGER trg_score_run_append_only
    BEFORE UPDATE OR DELETE ON score_run
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


def _create_score_run() -> None:
    """§4.7 score_run 平行重判结果账.

    - score_run_id：行 id（应用层 ULID），text PK
    - event_id + event_created_at：复合外键 → response_event(event_id, created_at)，
      分区表 PK 含分区键，故本外键也含 created_at（契约 §2 实现注记）
    - rerun_of：链式重判时指向上一级 score_run_id；NULL=直接重判原始事件
    - purpose_scope：场景域 practice/diagnosis/measurement（D5 禁混估）
    - scorer_id / scorer_version：本次重判所用的评分器与版本（来自 ScoreResult）
    - original_scorer_version：原始事件当时的评分器版本（response_event.scoring_trace
      中的 scorer_version），用于新旧版本对比报告
    - dimension_scores / scoring_trace / error_inferences / correct：重判结果，
      与 response_event 同构（便于新旧对比）
    - run_label：批次标签（年度重放/增量重判等），同事件同版本同标签幂等
    - input_snapshot_id：输入数据快照标识（D6 可重放——同代码+同快照必同输出）
    - created_at：行写入时刻
    """
    op.create_table(
        "score_run",
        sa.Column("score_run_id", sa.Text(), primary_key=True),
        sa.Column("event_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("event_created_at", sa.DateTime(timezone=True), nullable=False),
        # 链式重判：指向上一级 score_run_id；NULL=直接重判原始事件
        sa.Column("rerun_of", sa.Text(), nullable=True),
        sa.Column("purpose_scope", sa.Text(), nullable=False),
        sa.Column("scorer_id", sa.Text(), nullable=False),
        sa.Column("scorer_version", sa.Text(), nullable=False),
        sa.Column("original_scorer_version", sa.Text(), nullable=False),
        sa.Column("dimension_scores", JSONB(), nullable=False),
        sa.Column("scoring_trace", JSONB(), nullable=False),
        sa.Column("error_inferences", JSONB(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("run_label", sa.Text(), nullable=True),
        sa.Column("input_snapshot_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # 复合外键：指向 response_event 分区表的 PK (event_id, created_at)
        sa.ForeignKeyConstraint(
            ["event_id", "event_created_at"],
            ["response_event.event_id", "response_event.created_at"],
            name="fk_score_run_response_event",
            ondelete="RESTRICT",
        ),
        # 幂等保护：同事件同批次标签不重复写入
        # 为什么不含 scorer_version：scorer_version 存的是 Scorer 自报版本（审计
        # 追溯），用户传入的 new_scorer_version 是批次标签——同一批次同一事件
        # 只允许写一条 score_run（避免重复重判）。NULL run_label 视为「无批次」，
        # PG 默认 NULL 互不相等，允许同事件多次无标签重判（罕见，但保留能力）。
        # 对非 NULL run_label 的严格幂等见 _create_indexes 的部分唯一索引。
        sa.UniqueConstraint(
            "event_id",
            "event_created_at",
            "run_label",
            name="uq_score_run_identity",
        ),
        sa.CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_score_run_purpose_scope_domain",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引 + 部分唯一索引（run_label 非空时的幂等保护）."""
    op.create_index(
        "ix_score_run_event",
        "score_run",
        ["event_id", "event_created_at"],
    )
    op.create_index(
        "ix_score_run_purpose_scope",
        "score_run",
        ["purpose_scope"],
    )
    op.create_index(
        "ix_score_run_scorer_version",
        "score_run",
        ["scorer_version"],
    )
    op.create_index(
        "ix_score_run_rerun_of",
        "score_run",
        ["rerun_of"],
    )
    # run_label 非空时的严格幂等：同事件同非空标签唯一
    # （uq_score_run_identity 已覆盖，但 PG 对 NULL 视为不相等；
    # 此部分索引为查询便利 + 显式标注语义）
    op.create_index(
        "uq_score_run_identity_nonnull_label",
        "score_run",
        ["event_id", "event_created_at", "run_label"],
        unique=True,
        postgresql_where=sa.text("run_label IS NOT NULL"),
    )


def _create_trigger() -> None:
    """D1 append-only 物理强制：BEFORE UPDATE OR DELETE FOR EACH STATEMENT."""
    binding = op.get_bind()
    # raise_append_only_error() 由 0003 创建，0004/0005/0013 等均复用；
    # CREATE OR REPLACE 保证幂等（与既有迁移一致）。
    binding.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION raise_append_only_error() "
            "RETURNS TRIGGER AS $$ "
            "BEGIN "
            "  RAISE EXCEPTION 'append-only table (D1): UPDATE/DELETE forbidden'; "
            "END; "
            "$$ LANGUAGE plpgsql;"
        )
    )
    binding.execute(sa.text(_TRIGGER_SQL))


def upgrade() -> None:
    """创建 score_run + 索引 + append-only 触发器."""
    _create_score_run()
    _create_indexes()
    _create_trigger()


def downgrade() -> None:
    """删除 score_run + 触发器（触发器函数为 0003 统一函数，不删）."""
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_score_run_append_only ON score_run")
    )
    op.drop_index("uq_score_run_identity_nonnull_label", table_name="score_run")
    op.drop_index("ix_score_run_rerun_of", table_name="score_run")
    op.drop_index("ix_score_run_scorer_version", table_name="score_run")
    op.drop_index("ix_score_run_purpose_scope", table_name="score_run")
    op.drop_index("ix_score_run_event", table_name="score_run")
    op.drop_table("score_run")
