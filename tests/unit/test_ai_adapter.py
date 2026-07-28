"""T-W4-009 LLM 供应商适配器单元测试.

验收对照：
  #1 LiteLLMClient/DeepSeekClient.complete 返回统一格式（content/usage/model）
  #2 Fallback：主客户端失败（超时/5xx/配额）后自动切备，标记 fallback
  #3 配置热加载：policy.yaml 变更后 reload_policy 生效
  #4 make accept 全绿；单元测试用 mock 供应商（不消耗真实 API）
  #5 不 import 学科包/学段包

测试不消耗真实 API：注入 _FakeHttp（mock httpx.Client）+ _RecordingClient。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.ai.adapter._openai_compat import OpenAICompatibleClient
from src.core.ai.adapter.deepseek_client import DeepSeekClient
from src.core.ai.adapter.fallback import FallbackClient
from src.core.ai.adapter.litellm_client import LiteLLMClient
from src.core.ai.bus.models import AIResult


# ── Mock 设施 ──────────────────────────────────────────────────────

_DEFAULT_RESPONSE = {
    "id": "chatcmpl-fake-001",
    "model": "deepseek-chat",
    "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class _FakeResponse:
    """模拟 httpx.Response."""

    def __init__(self, data: dict[str, Any], *, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://fake")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=resp
            )


class _FakeHttp:
    """模拟 httpx.Client，记录调用并返回预设响应或抛预设异常."""

    def __init__(
        self,
        *,
        response_data: dict[str, Any] | None = None,
        status: int = 200,
        exc: Exception | None = None,
    ) -> None:
        self._data = response_data if response_data is not None else _DEFAULT_RESPONSE
        self._status = status
        self._exc = exc
        self.post_calls: list[dict[str, Any]] = []

    def post(
        self, url: str, headers: dict[str, str] | None = None, json: dict | None = None
    ) -> _FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._data, status=self._status)


class _RecordingClient:
    """记录调用的 mock 客户端（实现 LLMClient Protocol）."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> AIResult:
        if self._fail:
            raise RuntimeError(f"mock failure for {model}")
        self.calls.append(
            {"prompt": prompt, "model": model, "temperature": temperature,
             "max_tokens": max_tokens}
        )
        return AIResult(
            content=f"ok:{model}",
            model=model,
            token_in=len(prompt),
            token_out=10,
            duration_ms=1.0,
        )


# ── 验收 #1：DeepSeekClient 返回统一格式 ───────────────────────────

def test_deepseek_client_complete_returns_unified_format() -> None:
    """DeepSeekClient.complete 返回 AIResult（content/model/token_in/token_out）."""
    fake_http = _FakeHttp(response_data=_DEFAULT_RESPONSE)
    client = DeepSeekClient(api_key="test-key", http_client=fake_http)
    result = client.complete(
        "你好", model="deepseek-chat", temperature=0.3, max_tokens=100
    )
    assert result.content == "Hello!"
    assert result.model == "deepseek-chat"
    assert result.token_in == 10
    assert result.token_out == 5
    assert result.duration_ms >= 0.0
    assert result.fallback is False
    # 请求格式校验：OpenAI 兼容
    call = fake_http.post_calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["json"]["model"] == "deepseek-chat"
    assert call["json"]["messages"] == [{"role": "user", "content": "你好"}]
    assert call["json"]["temperature"] == 0.3
    assert call["json"]["max_tokens"] == 100


def test_litellm_client_complete_returns_unified_format() -> None:
    """LiteLLMClient.complete 返回统一 AIResult，URL 指向 LiteLLM 网关."""
    fake_http = _FakeHttp(response_data=_DEFAULT_RESPONSE)
    client = LiteLLMClient(api_key="litellm-key", http_client=fake_http)
    result = client.complete(
        "hello", model="gpt-4o", temperature=0.0, max_tokens=200
    )
    assert result.content == "Hello!"
    assert result.model == "deepseek-chat"  # 来自响应 data["model"]
    assert result.token_in == 10
    assert result.token_out == 5
    call = fake_http.post_calls[0]
    assert call["url"] == "http://localhost:4000/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer litellm-key"


def test_deepseek_client_uses_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_key 未显式传入时从环境变量读取（X3：不硬编码）.

    用 monkeypatch 清空环境变量，避免本地 .env 的真实 key 干扰断言
    （X3：key 不进日志/测试输出）。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    fake_http = _FakeHttp()
    client = DeepSeekClient(http_client=fake_http)
    assert client._api_key == "", "环境变量清空后 _api_key 应为空字符串"


