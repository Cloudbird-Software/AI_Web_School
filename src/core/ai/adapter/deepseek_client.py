"""T-W4-009 DeepSeek 直连客户端.

DeepSeek API 暴露 OpenAI 兼容格式，直连 https://api.deepseek.com/chat/completions。
已有 key（DEEPSEEK_API_KEY，.env 注释配置）。作为 L1/L2 主供应商。

宪法 X3：DEEPSEEK_API_KEY 从环境变量读取，不硬编码。
宪法 A5：本包不 import 学科包/学段包。
"""
from __future__ import annotations

from src.core.ai.adapter._openai_compat import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek 直连客户端（OpenAI 兼容格式）.

    生产配置：在 .env 设置 DEEPSEEK_API_KEY；应用启动调用 register_adapters()
    自动注册到 AI 总线。
    """

    BASE_URL = "https://api.deepseek.com"
    ENV_KEY_NAME = "DEEPSEEK_API_KEY"
