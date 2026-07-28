"""T-W4-009 Fallback 客户端：主备自动切换.

包装主客户端 + 备客户端，主失败（超时/5xx/配额）时自动切备，返回结果标记
fallback=True。供适配器层组合使用（如 DeepSeekClient + LiteLLMClient 互备）。

与 router.ai_call 的 L3 fallback 区别：
- router 的 fallback 按 policy.yaml 的 fallback 段切换 provider（路由级）；
- FallbackClient 在单个 provider 内部组合两个客户端（客户端级），更细粒度。
- 两者可叠加：router 路由到 FallbackClient，FallbackClient 内部再主备切换。

宪法 A5：本包不 import 学科包/学段包。
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import Optional

from src.core.ai.bus.models import AIResult, LLMClient


class FallbackClient:
    """主备客户端组合：主失败自动切备，标记 fallback=True.

    实现 LLMClient Protocol，可像普通客户端一样注入 router.ai_call。

    Args:
        primary: 主客户端（正常路径）。
        secondary: 备客户端（主失败时接管）。
        secondary_model: 备客户端使用的模型标识（None 时沿用 primary 的 model）。
            用于主备模型不同的场景（如主 deepseek-reasoner，备 gpt-4o）。
    """

    def __init__(
        self,
        primary: LLMClient,
        secondary: LLMClient,
        *,
        secondary_model: Optional[str] = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._secondary_model = secondary_model

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResult:
        """主客户端失败时自动切备，返回结果标记 fallback=True.

        主客户端抛任意异常（超时/5xx/配额/网络错误）即触发 fallback；
        不吞异常类型，原异常信息记入 result.raw.primary_error 供审计。
        """
        try:
            return self._primary.complete(
                prompt,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            fb_model = self._secondary_model or model
            fb_result = self._secondary.complete(
                prompt,
                model=fb_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return dc_replace(
                fb_result,
                fallback=True,
                raw={**fb_result.raw, "primary_error": str(exc)},
            )
