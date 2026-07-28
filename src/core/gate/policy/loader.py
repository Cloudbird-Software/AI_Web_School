"""T-W2-008 门策略矩阵加载器.

落地架构 v2 §4.3「门策略矩阵」：按 pack_id × artifact_type 配置有序验证器链。
策略文件为 YAML，人类可读可版本化（specs/contracts/gate/policy.default.yaml）。

核心 API：
- `GatePolicy.load(path)` → GatePolicy 对象，校验必填字段、artifact_type 域、
  validator_id 存在性（在注册表中声明即可，不强求实现）。
- `GatePolicy.get_chain(pack_id, artifact_type)` → 有序验证器步骤列表。

为什么 validator_id 只校验「声明」不校验「实现」：策略文件是契约产物，
引用的验证器 id 可能在策略加载时尚未实现（如学科验证器待学科包注册）。
loader 保证引用合法（id 在注册表中），实现在编排器（T-W2-010）调用时才需要。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.gate.validator import (
    Validator,
    list_validators,
    register_validator,
)

# ────────────────────────────────────────────────────────────────────
# 产物类型域（与 policy-schema.yaml / item-model.md §2 对齐）
# ────────────────────────────────────────────────────────────────────
# T-W4-014 增 passage（语篇）：C 线语篇为跨学科通用产物类型，三道语篇专用门。
VALID_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"item", "material", "corpus", "group", "blueprint", "audio", "passage"}
)


# ────────────────────────────────────────────────────────────────────
# Pydantic 模型
# ────────────────────────────────────────────────────────────────────


class ValidatorStep(BaseModel):
    """链中一个验证器步骤.

    - validator_id：验证器 id（必须在注册表中声明）。
    - blocking：可选；None 时编排器取验证器类 blocking 属性。
    - params：可选；验证器参数（传 GateContext）。
    """

    model_config = ConfigDict(extra="forbid")

    validator_id: str = Field(..., min_length=1)
    blocking: bool | None = Field(default=None)
    params: dict[str, Any] = Field(default_factory=dict)


class ChainEntry(BaseModel):
    """一条验证器链（pack_id × artifact_type）.

    - pack_id + artifact_type 组合在策略内唯一。
    - validators 有序列表，编排器按顺序调用。
    """

    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(..., min_length=1)
    artifact_type: str = Field(..., min_length=1)
    validators: list[ValidatorStep] = Field(..., min_length=1)

    @field_validator("artifact_type")
    @classmethod
    def _artifact_type_in_domain(cls, v: str) -> str:
        if v not in VALID_ARTIFACT_TYPES:
            raise ValueError(
                f"artifact_type {v!r} 不在合法域 {sorted(VALID_ARTIFACT_TYPES)}"
            )
        return v


class GatePolicy(BaseModel):
    """门策略矩阵.

    - policy_version：策略版本（落 gate_run/certificate.policy_version）。
    - chains：验证器链列表，按 pack_id × artifact_type 索引。
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    description: str = Field(default="")
    chains: list[ChainEntry] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _chain_keys_unique(self) -> "GatePolicy":
        """同一 (pack_id, artifact_type) 组合不得重复定义."""
        seen: set[tuple[str, str]] = set()
        for c in self.chains:
            key = (c.pack_id, c.artifact_type)
            if key in seen:
                raise ValueError(
                    f"策略内 (pack_id={c.pack_id!r}, artifact_type={c.artifact_type!r})"
                    " 重复定义"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _validator_ids_unique_per_chain(self) -> "GatePolicy":
        """同一条链内 validator_id 不得重复."""
        for c in self.chains:
            ids = [v.validator_id for v in c.validators]
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(
                    f"链 (pack={c.pack_id!r}, type={c.artifact_type!r}) 内"
                    f" validator_id 重复：{sorted(dupes)}"
                )
        return self

    def _validate_validator_ids_exist(self) -> None:
        """校验所有 validator_id 在注册表中声明（不强求实现）.

        校验规则：validator_id 必须在「本 pack 注册表 ∪ platform 注册表」中声明。
        为什么并集 platform：通用验证器（schema/license/duplicate_placeholder）
        由平台提供，对所有学科包可用（架构 v2 §4.3：通用验证器平台提供）；
        学科包链可引用 platform 验证器 + 本学科专属验证器。

        为什么单独方法而非 model_validator：需要访问全局注册表（运行时状态），
        放 model_validator 会在模型构造时跑，不利于纯 schema 校验测试。
        load() 中显式调用，便于测试单独覆盖。

        T-W4-014：校验前先 _ensure_generic_validator_stubs() 兜底重注册平台
        通用 + 语篇验证器，防止测试中 reset_registry() 后策略加载失败。
        """
        _ensure_generic_validator_stubs()
        missing: list[tuple[str, str, str]] = []
        platform_validators = set(list_validators("platform"))
        for c in self.chains:
            available = set(list_validators(c.pack_id)) | platform_validators
            for v in c.validators:
                if v.validator_id not in available:
                    missing.append((c.pack_id, c.artifact_type, v.validator_id))
        if missing:
            details = "; ".join(
                f"pack={p!r} type={t!r} validator_id={vid!r}"
                for p, t, vid in missing
            )
            raise ValueError(f"以下 validator_id 未在注册表声明：{details}")

    # ── 加载入口 ──

    @classmethod
    def load(cls, path: str | Path) -> "GatePolicy":
        """从 YAML 文件加载策略并校验.

        校验顺序：
        1. YAML 解析 + Pydantic 字段校验（必填、artifact_type 域、链内重复）。
        2. (pack_id, artifact_type) 组合唯一。
        3. validator_id 在注册表中声明。

        Args:
            path: YAML 策略文件路径。

        Returns:
            GatePolicy 对象。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 校验失败（字段缺失 / 域越界 / validator 未声明）。
        """
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"策略文件不存在：{p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"策略文件 {p} 顶层应为映射，实际 {type(data).__name__}")
        policy = cls.model_validate(data)
        policy._validate_validator_ids_exist()
        return policy

    # ── 查询 ──

    def get_chain(
        self, pack_id: str, artifact_type: str
    ) -> list[ValidatorStep]:
        """取 (pack_id, artifact_type) 对应的有序验证器链.

        查找规则：优先精确匹配 (pack_id, artifact_type)；
        若学科包未配置，回退到 platform 通用链（架构 v2 §4.3：通用验证器平台提供）。

        Returns:
            有序 ValidatorStep 列表；无匹配时返回空列表。

        Raises:
            ValueError: artifact_type 不在合法域。
        """
        if artifact_type not in VALID_ARTIFACT_TYPES:
            raise ValueError(
                f"artifact_type {artifact_type!r} 不在合法域"
            )
        # 精确匹配
        for c in self.chains:
            if c.pack_id == pack_id and c.artifact_type == artifact_type:
                return list(c.validators)
        # 回退 platform 通用链
        for c in self.chains:
            if c.pack_id == "platform" and c.artifact_type == artifact_type:
                return list(c.validators)
        return []


# ────────────────────────────────────────────────────────────────────
# 默认策略路径
# ────────────────────────────────────────────────────────────────────
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[4]
    / "specs" / "contracts" / "gate" / "policy.default.yaml"
)


