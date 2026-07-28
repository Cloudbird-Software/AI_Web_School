"""T-W4-007 AI 总线核心路由包.

对外暴露 ai_call() —— 全系统唯一的 LLM 调用入口（架构 v2 §4.8）。
"""
from src.core.ai.bus.models import AIResult, LLMClient, StubClient, TaskLevel
from src.core.ai.bus.router import ai_call, get_policy, register_client, reload_policy

__all__ = [
    "AIResult",
    "LLMClient",
    "StubClient",
    "TaskLevel",
    "ai_call",
    "get_policy",
    "register_client",
    "reload_policy",
]
