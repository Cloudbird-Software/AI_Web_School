"""T-W2-008 门策略矩阵.

按架构 v2 §4.3 落地门策略矩阵 schema 与加载器：
- specs/contracts/gate/policy-schema.yaml：策略文件结构契约。
- specs/contracts/gate/policy.default.yaml：W2 默认策略（通用链 + 数学包 skeleton）。
- GatePolicy.load(path)：加载并校验策略（字段/artifact_type 域/validator_id 声明性）。

宪法 A5/X6：核心域零学科特判。
"""
from src.core.gate.policy.loader import (
    DEFAULT_POLICY_PATH,
    VALID_ARTIFACT_TYPES,
    ChainEntry,
    GatePolicy,
    ValidatorStep,
    load_default_policy,
)

__all__ = [
    "DEFAULT_POLICY_PATH",
    "VALID_ARTIFACT_TYPES",
    "ChainEntry",
    "GatePolicy",
    "ValidatorStep",
    "load_default_policy",
]
