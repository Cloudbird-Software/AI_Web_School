"""T-W4-007 AI 能力总线核心路由.

ai_call() 是全系统唯一的 LLM 调用入口（架构 v2 §4.8）：
- 按 task_level 路由到 policy.yaml 配置的模型；
- L0 直接返回空（不调用任何供应商）；
- L1/L2/L3 转发到注入的 LLMClient（默认 StubClient，生产由 T-W4-009 注入真实适配器）；
- PII 在调用 client 前由 ledger.pii_filter 剥离（T-W4-008 落地，本模块延迟导入）。

依赖方向（不可逆）：
- 007（本包）不 import 008/009/010/011；下游子包单向依赖 007。
- 008 的 pii_filter 通过延迟导入接入；不可用时降级（记 warning，不阻断调用）。

宪法 A5：本包不 import 任何学科包/学段包。
宪法 D7：PII 在总线前剥离；本 router 在调用 client 前调用 pii_filter（若可用）。
宪法 X6 等价约束：核心域非 ai/ 禁止 import openai/deepseek/anthropic
（由 tests/unit/test_ai_router.py 静态扫描强制）。
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.ai.bus.models import (
    AIResult,
    LLMClient,
    StubClient,
    TaskLevel,
)


class PIIFilterError(RuntimeError):
    """D7强制：PII剥离失败禁止外发。"""


_POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"


@lru_cache(maxsize=1)
def _load_policy_cached() -> dict[str, Any]:
    """加载 policy.yaml（进程级缓存）.

    为什么用 lru_cache：避免每次调用都读盘解析 YAML；T-W4-009 验收 #3 的
    「配置热加载」由 reload_policy() 显式失效缓存实现，而非每次读盘
    （每次读盘在热路径上引入 IO 抖动，且测试不易确定性强断言）。
    """
    with _POLICY_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "levels" not in data:
        raise ValueError(f"policy.yaml 结构非法：期望顶层含 levels，实际 {type(data)}")
    return data


def reload_policy() -> None:
    """失效 policy 缓存，下次 ai_call 重新读盘（配置热加载，T-W4-009 验收 #3）."""
    _load_policy_cached.cache_clear()


def get_policy() -> dict[str, Any]:
    """返回当前生效的策略字典（测试与 ledger 消费用）."""
    return _load_policy_cached()


# 供应商客户端注册表：provider 名 → LLMClient
# 生产由 T-W4-009 适配器启动时调用 register_client 注入；007 独立验收时为空
_default_clients: dict[str, LLMClient] = {}


def register_client(provider: str, client: LLMClient) -> None:
    """注册供应商客户端（T-W4-009 适配器启动时调用）.

    为什么不 import 适配器：007 不依赖 009；适配器主动注册，router 被动消费，
    依赖方向正确（009 → 007）。
    """
    _default_clients[provider] = client


def _resolve_client(
    provider: str, injected: Optional[dict[str, LLMClient]]
) -> LLMClient:
    """解析 provider 到客户端：优先注入的，其次注册的，最后桩."""
    if injected and provider in injected:
        return injected[provider]
    if provider in _default_clients:
        return _default_clients[provider]
    return StubClient(provider=provider)


def _sanitize_prompt(prompt: str) -> tuple[str, list[str]]:
    """对 prompt 做 PII 剥离（D7）.

    延迟导入 ledger.pii_filter（T-W4-008）。008 未落地或剥离异常时降级：
    - ImportError：返回原 prompt，记 warning（007 独立验收场景）
    - 其他异常：返回原 prompt，记 warning（剥离失败不阻断调用）

    Returns:
        (剥离后文本, 剥离的 PII 类型列表 + 可能的 warning 标记)
    """
    try:
        from src.core.ai.ledger.pii_filter import strip as pii_strip
    except ImportError:
        return prompt, ["pii_filter_unavailable"]
    try:
        sanitized, stripped = pii_strip(prompt)
        if stripped:
            return sanitized, [f"stripped:{','.join(stripped)}"]
        return sanitized, []
    except Exception as exc:  # noqa: BLE001
        # D7强制：剥离失败必须阻断LLM调用，fail-closed
        raise ValueError(
            "PII filter failed, D7 forbids sending unsanitized prompt to LLM"
        ) from exc


