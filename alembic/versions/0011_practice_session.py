"""W3-S3 practice_session 在线作答会话表.

按架构 v2 §3.1/§4.5（S3 在线作答闭环）与 §4.8 合规层落地会话状态服务：
- 会话 = 一次在线作答过程（开始练习→取下一题→提交作答→即时反馈→错题回测）。
- item_sequence 在会话开始时快照固化（确定性：卷序列或实例池序列一经开始不变）。
- 时长保护阈值：低段（L）≤15 分钟、中/高段（M/H）≤60 分钟（§4.8 合规层
  「会话层内置时长与用眼保护」），last_resume_at 为保护计时锚点。

为什么不挂 append-only 触发器（与 paper/response_event 不同）：
- 三本账（内容版本/作答事件/校验签发）只增不改是宪法 D1；本表不在三本账内。
- 会话进度（current_index/answered_count/wrong_marks/status）是**运行态操作数据**，
  必须随作答推进原地更新；每次作答的历史事实由 response_event（append-only）
  承载，会话表只保存「当前状态」，不构成历史账。
- 类比 item_version.status 状态机字段：允许 UPDATE，但内容快照（item_sequence）
  一经写入不变（应用层纪律，DB 以 NOT NULL + 应用层只写一次保证）。

迁移可逆性：upgrade→downgrade→upgrade 全绿（同 0009 标准）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_practice_session() -> None:
    """在线作答会话表.

    - session_id：应用层 uuid4 生成（与 response_event.session_id 类型一致）。
    - student_alias_id：匿名学生 id（D7：直接标识只在 PII 保险库 schema）。
    - scene：practice/diagnosis 二值（measurement 首年不做，W3 非目标；
      response_event.scene 三值枚举在本表收窄为会话实际支持的两值）。
    - gradeband：学段（L/M/H），决定时长保护阈值。
    - status：active / rest_prompted（时长保护触发，待休息确认）/
      completed（序列+回测走完）/ abandoned（学生中途放弃）。
    - paper_id：静态卷来源（可空；在线实例池序列为 NULL）。
    - item_sequence：题目序列快照 [{item_version_id, placement_token, item_number}]，
      会话开始时固化，之后不变（确定性）。
    - retest_wrong：是否在主序列走完後对错题进行一轮回测。
    - wrong_marks：错题回测标记 [{item_version_id, error_type_ids,
      first_seen_at, retest_status}]，retest_status ∈ pending/served/passed/failed。
    - time_limit_sec：时长保护阈值快照（L=900，M/H=3600；建会话时按 gradeband
      定型并落列，避免策略变更回溯影响进行中的会话）。
    - last_resume_at：时长保护计时锚点（开始=started_at；休息确认后重置），
      保护判定为 now - last_resume_at > time_limit_sec。
    """
    op.create_table(
        "practice_session",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("student_alias_id", sa.Uuid(), nullable=False),
        sa.Column("scene", sa.Text(), nullable=False),
        sa.Column("gradeband", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
        sa.Column("paper_id", sa.Text(), nullable=True),
        sa.Column("item_sequence", JSONB(), nullable=False),
        sa.Column(
            "current_index",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "retest_wrong",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "wrong_marks",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("time_limit_sec", sa.Integer(), nullable=False),
        sa.Column(
            "answered_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "correct_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_resume_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.paper_id"],
            name="fk_practice_session_paper",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "scene IN ('practice', 'diagnosis')",
            name="ck_practice_session_scene_domain",
        ),
        sa.CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_practice_session_gradeband_domain",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'rest_prompted', 'completed', 'abandoned')",
            name="ck_practice_session_status_domain",
        ),
        sa.CheckConstraint(
            "current_index >= 0",
            name="ck_practice_session_current_index_nonneg",
        ),
        sa.CheckConstraint(
            "time_limit_sec > 0",
            name="ck_practice_session_time_limit_positive",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：按学生查会话（学习记录/复习排程 S6 消费）."""
    op.create_index(
        "ix_practice_session_student",
        "practice_session",
        ["student_alias_id"],
    )


def upgrade() -> None:
    """创建 practice_session + 索引."""
    _create_practice_session()
    _create_indexes()


def downgrade() -> None:
    """删除 practice_session."""
    op.drop_index("ix_practice_session_student", table_name="practice_session")
    op.drop_table("practice_session")
