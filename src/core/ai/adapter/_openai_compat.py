"""T-W4-009 OpenAI 兼容格式客户端基类（共享逻辑）.

DeepSeek 与 LiteLLM 网关都暴露 OpenAI 兼容的 /chat/completions 接口，
共享 HTTP 调用与响应解析逻辑；子类只覆盖 BASE_URL 与 ENV_KEY_NAME。

为什么用 httpx 同步 Client 而非 openai SDK：
- 避免引入 openai 重依赖（X8：新依赖须评审 + 锁定）；
- httpx 已是项目依赖（T-W2-039 API 单测 TestClient 底层）；
- OpenAI 兼容接口足够简单，直连 HTTP 最直接；
- 同步 complete 与 LLMClient Protocol（007 定义）一致；生产演进 async 时
  可加 AsyncLLMClient Protocol，不影响现有契约。

宪法 A5：本包不 import 学科包/学段包。
宪法 X3：api_key 从环境变量读取，不硬编码、不进日志/prompt。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

from src.core.ai.bus.models import AIResult


class OpenAICompatibleClient:
    """OpenAI 兼容格式客户端基类（实现 LLMClient Protocol）.

    子类设置类属性 BASE_URL 与 ENV_KEY_NAME；实例化时可覆盖 api_key/base_url。
    测试通过 http_client 参数注入 mock，不消耗真实 API。
    """

    BASE_URL: str = ""
    ENV_KEY_NAME: str = ""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        # api_key 优先用显式传入，其次环境变量（X3：不硬编码）
        self._api_key = (
            api_key if api_key is not None else os.environ.get(self.ENV_KEY_NAME, "")
        )
        self._base_url = base_url or self.BASE_URL
        self._timeout = timeout
        # http_client 懒建：None 时首次 complete 才创建（测试注入 mock 避免真实连接）
        self._http = http_client

    def _get_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AIResult:
        """调用 /chat/completions，返回统一 AIResult.

        Raises:
            RuntimeError: api_key 未配置（X3：key 不进代码/日志）。
            httpx.TimeoutException: 请求超时（上层 fallback 接管）。
            httpx.HTTPStatusError: 4xx/5xx（含配额 429，上层 fallback 接管）。
        """
        if not self._api_key:
            raise RuntimeError(
                f"{self.ENV_KEY_NAME} 未配置（X3：key 从环境变量读取，不进代码/日志）"
            )
        start = time.monotonic()
        resp = self._get_http().post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        duration_ms = (time.monotonic() - start) * 1000.0
        usage = data.get("usage", {})
        return AIResult(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", model),
            token_in=int(usage.get("prompt_tokens", 0)),
            token_out=int(usage.get("completion_tokens", 0)),
            duration_ms=duration_ms,
            raw={"provider_response_id": data.get("id", "")},
        )
