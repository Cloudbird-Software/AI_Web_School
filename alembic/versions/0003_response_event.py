"""T-W1-005 response_event 按月分区 + append-only 触发器.

按 specs/contracts/events/response_event.md v1.0.0 落地 response_event 真实结构，
替代 T-W0-005 中的占位表（0001 创建的 BIGINT id + created_at 两列表）。

契约承载（宪法 D1 三本账之「作答事件账」）：
- §2.1 append-only：BEFORE UPDATE OR DELETE 触发器 RAISE EXCEPTION——物理强制，
  不依赖角色体系（X7 一切 DDL 走迁移；角色/权限为兜底而非唯一防线）。
- §2.2 按月分区：以 created_at 为分区键；PK 必含分区键 → PK=(event_id, created_at)
  （契约 §2 实现注记）。event_id 全局唯一性由应用层 ULID/uuid 保证。
- §1 字段表全要素：event_id/student_alias_id/item_version_id/scene/raw_payload/
  duration_ms/scoring_trace/error_inferences/testlet_id/session_id/
  audio_play_events/source_ref/created_at。duration_ms 与 session_id 按契约 v1.1
  可空（NULL=未知/无会话，禁止填 0 冒充——耗时是健康度监控维度）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
downgrade 重建 0001 占位表（与 0001 完全一致），保证再次 upgrade 可执行。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器函数：append-only 物理强制（D1）
# ────────────────────────────────────────────────────────────────────
_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 三本账只增不改：作答事件账禁止 UPDATE/DELETE。
    -- 为什么用 EXCEPTION 而非 silently skip：违反即失败（宪法开篇铁律），
    -- 应用层应感知错误并走升级流程；silently skip 会让 bug 静默累积。
    RAISE EXCEPTION 'response_event is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

# 治理裁决（#43 Minor 项）：CodeRabbit 建议 FOR EACH ROW，但语句级语义是
# 早期验收钉死的行为（tests/unit/test_response_event_writer.py 断言），且
# X1 反测试削弱门禁止 agent 改动既有断言——升级到行级需人类裁决后同步改
# 契约测试，本仓不做 agent 单方面翻转。
_TRIGGER_SQL = """
CREATE TRIGGER trg_response_event_append_only
    BEFORE UPDATE OR DELETE ON response_event
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""

# ────────────────────────────────────────────────────────────────────
# 初始分区：当前月 + 接下来 3 个月（覆盖一个季度的测试/运行窗口）
# ────────────────────────────────────────────────────────────────────
# 为什么用动态计算而非硬编码：硬编码日期会在几个月后让 INSERT 找不到分区而失败；
# 动态计算保证迁移在任意时间运行都能立刻写入。partition creation 走迁移，
# 后续月份由运维批次补建（契约 §2.2）。
_CREATE_PARTITIONS_SQL = """
DO $$
DECLARE
    m date;
    next_m date;
    part_name text;
    i int;
BEGIN
    FOR i IN 0..3 LOOP
        m := date_trunc('month', CURRENT_DATE + (i || ' month')::interval)::date;
        next_m := date_trunc('month', CURRENT_DATE + ((i+1) || ' month')::interval)::date;
        part_name := 'response_event_' || to_char(m, 'YYYYMM');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF response_event FOR VALUES FROM (%L) TO (%L)',
            part_name, m, next_m
        );
    END LOOP;
END $$;
"""


def _create_scene_enum() -> None:
    """§1 scene 枚举：practice/diagnosis/measurement（D5 分场景独立统计禁止混估）.

    为什么单独 enum 而非 text+CHECK：enum 在 information_schema 有明确类型名，
    便于契约对照测试；CHECK 约束失效时 enum 仍兜底。
    """
    binding = op.get_bind()
    exists = binding.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'response_event_scene_enum'")
    ).scalar()
    if exists:
        return
    binding.execute(
        sa.text(
            "CREATE TYPE response_event_scene_enum AS ENUM "
            "('practice', 'diagnosis', 'measurement')"
        )
    )


def _drop_scene_enum() -> None:
    binding = op.get_bind()
    binding.execute(sa.text("DROP TYPE IF EXISTS response_event_scene_enum"))


def _drop_placeholder_response_event() -> None:
    """0001 创建的 response_event 占位表（BIGINT id + created_at）让位给真实结构."""
    # 幂等：若占位表已被先前的部分迁移删除，则跳过。
    binding = op.get_bind()
    exists = binding.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'response_event'"
        )
    ).scalar()
    if exists:
        op.drop_table("response_event")


def _create_response_event() -> None:
    """§1 response_event 主表：按月分区，PK=(event_id, created_at)（契约 §2 实现注记）.

    为什么 PK 含 created_at：PG 分区表的主键必须含分区键。event_id 全局唯一性
    由应用层 ULID/uuid 保证；被引用需求以 (event_id, created_at) 复合键承载。
    """
    op.create_table(
        "response_event",
        sa.Column("event_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("student_alias_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column(
            "scene",
            PG_ENUM(
                "practice", "diagnosis", "measurement",
                name="response_event_scene_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("raw_payload", JSONB(), nullable=False),
        # §1 duration_ms：NULL=未知（纸卷回录 S2 无真实耗时，禁止填 0 冒充）
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("scoring_trace", JSONB(), nullable=False),
        sa.Column("error_inferences", JSONB(), nullable=False),
        sa.Column("testlet_id", sa.Text(), nullable=True),
        # §1 session_id：NULL=无会话（纸卷录入场景；S2 批次标识放 source_ref.batch_id）
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("audio_play_events", JSONB(), nullable=True),
        sa.Column("source_ref", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("event_id", "created_at", name="pk_response_event"),
        # 分区键：按 created_at 月度 RANGE 分区
        postgresql_partition_by="RANGE (created_at)",
    )


def _create_trigger() -> None:
    """D1 append-only 物理强制：BEFORE UPDATE OR DELETE FOR EACH STATEMENT."""
    binding = op.get_bind()
    binding.execute(sa.text(_TRIGGER_FUNCTION_SQL))
    binding.execute(sa.text(_TRIGGER_SQL))


def _drop_trigger() -> None:
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_response_event_append_only ON response_event")
    )
    binding.execute(sa.text("DROP FUNCTION IF EXISTS raise_append_only_error()"))


def _create_partitions() -> None:
    """§2.2 至少创建初始分区：当前月 + 接下来 3 个月."""
    binding = op.get_bind()
    binding.execute(sa.text(_CREATE_PARTITIONS_SQL))


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 response_event 真实结构（分区 + 触发器 + 初始分区）."""
    _create_scene_enum()
    # 先删 0001 占位表
    _drop_placeholder_response_event()
    # 建真实分区表
    _create_response_event()
    # 触发器（在表建完后挂）
    _create_trigger()
    # 初始分区（在表与触发器就绪后）
    _create_partitions()


def downgrade() -> None:
    """回滚：删 response_event 真实结构，重建 0001 占位表.

    为什么重建占位：migrate-check 跑 upgrade→downgrade→upgrade，downgrade 必须
    让库回到 0001 的状态，否则再次 upgrade 会在 _drop_placeholder_response_event
    处失败（表已不存在）。
    """
    _drop_trigger()
    # DROP 主表自动级联删除所有分区（PG 分区表行为）
    op.drop_table("response_event")
    _drop_scene_enum()
    # 重建 0001 占位表（与 0001 migration 完全一致）
    op.create_table(
        "response_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
