"""T-W4-008 台账条目 Pydantic schema.

字段对齐 ADR §4.8 台账要求：任务/模型/prompt 版本/token/成本/关联产物 id。
prompt 本身不入账（防 PII 残留），只存 prompt_hash + prompt_version。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# 台账任务阶段（T-W4-010 归集按阶段分桶）
TaskStage = Literal["draft", "instantiate", "validate", "score", "rescore", "other"]


class LedgerEntry(BaseModel):
    """单次 AI 调用的台账记录（append-only，禁止 UPDATE/DELETE）.

    Attributes:
        call_id: 调用唯一 id（ULID）。
        task_level: L0/L1/L2/L3（路由档位）。
        task_name: 业务任务名（draft_passage / validate / score / rescore 等）。
        task_stage: 成本归集阶段（T-W4-010 按此分桶）。
        provider: 供应商名（deepseek/litellm/stub）。
        model: 实际命中的模型标识。
        prompt_hash: prompt 的 sha256 hex 前 16 位（不存原文，防 PII 残留）。
        prompt_version: prompt 模板版本（可追溯模板演进）。
        token_in: 输入 token 数。
        token_out: 输出 token 数。
        cost_cny: 本次调用人民币成本（按模型单价表算）。
        duration_ms: 调用耗时（毫秒）。
        fallback: 是否走了 fallback 供应商。
        artifact_ref: 关联产物 id（item_revision_id 等，T-W4-010 归集键）。
        created_at: 调用时间戳（UTC）。
        raw_meta: 供应商原始响应的必要子集（禁止含 PII）。
    """

    call_id: str
    task_level: str
    task_name: str
    task_stage: TaskStage = "other"
    provider: str
    model: str
    prompt_hash: str
    prompt_version: str = "v1"
    token_in: int = Field(ge=0)
    token_out: int = Field(ge=0)
    cost_cny: float = Field(ge=0.0)
    duration_ms: float = Field(ge=0.0)
    fallback: bool = False
    artifact_ref: Optional[str] = None
    created_at: datetime
    raw_meta: dict[str, Any] = Field(default_factory=dict)

    def to_jsonl(self) -> str:
        """序列化为单行 JSON（JSONL 格式，append-only 友好）."""
        return self.model_dump_json()


def now_utc() -> datetime:
    """当前 UTC 时间（默认 created_at 工具函数）."""
    return datetime.now(timezone.utc)