def load_default_policy() -> GatePolicy:
    """加载 W2 默认策略（specs/contracts/gate/policy.default.yaml）.

    T-W4-014：加载前确保通用 + 语篇验证器已注册（测试中 reset_registry 后
    可能清空注册表，此处兜底重注册，保证默认策略 passage 链可加载）。
    """
    _ensure_generic_validator_stubs()
    return GatePolicy.load(DEFAULT_POLICY_PATH)


# ────────────────────────────────────────────────────────────────────
# 通用验证器桩声明
# ────────────────────────────────────────────────────────────────────
# 为什么需要桩：默认策略引用 schema/license/duplicate_placeholder 三个 validator_id。
# T-W2-007 已注册示例 SchemaValidator('platform','schema')。
# license / duplicate_placeholder 的真实实现在 T-W2-009 validators/generic.py；
# 此处仅「声明」id（注册一个 NotImplementedError 桩类），让默认策略可加载校验通过。
# T-W2-009 import generic.py 时 register_validator 同 key 覆盖桩，真实实现生效。
#
# 为什么桩的 validate 抛 NotImplementedError：桩仅满足「声明」要求，不应被调用；
# 若编排器误调，立即暴露（而非静默返回假结果）。
# 为什么 not in list_validators 判存在：避免 T-W2-009 已注册真实实现后此处覆盖。


class _StubLicenseValidator(Validator):
    """license 验证器桩（真实实现见 T-W2-009 validators/generic.py LicenseValidator）."""

    validator_id = "license"
    version = "0.0.0-stub"

    async def validate(self, artifact_ref: str, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError(
            "license 验证器桩：真实实现见 T-W2-009 src/core/gate/validators/generic.py"
        )


class _StubDuplicateValidator(Validator):
    """duplicate_placeholder 验证器桩（真实实现见 T-W2-009 validators/generic.py）."""

    validator_id = "duplicate_placeholder"
    version = "0.0.0-stub"

    async def validate(self, artifact_ref: str, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError(
            "duplicate_placeholder 验证器桩：真实实现见 T-W2-009 validators/generic.py"
        )


def _ensure_generic_validator_stubs() -> None:
    """声明通用验证器桩（若未注册）.

    T-W4-014：同时确保语篇验证器已注册。reset_registry() 后模块缓存命中
    不重执行（register_validator 不触发），故显式 import 类 + 调用
    register_validator 重注册，而非依赖 import 副作用。
    """
    if "license" not in list_validators("platform"):
        register_validator("platform", _StubLicenseValidator)
    if "duplicate_placeholder" not in list_validators("platform"):
        register_validator("platform", _StubDuplicateValidator)
    # T-W4-014：确保语篇验证器已注册（reset_registry 后显式重注册）
    try:
        from src.core.gate.validators.passage_fact_check import (
            PassageFactCheckValidator,
        )
        if "passage_fact_check" not in list_validators("platform"):
            register_validator("platform", PassageFactCheckValidator)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.core.gate.validators.passage_age_appropriate import (
            PassageAgeAppropriateValidator,
        )
        if "passage_age_appropriate" not in list_validators("platform"):
            register_validator("platform", PassageAgeAppropriateValidator)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.core.gate.validators.passage_difficulty_gate import (
            PassageDifficultyGateValidator,
        )
        if "passage_difficulty_gate" not in list_validators("platform"):
            register_validator("platform", PassageDifficultyGateValidator)
    except Exception:  # noqa: BLE001
        pass


# 模块加载时声明桩，保证默认策略可加载.
_ensure_generic_validator_stubs()
