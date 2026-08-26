"""T-W5-014 ai_call_ledger 台账表（D10 AI 可回放，W5-R Go 重锚定）.

镜像 db/migrations/0026_ai_call_ledger.up.sql（语义双源纪律见该文件头注）。

宪法 D10：所有生成式调用必须经统一 AI 总线并落台账——模型标识、模型版本、
prompt 版本、token 与成本、关联产物 id 缺一不可；PII 剥离失败 fail-closed。
此前台账只存在于冻结实现的 JSONL 文件（src/core/ai/ledger/），且 record_call
不在 ai_call 内统一调用（语篇生成/量规评分有台账盲区）。本迁移以 DB 表承载：
- status 三态 ok/failed/rejected——拒绝也是账面事实（X12：合规失败不留暗数）；
- prompt_hash 只存 sha256 前 16 hex（原文与剥离前文本皆不入库，防 PII 残留）；
- reason 为固定短码（总线哨兵常量），禁入底层 error 文本与原文片段——凭证与
  PII 不得进日志/异常消息/台账（验收 #1/#3）。
- append-only 物理强制复用 0005 统一的 raise_append_only_error()（同 0017
  score_run 的处置；零新函数，不 CREATE OR REPLACE 重定义函数体）。

cost_cny 用 DOUBLE PRECISION 与冻结 JSONL float 单价口径逐位一致（round 到
1e-6），避免引入第二套十进制舍入语义。task_level 可空：NULL=未完成路由即被
前置门拒绝（PG 对 NULL 恒放行 CHECK）。

迁移可逆性（make migrate-go-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 ai_call_ledger + 索引 + append-only 触发器."""
    op.create_table(
        "ai_call_ledger",
        # call_id：应用层生成的调用唯一 id（crypto/rand hex，对齐 ULID 位阶）
        sa.Column("call_id", sa.Text(), primary_key=True),
        # 任务类型四值域（LLM/TTS/嵌入/ASR，ADR 附录 A）
        sa.Column("modality", sa.Text(), nullable=False),
        # 路由档位 L0–L3；NULL=尚未完成路由即被前置门拒绝
        sa.Column("task_level", sa.Text(), nullable=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        # sha256(sanitized prompt) 前 16 hex；原文永不入账（防 PII 残留）
        sa.Column("prompt_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("token_in", sa.Integer(), nullable=False),
        sa.Column("token_out", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.DOUBLE_PRECISION(), nullable=False),
        sa.Column("duration_ms", sa.DOUBLE_PRECISION(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # 失败短码（固定枚举词）；禁止携带底层 error 文本/原文片段
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        # 关联产物 id（item_revision_id 等）：单题全生命周期 AI 成本归集键
        sa.Column("artifact_ref", sa.Text(), nullable=True),
        # 注册的出站目标名（allowlist 键）
        sa.Column("caller_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "modality IN ('llm', 'tts', 'embedding', 'asr')",
            name="ck_ai_call_ledger_modality_domain",
        ),
        sa.CheckConstraint(
            "task_level IN ('L0', 'L1', 'L2', 'L3')",
            name="ck_ai_call_ledger_task_level_domain",
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'failed', 'rejected')",
            name="ck_ai_call_ledger_status_domain",
        ),
    )
    op.create_index(
        "ix_ai_call_ledger_artifact", "ai_call_ledger", ["artifact_ref"]
    )
    op.create_index(
        "ix_ai_call_ledger_task",
        "ai_call_ledger",
        ["modality", "task_level", "task_name"],
    )
    op.create_index(
        "ix_ai_call_ledger_created_at", "ai_call_ledger", ["created_at"]
    )
    op.execute(
        """CREATE TRIGGER trg_ai_call_ledger_append_only
    BEFORE UPDATE OR DELETE ON ai_call_ledger
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();"""
    )


def downgrade() -> None:
    """成对回滚：精确 DROP 本迁移引入的触发器/索引/表."""
    binding = op.get_bind()
    binding.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_ai_call_ledger_append_only ON ai_call_ledger"
        )
    )
    op.drop_index("ix_ai_call_ledger_created_at", table_name="ai_call_ledger")
    op.drop_index("ix_ai_call_ledger_task", table_name="ai_call_ledger")
    op.drop_index("ix_ai_call_ledger_artifact", table_name="ai_call_ledger")
    op.drop_table("ai_call_ledger")
