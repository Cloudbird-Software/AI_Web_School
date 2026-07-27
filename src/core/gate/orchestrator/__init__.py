"""T-W2-010 门编排引擎子包.

唯一对外导出：run_gate / GateOutcome / GateRunRecord。
具体实现在 orchestrator.py 中。
"""
from src.core.gate.orchestrator.orchestrator import (
    GateOutcome,
    GateRunRecord,
    run_gate,
)

__all__ = ["GateOutcome", "GateRunRecord", "run_gate"]
