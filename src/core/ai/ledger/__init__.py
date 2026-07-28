"""T-W4-008 LLM 调用台账 + PII 剥离中间件.

- ledger.py：append-only 台账（JSONL），按 item_revision 归集单题 AI 成本
- pii_filter.py：LLM/TTS 调用前剥离 PII（D7）
- schemas.py：台账条目 Pydantic schema

宪法 D7：学生直标识只在保险库 schema；LLM/TTS 调用前必须剥离 PII。
宪法 A5：本包不 import 任何学科包/学段包。
"""
from src.core.ai.ledger.ledger import (
    Ledger,
    get_default_ledger,
    query_by_artifact,
    record_call,
    set_default_ledger,
)
from src.core.ai.ledger.pii_filter import strip as pii_strip
from src.core.ai.ledger.schemas import LedgerEntry

__all__ = [
    "Ledger",
    "LedgerEntry",
    "get_default_ledger",
    "pii_strip",
    "query_by_artifact",
    "record_call",
    "set_default_ledger",
]