def test_deepseek_client_reads_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量设置时被读取（X3：key 从 env 读取，不进代码）."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-loaded-placeholder-key")
    fake_http = _FakeHttp()
    client = DeepSeekClient(http_client=fake_http)
    assert client._api_key == "env-loaded-placeholder-key"


def test_deepseek_client_no_key_raises() -> None:
    """api_key 未配置时 complete 抛 RuntimeError（X3：不进代码/日志）."""
    fake_http = _FakeHttp()
    client = DeepSeekClient(api_key="", http_client=fake_http)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        client.complete("p", model="deepseek-chat", temperature=0.0, max_tokens=10)


# ── 验收 #2：异常类型（超时/5xx/配额）供 fallback 接管 ─────────────

def test_deepseek_client_timeout_raises() -> None:
    """超时抛 httpx.TimeoutException，供上层 fallback 接管."""
    fake_http = _FakeHttp(exc=httpx.TimeoutException("timeout"))
    client = DeepSeekClient(api_key="k", http_client=fake_http)
    with pytest.raises(httpx.TimeoutException):
        client.complete("p", model="deepseek-chat", temperature=0.0, max_tokens=10)


def test_deepseek_client_5xx_raises() -> None:
    """5xx 响应抛 httpx.HTTPStatusError，供上层 fallback 接管."""
    fake_http = _FakeHttp(status=500)
    client = DeepSeekClient(api_key="k", http_client=fake_http)
    with pytest.raises(httpx.HTTPStatusError):
        client.complete("p", model="deepseek-chat", temperature=0.0, max_tokens=10)


def test_deepseek_client_429_quota_raises() -> None:
    """配额耗尽（429）抛 HTTPStatusError，供上层 fallback 接管."""
    fake_http = _FakeHttp(status=429)
    client = DeepSeekClient(api_key="k", http_client=fake_http)
    with pytest.raises(httpx.HTTPStatusError):
        client.complete("p", model="deepseek-chat", temperature=0.0, max_tokens=10)


# ── 验收 #2：FallbackClient 主备切换 ───────────────────────────────

def test_fallback_client_primary_success() -> None:
    """主客户端成功时返回主结果，fallback=False."""
    primary = _RecordingClient()
    secondary = _RecordingClient()
    client = FallbackClient(primary, secondary)
    result = client.complete("p", model="m", temperature=0.0, max_tokens=10)
    assert result.fallback is False
    assert result.content == "ok:m"
    assert primary.calls and not secondary.calls, "主成功不应调备"


def test_fallback_client_primary_failure_switches() -> None:
    """主客户端失败时自动切备，标记 fallback=True."""
    primary = _RecordingClient(fail=True)
    secondary = _RecordingClient()
    client = FallbackClient(primary, secondary)
    result = client.complete("p", model="primary-m", temperature=0.0, max_tokens=10)
    assert result.fallback is True
    assert "primary_error" in result.raw
    assert "mock failure" in result.raw["primary_error"]
    assert secondary.calls, "备客户端应被调用"


def test_fallback_client_secondary_model_override() -> None:
    """secondary_model 指定时，备客户端用指定模型（主备模型不同场景）."""
    primary = _RecordingClient(fail=True)
    secondary = _RecordingClient()
    client = FallbackClient(primary, secondary, secondary_model="gpt-4o")
    result = client.complete("p", model="deepseek-reasoner", temperature=0.0, max_tokens=10)
    assert result.fallback is True
    assert secondary.calls[0]["model"] == "gpt-4o", "备客户端应用 secondary_model"


def test_fallback_client_with_real_adapters() -> None:
    """FallbackClient 组合 DeepSeekClient + LiteLLMClient（主备互备）.

    主 DeepSeek 超时，备 LiteLLM 接管——验证适配器层 fallback 与真实客户端协作。
    """
    deepseek_http = _FakeHttp(exc=httpx.TimeoutException("timeout"))
    litellm_http = _FakeHttp(response_data=_DEFAULT_RESPONSE)
    primary = DeepSeekClient(api_key="ds-key", http_client=deepseek_http)
    secondary = LiteLLMClient(api_key="ll-key", http_client=litellm_http)
    client = FallbackClient(primary, secondary, secondary_model="gpt-4o")
    result = client.complete("p", model="deepseek-reasoner", temperature=0.0, max_tokens=10)
    assert result.fallback is True
    assert result.content == "Hello!"
    assert "timeout" in result.raw["primary_error"]


