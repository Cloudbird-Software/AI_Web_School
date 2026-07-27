"""T-W2-007 验证器插件统一契约与基类.

落地架构 v2 §4.3「校验域」三要素中的「验证器插件」层：
- 统一契约：`validate(artifact_ref, ctx) -> ValidatorResult`
  字段：{verdict, evidence, confidence, validator_id, version, cost_ms, cost_tokens}
- 抽象基类 `Validator`：学科包与平台通用验证器共用。
- 注册制：`register_validator(pack_id, validator_cls)` / `get_validator(pack_id, validator_id)`，
  按 pack_id 分桶——通用验证器 pack_id='platform'，学科包验证器 pack_id='subject-math' 等。
- 示例验证器 `SchemaValidator`：最小结构校验，证明契约可运行。

宪法 A5/X6：核心域零学科特判——本模块不 import 任何学科包/学段包；
学科验证器由各学科包在各自模块中调用 register_validator 注入（运行时装配），
本模块只提供框架与注册表，不感知任何学科语义。

为什么 ValidatorResult 用 Pydantic：契约字段类型化、extra='forbid' 拒绝未声明字段，
与 gate_run 表 schema（迁移 0004）一一对应，便于编排器（T-W2-010）直接落库。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


# ────────────────────────────────────────────────────────────────────
# 统一契约：验证器返回值
# ────────────────────────────────────────────────────────────────────
# 与 gate_run 表（迁移 0004）字段一一对应：verdict/evidence/confidence/
# validator_id/validator_version/cost_ms/cost_tokens。
# verdict 三值：pass（通过）/ fail（阻断失败）/ review（人工复核）。
# 为什么 extra='forbid'：契约冻结，禁止验证器私自塞字段，避免 gate_run 落库时
# 因列名不匹配导致 INSERT 失败或证据污染。

Verdict = str  # 'pass' | 'fail' | 'review'（Literal 见下，用于类型标注收紧）
VALID_VERDICTS: frozenset[str] = frozenset({"pass", "fail", "review"})


class ValidatorResult(BaseModel):
    """验证器统一返回契约.

    - verdict：pass/fail/review（review=人工复核，编排器不短路）。
    - evidence：证据（验证器自描述结构，落 gate_run.evidence jsonb）。
    - confidence：0.000~1.000（落 gate_run.confidence numeric(4,3)）。
    - validator_id / version：验证器身份与版本（落 gate_run 对应列）。
    - cost_ms / cost_tokens：运行成本（落 gate_run 对应列，≥0）。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(..., description="判定：pass/fail/review")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="证据（验证器自描述结构）"
    )
    confidence: Decimal = Field(
        default=Decimal("1.000"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="置信度 0.000~1.000",
    )
    validator_id: str = Field(..., min_length=1, description="验证器 id（注册表）")
    version: str = Field(..., min_length=1, description="验证器版本")
    cost_ms: int = Field(default=0, ge=0, description="耗时（毫秒）")
    cost_tokens: int = Field(default=0, ge=0, description="token 成本")

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.verdict not in VALID_VERDICTS:
            raise ValueError(
                f"verdict 必须 ∈ {sorted(VALID_VERDICTS)}，实际：{self.verdict!r}"
            )


# ────────────────────────────────────────────────────────────────────
# 验证器运行上下文
# ────────────────────────────────────────────────────────────────────
# 为什么用 Pydantic 而非裸 dict：字段类型化、可校验、IDE 友好；
# extra='allow' 允许学科验证器扩展自定义字段（如数学包可塞 sympy 表达式缓存）。
# artifact_ref 作为 validate() 的独立参数（契约字面），ctx 携带附加信息。


class GateContext(BaseModel):
    """验证器运行上下文.

    - artifact_type：产物类型（item/material/corpus/group/blueprint/audio）。
    - pack_id：学科包 id（'platform' 或 'subject-math' 等），编排器按策略矩阵查找链时用。
    - artifact_payload：产物内容快照（验证器读取结构/字段）。
    - extra='allow'：学科验证器可注入自定义字段（如 db 会话、图谱快照）。
    """

    model_config = ConfigDict(extra="allow")

    artifact_type: str = Field(..., description="产物类型")
    pack_id: str = Field(..., description="学科包 id")
    artifact_payload: dict[str, Any] | None = Field(
        default=None, description="产物内容快照"
    )


# ────────────────────────────────────────────────────────────────────
# 抽象基类
# ────────────────────────────────────────────────────────────────────


class Validator(ABC):
    """验证器抽象基类.

    子类须声明类属性：
    - validator_id：注册表 id（同 pack_id 内唯一）。
    - version：版本串（落 gate_run.validator_version，便于回溯）。
    - blocking：是否阻断项——True 时 fail 短路（编排器 T-W2-010）；
      False 时（如抽检类）fail 仅留痕不阻断。
    - cost_tier：'cheap'（廉价先行）/ 'expensive'（后置，如强模型裁判），
      编排器按 cost_tier 排序，廉价验证器先跑省钱。

    为什么 validate 是 async：LicenseValidator/DuplicatePlaceholderValidator 需查 DB
    （material_license / 已 published 版本），运行时走 AsyncSession；同步验证器
    用 `async def` 即可，无额外成本。
    """

    validator_id: ClassVar[str]
    version: ClassVar[str]
    blocking: ClassVar[bool] = True
    cost_tier: ClassVar[str] = "cheap"

    @abstractmethod
    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        """执行校验，返回统一契约结果.

        Args:
            artifact_ref: 被检产物引用（如 item_version_id）。
            ctx: 运行上下文（产物类型/载荷/扩展字段）。

        Returns:
            ValidatorResult：verdict/evidence/confidence/validator_id/version/cost。
        """
        ...

    def _timed_result(
        self,
        verdict: str,
        evidence: dict[str, Any],
        confidence: Decimal,
        elapsed_ms: int,
        cost_tokens: int = 0,
    ) -> ValidatorResult:
        """构造带耗时与身份的 ValidatorResult（子类便利方法）.

        为什么单独提供：子类无需重复填 validator_id/version，且确保与类声明一致。
        """
        return ValidatorResult(
            verdict=verdict,
            evidence=evidence,
            confidence=confidence,
            validator_id=self.validator_id,
            version=self.version,
            cost_ms=elapsed_ms,
            cost_tokens=cost_tokens,
        )


# ────────────────────────────────────────────────────────────────────
# 注册表
# ────────────────────────────────────────────────────────────────────
# 按 (pack_id, validator_id) 索引验证器类。pack_id='platform' 为通用验证器；
# 学科包各自注册时用 pack_id='subject-math' 等。
# 为什么存类而非实例：验证器无状态（配置经 ctx 传入），按需实例化，避免全局可变状态持有实例。


_VALIDATOR_REGISTRY: dict[tuple[str, str], type[Validator]] = {}


def register_validator(pack_id: str, validator_cls: type[Validator]) -> None:
    """注册验证器类.

    Args:
        pack_id: 学科包 id（'platform' 或 'subject-math' 等）。
        validator_cls: Validator 子类（须已声明 validator_id / version 类属性）。

    Raises:
        TypeError: validator_cls 不是 Validator 子类。
        AttributeError: validator_cls 未声明 validator_id / version。
    """
    if not isinstance(validator_cls, type) or not issubclass(validator_cls, Validator):
        raise TypeError(f"{validator_cls!r} 不是 Validator 子类")
    vid = getattr(validator_cls, "validator_id", None)
    ver = getattr(validator_cls, "version", None)
    if not vid or not ver:
        raise AttributeError(
            f"{validator_cls.__name__} 须声明非空类属性 validator_id 与 version"
        )
    _VALIDATOR_REGISTRY[(pack_id, vid)] = validator_cls


def get_validator(pack_id: str, validator_id: str) -> Validator:
    """取验证器实例.

    Args:
        pack_id: 学科包 id。
        validator_id: 验证器 id。

    Returns:
        Validator 实例（按需实例化，无状态）。

    Raises:
        KeyError: 未注册。
    """
    cls = _VALIDATOR_REGISTRY.get((pack_id, validator_id))
    if cls is None:
        raise KeyError(
            f"验证器 {validator_id!r} 未在 pack {pack_id!r} 注册"
        )
    return cls()


def list_validators(pack_id: str) -> list[str]:
    """列出某 pack 下已注册的 validator_id（调试/策略加载校验用）."""
    return [vid for (pid, vid) in _VALIDATOR_REGISTRY if pid == pack_id]


def reset_registry() -> None:
    """清空注册表（测试隔离用）."""
    _VALIDATOR_REGISTRY.clear()


# ────────────────────────────────────────────────────────────────────
# 示例验证器：SchemaValidator（最小结构校验）
# ────────────────────────────────────────────────────────────────────
# 本类是契约可运行性的最小证明——T-W2-009 在 validators/generic.py 落地生产级
# SchemaValidator（调 JSON Schema / DSL Linter）。此处仅做「必填键存在性」校验。
# 三种 verdict 演示：pass（齐全）/ fail（缺键）/ review（payload 为 None，待人工）。


class SchemaValidator(Validator):
    """示例结构验证器：校验 artifact_payload 含 ctx 指定的 required_keys.

    ctx 期望字段：
    - required_keys: list[str]——必填键（缺任一即 fail）。

    verdict 规则：
    - review：artifact_payload 为 None（无法机器判定，转人工）。
    - fail：缺少任一 required_keys。
    - pass：全部必填键存在。
    """

    validator_id: ClassVar[str] = "schema"
    version: ClassVar[str] = "0.1.0+example"
    blocking: ClassVar[bool] = True
    cost_tier: ClassVar[str] = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        required: list[str] = list(ctx.model_dump().get("required_keys", []) or [])
        payload = ctx.artifact_payload

        if payload is None:
            elapsed = int((time.monotonic() - start) * 1000)
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "artifact_payload 为 None，无法机器校验结构",
                    "required_keys": required,
                },
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed,
            )

        missing = [k for k in required if k not in payload]
        elapsed = int((time.monotonic() - start) * 1000)
        if missing:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "missing_keys": missing,
                    "required_keys": required,
                    "present_keys": list(payload.keys()),
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed,
            )
        return self._timed_result(
            verdict="pass",
            evidence={
                "checked_keys": required,
                "present_keys": list(payload.keys()),
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed,
        )


# 模块加载时注册示例验证器（pack_id='platform'），证明注册机制闭环.
# 为什么不在这里注册 T-W2-009 的真实验证器：那些在 validators/generic.py 中注册，
# 由编排器按需 import；本模块只保证「框架自带一个可运行示例」.
register_validator("platform", SchemaValidator)
