"""T-W4-009 LLM 供应商适配层.

LiteLLM 兼容客户端（统一 OpenAI 格式网关）与 DeepSeek 直连客户端，
两者互为备份。配置驱动（环境变量 + policy.yaml），fallback 机制在主供应商
失败时自动切换。

适配器实现 src.core.ai.bus.models.LLMClient Protocol，启动时调用
register_adapters() 注册到 AI 总线（router）。

宪法 A5：本包不 import 任何学科包/学段包。
宪法 X3：api_key 从环境变量读取，不硬编码、不进日志、不进 prompt。
"""
from src.core.ai.adapter.deepseek_client import DeepSeekClient
from src.core.ai.adapter.fallback import FallbackClient
from src.core.ai.adapter.litellm_client import LiteLLMClient

__all__ = [
    "DeepSeekClient",
    "FallbackClient",
    "LiteLLMClient",
    "register_adapters",
]


def register_adapters() -> None:
    """应用启动时调用：注册适配器到 AI 总线（router）.

    为什么不在 import 时自动注册：避免 import 副作用（测试导入模块时不应
    触发真实客户端创建与 key 读取）。由应用入口显式调用。

    注册后，router.ai_call 默认使用这些适配器；测试可注入 mock 覆盖。
    """
    from src.core.ai.bus.router import register_client

    register_client("deepseek", DeepSeekClient())
    register_client("litellm", LiteLLMClient())
