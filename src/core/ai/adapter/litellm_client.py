"""T-W4-009 LiteLLM 兼容层客户端.

LiteLLM proxy 网关统一 OpenAI 格式（多供应商分发），默认 http://localhost:4000。
作为 L3 主供应商与 L1/L2 的 fallback 备份（与 DeepSeek 互备）。

为什么用 LiteLLM 网关而非 openai SDK 直连：
- LiteLLM 网关统一管理多供应商 key（OpenAI/Anthropic/DeepSeek），应用只持
  LITELLM_MASTER_KEY 一个角色虚拟 key（OPC §6.6 安全实践）；
- 切换供应商只改网关配置，不需改应用代码；
- 网关层统一计费/限流/审计。

宪法 X3：LITELLM_MASTER_KEY 从环境变量读取，不硬编码。
宪法 A5：本包不 import 学科包/学段包。
"""
from __future__ import annotations

from src.core.ai.adapter._openai_compat import OpenAICompatibleClient


class LiteLLMClient(OpenAICompatibleClient):
    """LiteLLM 网关客户端（OpenAI 兼容格式）.

    生产配置：部署 LiteLLM proxy，在 .env 设置 LITELLM_MASTER_KEY 与
    LITELLM_BASE_URL（可选，默认 http://localhost:4000）。
    """

    BASE_URL = "http://localhost:4000"
    ENV_KEY_NAME = "LITELLM_MASTER_KEY"
