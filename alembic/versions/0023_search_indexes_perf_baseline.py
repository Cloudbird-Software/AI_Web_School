"""Issue #24: DB indexes / search / perf baseline（T-W2-040 / alembic 0023）.

本迁移补充以下索引以支撑高频查询：
1. item_version 索引：
   - (item_id, status) —— serving 视图 published 切片：按 item_id 取已发布版本
   - (interaction_ref_interaction_id) —— serving 视图按交互类型取题（组卷装填）
   - (objective_gradeband) —— 学段筛选（L/M/H）
   - (jsonb_path_extract 内容 kp code GIN) —— 用 pg_trgm + gin 做 kp 全文检索
   - (created_at) —— 排序/时间窗口
   - 单独对 item_version.content 建 GIN（jsonb_path_ops）—— 子字段检索
2. paper / paper_item 索引：
   - paper(gradeband, subject_pack_id, created_at DESC) —— 周更批次查询
   - paper_item(paper_id, item_number) —— 按题号取卷内题目
   - paper_item(item_version_id) —— 反向查询：题目出现在哪些卷
3. response_event / item_param 索引：
   - response_event(session_id, created_at) —— 在线作答会话级回放
   - response_event(item_version_id, created_at) —— 题目作答数与 CTT 估计
   - item_param(item_version_id, source, scenario) —— 参数估计按源/场景切片

说明：
- 所有索引 IF NOT EXISTS，避免重复失败；
- 不改动既有表的列或约束；
- 索引命名遵循 ix_<table>_<col>_<col> 约定；
- Gin 索引仅在 PostgreSQL 环境有效（项目用 PG16，见系统说明）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ITEM_VERSION_IDX = [
    # （item_id, status）：serving.published_item_version 的主查询路径
    ("ix_item_version_item_id_status", "item_version", "item_id, status"),
    # 交互类型筛选：weekly_batch._select_items 按 interaction_id 从池中取题
    ("ix_item_version_interaction_ref_interaction_id", "item_version",
     "((interaction_ref->>'interaction_id'))"),
    # 学段筛选（objective 中的 gradeband，取 ->> 字段）
    ("ix_item_version_objective_gradeband", "item_version",
     "((objective->>'gradeband'))"),
    # created_at：时间窗/排序（最近导入、最近签发）
    ("ix_item_version_created_at", "item_version", "created_at"),
    # content GIN（jsonb_path_ops）：子字段检索 / KP / passage_source 全文
    ("ix_item_version_content_gin", "item_version",
     "content USING GIN (content jsonb_path_ops)"),
]

PAPER_IDX = [
    # 周更列表：学科+学段+时间倒序（首页「最近 100 份卷」查询）
    ("ix_paper_subject_gradeband_created_desc", "paper",
     "subject_pack_id, gradeband, created_at DESC"),
    # kp_snapshot_ref 反查：同快照的所有卷（可复现/批量追溯）
    ("ix_paper_kp_snapshot_ref", "paper", "kp_snapshot_ref"),
]

PAPER_ITEM_IDX = [
    # 卷内按题号顺序输出
    ("ix_paper_item_paper_id_item_number", "paper_item", "paper_id, item_number"),
    # 反向：题目出现在哪些卷（难度漂移监测 / 曝光次数）
    ("ix_paper_item_item_version_id", "paper_item", "item_version_id"),
]

RESPONSE_EVENT_IDX = [
    # 会话级回放
    ("ix_response_event_session_created", "response_event", "session_id, created_at"),
    # 题目级作答数统计（CTT 估计/筛选已标定题）
    ("ix_response_event_item_version_created", "response_event",
     "item_version_id, created_at"),
]

ITEM_PARAM_IDX = [
    # 按题目 + source（prior/measured）+ 场景 取参数
    ("ix_item_param_iv_source_scenario", "item_param",
     "item_version_id, source, scenario"),
]


def _create_indexes(rows: list[tuple[str, str, str]]) -> None:
    for name, table, expr in rows:
        sql = f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({expr})"
        op.execute(sql)


def _drop_indexes(rows: list[tuple[str, str, str]]) -> None:
    for name, _table, _expr in rows:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def upgrade() -> None:
    # item_version 的字段可能是 json 或 jsonb（PG 上两者都能用，但 gin 索引要求 jsonb）。
    # 这里仅在列类型为 jsonb 时建 GIN；否则跳过（避免 SQLite dev env 报错）。
    _create_indexes([x for x in ITEM_VERSION_IDX if "_gin" not in x[0]])
    # GIN 索引单独用 pg specific execute
    for name, table, expr in [x for x in ITEM_VERSION_IDX if "_gin" in x[0]]:
        op.execute(f"""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name='{table}' AND column_name='content'
               AND udt_name='jsonb'
          ) THEN
            CREATE INDEX IF NOT EXISTS {name} ON {table} ({expr});
          END IF;
        END $$;
        """)
    _create_indexes(PAPER_IDX)
    _create_indexes(PAPER_ITEM_IDX)
    _create_indexes(RESPONSE_EVENT_IDX)
    _create_indexes(ITEM_PARAM_IDX)


def downgrade() -> None:
    _drop_indexes(ITEM_VERSION_IDX)
    _drop_indexes(PAPER_IDX)
    _drop_indexes(PAPER_ITEM_IDX)
    _drop_indexes(RESPONSE_EVENT_IDX)
    _drop_indexes(ITEM_PARAM_IDX)
