"""T-W1-006 校验域·门证书账（gate 三表）.

按 specs/constitution.md#D1-D2 与 specs/contracts/db/item-model.md#4.3 落地
「校验签发账」（D1 三本账之三）：
- gate_certificate：门证书（签发后只增不改），发布事务的合法凭证。
- gate_run：一次校验运行记录（策略版本/验证器版本/判定/证据/成本）。
- gate_verdict：每个验证器的判定结果明细。

宪法 D1 三本账只增不改：append-only 由迁移 0004 的 BEFORE UPDATE OR DELETE
触发器物理强制。ORM 层仅暴露 INSERT 与 SELECT 路径——不提供 update/delete 方法，
任何修改意图都应在应用层被拒绝（触发器是兜底）。

宪法 A5：核心域零学科特判，本包不 import 任何学科包/学段包。
"""
from src.core.gate.models import (
    GateCertificate,
    GateCertificateCreate,
    GateRun,
    GateRunCreate,
    GateVerdict,
    GateVerdictCreate,
)

__all__ = [
    "GateCertificate",
    "GateCertificateCreate",
    "GateRun",
    "GateRunCreate",
    "GateVerdict",
    "GateVerdictCreate",
]
