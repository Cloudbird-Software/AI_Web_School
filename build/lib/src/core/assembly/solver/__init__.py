"""§4.4 组卷求解器子包：在线启发式 + 离线 CP-SAT（T-W4-028）.

架构 v2 §4.4「求解」段双出口共存于本子包：
- ``heuristic``：在线练习/诊断出口的候选预算装填启发式（毫秒级，
  T-W3-assembly S1/S2）。确定性 = 快照 id + Profile 版本 + 种子。
- ``cpsat_solver``：离线测量卷出口的 CP-SAT 求解器（秒级，T-W4-028），
  主入口 ``solve(spec_table, candidate_pool, seed=0)``。
- ``constraints``：CP-SAT 候选/冲突/不可行报告数据模型。

为什么 solver 由模块升级为子包：原 ``src/core/assembly/solver.py``（启发式）
与 T-W4-028 交付物 ``src/core/assembly/solver/cpsat_solver.py`` 同名冲突——
Python 包遮蔽同名模块。标准解法：启发式迁入子包为 ``heuristic.py``，
本 ``__init__`` 同时重导出启发式 API（W3 既有 ``from src.core.assembly.solver
import assemble`` 零改动兼容）与 CP-SAT API。

宪法 A5/A7：本子包不 import 任何学科包/学段包（学科零特判）；
candidate_pool 由调用方从 serving 视图加载后包装为 MeasurementCandidate 传入。
"""
# 启发式 API（W3 既有契约，重导出以保持 from src.core.assembly.solver import assemble 兼容）
from src.core.assembly.solver.heuristic import (
    AssemblyResult,
    ConflictReason,
    ConflictReport,
    InfeasibleError,
    assemble,
)
# CP-SAT API（T-W4-028）
from src.core.assembly.solver.constraints import (
    CpSatConflict,
    CpSatInfeasible,
    MeasurementCandidate,
    measurement_candidate_from_serving_row,
)
from src.core.assembly.solver.cpsat_solver import solve

__all__ = [
    # 启发式（W3）
    "assemble",
    "AssemblyResult",
    "InfeasibleError",
    "ConflictReport",
    "ConflictReason",
    # CP-SAT（W4-028）
    "MeasurementCandidate",
    "CpSatConflict",
    "CpSatInfeasible",
    "solve",
    "measurement_candidate_from_serving_row",
]
