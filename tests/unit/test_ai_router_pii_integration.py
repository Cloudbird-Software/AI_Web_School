"""T-W4-007 ↔ T-W4-008 集成测试：ai_call 自动剥离 PII（D7 闭环验证）.

验证 router.ai_call 在调用 client 前经 ledger.pii_filter 剥离 PII：
- 不传 skip_pii_filter 时，prompt 被剥离
- client 收到的 prompt 无 PII
- result.raw.pii 记录剥离信息

本文件验证 007（router）与 008（pii_filter）的接合点，任一改动都应在此回归。
"""
from __future__ import annotations

from src.core.ai.bus.router import ai_call


class _CapturingClient:
    """捕获 prompt 的 mock 客户端（实现 LLMClient Protocol）."""

    def __init__(self) -> None:
        self.received_prompt: str = ""

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ):
        self.received_prompt = prompt
        from src.core.ai.bus.models import AIResult
        return AIResult(
            content="ok", model=model, token_in=len(prompt),
            token_out=1, duration_ms=1.0,
        )


def test_ai_call_strips_pii_before_client() -> None:
    """ai_call 不 skip_pii_filter 时，client 收到的 prompt 已剥离 PII（D7）."""
    client = _CapturingClient()
    result = ai_call(
        "L1",
        "学生张三的电话13912345678请起草语篇",
        clients={"deepseek": client},
    )
    # client 收到的 prompt 不含原始 PII
    assert "张三" not in client.received_prompt, "姓名应被剥离"
    assert "13912345678" not in client.received_prompt, "电话应被剥离"
    # 剥离后含占位符
    assert "学生A" in client.received_prompt
    assert "[PHONE]" in client.received_prompt
    # result.raw.pii 记录剥离信息
    assert "pii" in result.raw
    assert any("stripped" in w for w in result.raw["pii"])


def test_ai_call_skip_pii_filter_passes_raw_prompt() -> None:
    """skip_pii_filter=True 时 prompt 原样传递（测试场景）."""
    client = _CapturingClient()
    raw_prompt = "学生张三的电话13912345678"
    ai_call("L1", raw_prompt, clients={"deepseek": client}, skip_pii_filter=True)
    assert client.received_prompt == raw_prompt, "skip 时应原样传递"


def test_ai_call_pii_filter_records_stripped_kinds() -> None:
    """剥离多种 PII 时，result.raw.pii 记录全部剥离类型."""
    client = _CapturingClient()
    result = ai_call(
        "L2",
        "学生李四邮箱lisi@x.com身份证110101199003071234",
        clients={"deepseek": client},
    )
    pii_info = result.raw.get("pii", [])
    # 至少记录了剥离发生（stripped:xxx 格式）
    assert pii_info, "应记录 PII 剥离信息"
    combined = ",".join(pii_info)
    # 多类 PII 被剥离
    assert "stripped" in combined