def ai_call(
    task_level: TaskLevel,
    prompt: str,
    context: Optional[dict[str, Any]] = None,
    *,
    clients: Optional[dict[str, LLMClient]] = None,
    bypass_pii_filter: bool = False,
) -> AIResult:
    """AI 总线统一入口（任务卡 T-W4-007 验收 #1/#2）.

    Args:
        task_level: L0/L1/L2/L3，按 policy.yaml 路由。
        prompt: 调用 prompt（可能含 PII，由 pii_filter 在调用 client 前剥离）。
        context: 可选上下文（artifact_ref 等，透传给 ledger，不影响路由）。
        clients: 注入的供应商客户端映射（测试用 mock；生产为空走 register_client
            注册的；都没有则用 StubClient 兜底）。
        bypass_pii_filter: 测试用绕过 PII 剥离。生产禁止传 True（D7 要求总线前剥离）；
            仅用于 007 独立验收时绕过尚未实现的 pii_filter。

    Returns:
        AIResult：统一结构（内容 + token + 耗时 + 模型 + fallback 标志）。

    Raises:
        ValueError: task_level 不在 policy 中。
        Exception: 主客户端失败且无 fallback 配置时，向上抛出。

    Notes:
        - L0：policy.use_ai=false，直接返回空内容 AIResult，不调用任何 client。
        - L1/L2/L3：按 policy.provider 路由到 client.complete()。
        - PII 剥离（D7）：未 bypass 时对 prompt 做剥离；剥离结果记入 result.raw.pii。
        - fallback：配置了 fallback 段且主 client 抛异常时，走 fallback provider，
          返回结果标记 fallback=True，raw.primary_error 记录主异常。
    """
    policy = _load_policy_cached()
    levels = policy.get("levels", {})
    if task_level not in levels:
        raise ValueError(
            f"未知 task_level={task_level}，policy 仅含 {list(levels)}"
        )

    cfg = levels[task_level]
    if not cfg.get("use_ai", False):
        # L0：不用 AI，直接返回空（验收 #1）
        result = AIResult(
            content="",
            model=f"{task_level}-noop",
            token_in=0,
            token_out=0,
            duration_ms=0.0,
        )
        # P0-10：L0也记台账
        try:
            from src.core.ai.ledger.ledger import record_call as ledger_record_call
            ctx = context or {}
            ledger_record_call(
                task_level=task_level,
                task_name=ctx.get("task_name", "ai_call"),
                provider="noop",
                model=result.model,
                prompt=prompt,
                token_in=0,
                token_out=0,
                duration_ms=0.0,
                prompt_version=ctx.get("prompt_version", "v1"),
                task_stage=ctx.get("task_stage", "other"),
                fallback=False,
                artifact_ref=ctx.get("artifact_ref"),
                raw_meta=ctx,
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    # PII 剥离（D7）：在调用 client 前完成
    pii_warnings: list[str] = []
    if bypass_pii_filter:
        sanitized_prompt = prompt
    else:
        sanitized_prompt, pii_warnings = _sanitize_prompt(prompt)

    provider = cfg["provider"]
    model = cfg["model"]
    temperature = cfg.get("temperature", 0.0)
    max_tokens = cfg.get("max_tokens", 1024)

    client = _resolve_client(provider, clients)
    try:
        result = client.complete(
            sanitized_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        # fallback：双供应商预案（验收 #2 异常时走 fallback 供应商）
        fb = cfg.get("fallback")
        if not fb:
            raise
        fb_client = _resolve_client(fb["provider"], clients)
        fb_result = fb_client.complete(
            sanitized_prompt,
            model=fb["model"],
            temperature=fb.get("temperature", temperature),
            max_tokens=fb.get("max_tokens", max_tokens),
        )
        result = dc_replace(
            fb_result,
            fallback=True,
            raw={**fb_result.raw, "primary_error": str(exc)},
        )

    # 附加 PII 剥离与上下文信息（frozen dataclass 用 replace 构造新实例）
    extra_raw: dict[str, Any] = {}
    if pii_warnings:
        extra_raw["pii"] = pii_warnings
    if context:
        extra_raw["context"] = context
    if extra_raw:
        result = dc_replace(result, raw={**result.raw, **extra_raw})

    # P0-10：成功返回前记台账
    try:
        from src.core.ai.ledger.ledger import record_call as ledger_record_call
        ctx = context or {}
        ledger_record_call(
            task_level=task_level,
            task_name=ctx.get("task_name", "ai_call"),
            provider=provider,
            model=result.model,
            prompt=sanitized_prompt,
            token_in=result.token_in,
            token_out=result.token_out,
            duration_ms=result.duration_ms,
            prompt_version=ctx.get("prompt_version", "v1"),
            task_stage=ctx.get("task_stage", "other"),
            fallback=result.fallback,
            artifact_ref=ctx.get("artifact_ref"),
            raw_meta={
                **result.raw,
                **({"pii_warnings": pii_warnings} if pii_warnings else {}),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return result
