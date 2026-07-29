"""AI 语篇起草器（T-W4-013）.

架构 v2 §4.1 / §4.8：按命题方向经 S2 AI 总线 L2 档生成语篇草稿；
草稿标记 AI 生成来源（模型/prompt_hash/token），待教研改写定稿（T-W4-016）。

调用链：
    PromptDirection → validate_prompt_direction → direction_to_prompt
    → ai_call("L2", prompt, ...) → record_call(台账) → PassageDraft

宪法 D7：ai_call 在调用 client 前由总线剥离 PII（ledger.pii_filter）；
本模块不直接处理 PII，prompt 中若含 PII 由总线兜底剥离。
宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from src.core.ai.bus.models import AIResult, LLMClient
from src.core.ai.bus.router import ai_call, get_policy
from src.core.ai.ledger.ledger import Ledger, get_default_ledger
from src.core.content.passage_schema import (
    PromptDirection,
    direction_to_prompt,
    validate_prompt_direction,
)

# L2 档：强模型生产语篇起草（架构 v4.8）
_DRAFT_TASK_LEVEL = "L2"
_DRAFT_TASK_NAME = "draft_passage"
_DRAFT_TASK_STAGE = "draft"
_PROMPT_VERSION = "v1"


def _hash_prompt(prompt: str) -> str:
    """prompt 的 sha256 hex 前 16 位（与 ledger.hash_prompt 一致，避免循环 import）."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class GenerationMeta:
    """AI 生成来源元数据（落 Passage 谱系，可追溯生成模型与台账记录）.

    Attributes:
        model: 实际命中的模型标识（如 deepseek-reasoner）。
        prompt_hash: prompt 的 sha256 前 16 位（不存原文，防 PII 残留）。
        prompt_version: prompt 模板版本。
        token_in: 输入 token 数。
        token_out: 输出 token 数。
        duration_ms: 调用耗时（毫秒）。
        fallback: 是否走了 fallback 供应商。
        call_id: 台账记录 id（ULID），None 表示未入台账（如测试未注入 ledger）。
        task_level: 路由档位（L2）。
    """

    model: str
    prompt_hash: str
    prompt_version: str
    token_in: int
    token_out: int
    duration_ms: float
    fallback: bool
    call_id: Optional[str]
    task_level: str = _DRAFT_TASK_LEVEL


@dataclass(frozen=True)
class PassageDraft:
    """AI 起草的语篇草稿.

    - body：语篇正文（AI 生成，待教研改写定稿）。
    - prompt：渲染后的 prompt 文本（供定稿接口回溯，不入库 PII 已由总线剥离）。
    - generation_meta：AI 生成来源元数据（模型/token/台账 call_id）。
    - direction：命题方向（草稿对应的输入规约，落 Passage.kp_refs 等字段）。
    """

    body: str
    prompt: str
    generation_meta: GenerationMeta
    direction: PromptDirection


def _resolve_provider(task_level: str) -> str:
    """从 policy.yaml 取 provider 名（台账 record_call 需要）."""
    policy = get_policy()
    cfg = policy.get("levels", {}).get(task_level, {})
    return cfg.get("provider", "unknown")


def generate_passage(
    direction: PromptDirection,
    grade_band: Optional[str] = None,
    *,
    clients: Optional[dict[str, LLMClient]] = None,
    ledger: Optional[Ledger] = None,
    artifact_ref: Optional[str] = None,
    bypass_pii_filter: bool = False,
) -> PassageDraft:
    """按命题方向经 AI 总线 L2 档起草语篇（任务卡 T-W4-013 验收 #1/#3）.

    Args:
        direction: 命题方向（知识点×体裁×难度×学段×学科）。
        grade_band: 学段（兼容任务卡签名；优先取 direction.grade_band，本参数
            仅作显式覆盖校验，与 direction 不一致时报错）。
        clients: 注入的供应商客户端映射（测试用 mock；生产为空走注册的适配器）。
        ledger: 台账实例（None 时用默认实例 get_default_ledger；测试注入 tmp_path 实例）。
        artifact_ref: 关联产物 id（item_revision_id，台账归集键，T-W4-010 消费）。
        bypass_pii_filter: 测试用绕过 PII 剥离。生产禁止传 True（D7）。

    Returns:
        PassageDraft：含正文 + 生成元数据 + 命题方向。

    Raises:
        ValueError: 命题方向校验失败（知识点/难度/学段）或 grade_band 不一致。

    Notes:
        - 经 S2 AI 总线（T-W4-007）：调用 ai_call("L2", ...)，路由到 policy 配置的强模型。
        - 调用记录入台账（T-W4-008）：record_call 写 JSONL，返回 call_id 落 GenerationMeta。
        - PII 剥离（D7）：ai_call 在调用 client 前经 ledger.pii_filter 剥离 prompt。
    """
    # 1. 校验命题方向
    if grade_band is not None and grade_band != direction.grade_band:
        raise ValueError(
            f"grade_band 不一致：参数={grade_band!r}，"
            f"direction={direction.grade_band!r}"
        )
    errors = validate_prompt_direction(direction)
    if errors:
        raise ValueError(
            "命题方向校验失败：" + "; ".join(errors)
        )

    # 2. 渲染 prompt
    prompt = direction_to_prompt(direction)
    prompt_hash = _hash_prompt(prompt)

    # 3. 经 AI 总线 L2 档调用（验收 #3：经 S2 总线）
    result: AIResult = ai_call(
        _DRAFT_TASK_LEVEL,
        prompt,
        context={"artifact_ref": artifact_ref, "task_name": _DRAFT_TASK_NAME},
        clients=clients,
        bypass_pii_filter=bypass_pii_filter,
    )

    # 4. 入台账（验收 #3：调用记录入台账 T-W4-008）
    call_id: Optional[str] = None
    lg = ledger if ledger is not None else get_default_ledger()
    if lg is not None:
        call_id = lg.record_call(
            task_level=_DRAFT_TASK_LEVEL,
            task_name=_DRAFT_TASK_NAME,
            task_stage=_DRAFT_TASK_STAGE,
            provider=_resolve_provider(_DRAFT_TASK_LEVEL),
            model=result.model,
            prompt=prompt,
            token_in=result.token_in,
            token_out=result.token_out,
            duration_ms=result.duration_ms,
            prompt_version=_PROMPT_VERSION,
            fallback=result.fallback,
            artifact_ref=artifact_ref,
            raw_meta=dict(result.raw),
        )

    meta = GenerationMeta(
        model=result.model,
        prompt_hash=prompt_hash,
        prompt_version=_PROMPT_VERSION,
        token_in=result.token_in,
        token_out=result.token_out,
        duration_ms=result.duration_ms,
        fallback=result.fallback,
        call_id=call_id,
    )

    return PassageDraft(
        body=result.content,
        prompt=prompt,
        generation_meta=meta,
        direction=direction,
    )


__all__ = [
    "GenerationMeta",
    "PassageDraft",
    "generate_passage",
]
