"""T-W4-007 AI 总线核心数据模型.

定义任务分级（L0–L3）与统一调用结果契约（架构 v2 §4.8）。

- L0 不用 AI / L1 弱模型初筛 / L2 强模型生产与裁判 / L3 双模型或 AI+人工复核
- AIResult 为所有供应商/适配器的统一返回结构，router 不做二次包装
- LLMClient 为供应商客户端协议（T-W4-009 适配器实现此协议）

为什么用 Protocol 而非 ABC：核心域不强制继承基类，适配器只需鸭子类型实现；
Protocol 在运行时可被任意实现类满足，避免 007→009 反向依赖（007 不 import 009）。

宪法 A5：本包禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# 任务分级（架构 v2 §4.8 原文）
# L0 不用 AI / L1 弱模型初筛 / L2 强模型生产与裁判 / L3 双模型或 AI+人工复核
TaskLevel = Literal["L0", "L1", "L2", "L3"]


@dataclass(frozen=True)
class AIResult:
    """统一调用结果（任务卡 T-W4-007 验收 #2）.

    所有供应商/适配器返回此结构。字段对齐 ADR §4.8 台账要求（model/token/时长），
    便于 ledger（T-W4-008）直接消费、cost（T-W4-010）归集。

    Attributes:
        content: 模型输出文本（L0 为空字符串）。
        model: 实际命中的模型标识（如 deepseek-chat / gpt-4o）。
        token_in: 输入 token 数（prompt 计费依据）。
        token_out: 输出 token 数。
        duration_ms: 调用耗时（毫秒）。
        fallback: 是否走了 fallback 供应商（主供应商失败时 True）。
        raw: 供应商原始响应的必要子集，供审计与排错；禁止含 PII。
    """

    content: str
    model: str
    token_in: int
    token_out: int
    duration_ms: float
    fallback: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """LLM 供应商客户端统一契约（T-W4-009 适配器实现此协议）.

    实现方负责：
    - 记录 token 用量与耗时；
    - 异常时由 fallback 客户端接管（T-W4-009 fallback.py 编排）；
    - 不在结果中泄漏 PII（PII 剥离在 router 入口完成，D7）。
    """

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResult:
        """同步完成一次 LLM 调用，返回统一 AIResult."""
        ...


class StubClient:
    """默认桩客户端：无外部依赖，用于 007 独立验收与未配置供应商时的兜底.

    为什么需要桩：
    - 007 的 owner_module=src/core/ai/bus，不能 import adapter（009）；
    - 真实供应商 key 在 CI 中不可用（X3 禁止密钥进仓库/日志）。
    桩客户端让 router 在无供应商注入时仍可工作，并记录被调用参数，便于测试断言路由命中。

    生产环境应注入 T-W4-009 的真实适配器替换桩（register_client）。
    """

    def __init__(self, provider: str = "stub") -> None:
        self._provider = provider
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResult:
        self.calls.append(
            {"model": model, "temperature": temperature, "max_tokens": max_tokens}
        )
        return AIResult(
            content=f"[stub:{self._provider}] model={model}",
            model=model,
            token_in=len(prompt),
            token_out=0,
            duration_ms=0.0,
        )