# ── 验收 #3：配置热加载 ───────────────────────────────────────────

def test_policy_hot_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """policy.yaml 变更后 reload_policy 生效，ai_call 用新配置（验收 #3）."""
    from src.core.ai.bus import router as router_mod
    from src.core.ai.bus.router import ai_call, reload_policy

    # 写临时 policy，L1 model 改为 hot-reloaded-model
    tmp_policy = tmp_path / "policy_test.yaml"
    tmp_policy.write_text(
        """
contract_version: "1.0.0"
levels:
  L0:
    use_ai: false
    description: "test"
  L1:
    use_ai: true
    provider: "deepseek"
    model: "hot-reloaded-model"
    temperature: 0.5
    max_tokens: 2048
    description: "test L1"
  L2:
    use_ai: true
    provider: "deepseek"
    model: "deepseek-reasoner"
    temperature: 0.2
    max_tokens: 4096
    description: "test L2"
  L3:
    use_ai: true
    provider: "litellm"
    model: "gpt-4o"
    temperature: 0.0
    max_tokens: 8192
    fallback:
      provider: "deepseek"
      model: "deepseek-reasoner"
    description: "test L3"
""",
        encoding="utf-8",
    )
    # 替换 policy 路径并清缓存
    monkeypatch.setattr(router_mod, "_POLICY_PATH", tmp_policy)
    reload_policy()

    try:
        client = _RecordingClient()
        result = ai_call(
            "L1", "p", clients={"deepseek": client}, bypass_pii_filter=True
        )
        assert client.calls[0]["model"] == "hot-reloaded-model"
        assert client.calls[0]["temperature"] == 0.5
        assert client.calls[0]["max_tokens"] == 2048
    finally:
        # 恢复真实 policy 缓存（避免污染后续测试）
        reload_policy()


# ── register_adapters 注册 ─────────────────────────────────────────

def test_register_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_adapters 注册 deepseek/litellm 客户端到 router.

    用 monkeypatch 替换 register_client 捕获注册调用，避免真实 key 读取
    （register_adapters 会实例化 DeepSeekClient/LiteLLMClient，读环境变量，
    但不会因 key 为空而失败——key 在 complete 时才校验）。
    """
    from src.core.ai.adapter import register_adapters
    from src.core.ai.bus import router as router_mod

    registered: dict[str, object] = {}
    monkeypatch.setattr(
        router_mod, "register_client", lambda provider, client: registered.update({provider: client})
    )
    register_adapters()
    assert "deepseek" in registered
    assert "litellm" in registered
    assert isinstance(registered["deepseek"], DeepSeekClient)
    assert isinstance(registered["litellm"], LiteLLMClient)


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_adapter() -> None:
    """src/core/ai/adapter/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    adapter_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "adapter"
    )
    assert adapter_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(adapter_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(adapter_dir)))
    assert not violations, f"ai/adapter 存在学科包 import（违反 A5）：{violations}"


# ── OpenAICompatibleClient 子类契约 ────────────────────────────────

def test_subclass_attributes() -> None:
    """DeepSeek/LiteLLM 子类正确设置 BASE_URL 与 ENV_KEY_NAME."""
    assert DeepSeekClient.BASE_URL == "https://api.deepseek.com"
    assert DeepSeekClient.ENV_KEY_NAME == "DEEPSEEK_API_KEY"
    assert LiteLLMClient.BASE_URL == "http://localhost:4000"
    assert LiteLLMClient.ENV_KEY_NAME == "LITELLM_MASTER_KEY"


def test_openai_compat_is_llmclient_protocol() -> None:
    """OpenAICompatibleClient 满足 LLMClient Protocol（鸭子类型）."""
    # Protocol 是结构化类型，运行时 isinstance 检查需 typing.runtime_checkable
    # 这里验证方法签名存在即可
    fake_http = _FakeHttp()
    client = DeepSeekClient(api_key="k", http_client=fake_http)
    assert callable(client.complete)
    result = client.complete("p", model="m", temperature=0.0, max_tokens=10)
    assert isinstance(result, AIResult)
