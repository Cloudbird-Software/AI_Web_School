"""T-W2-007 验证器插件统一契约与基类 单元测试.

对照任务卡验收标准逐条覆盖：
1. validator.py 定义 Validator 抽象基类与 ValidatorResult Pydantic model。
2. register_validator / get_validator 可用。
3. 示例 SchemaValidator 可运行并返回 pass/fail/review 之一。
4. 单元测试覆盖基类契约、注册表、返回值 schema 校验。
5. 不 import 任何学科包/学段包。

宪法 A5/X6：核心域零学科特判。
"""
from __future__ import annotations

import os
import re
from decimal import Decimal
import pytest
from pydantic import ValidationError

from src.core.gate.validator import (
    VALID_VERDICTS,
    GateContext,
    SchemaValidator,
    Validator,
    ValidatorResult,
    get_validator,
    list_validators,
    register_validator,
    reset_registry,
)


# ────────────────────────────────────────────────────────────────────
# §1 ValidatorResult 契约 schema
# ────────────────────────────────────────────────────────────────────

def test_validator_result_accepts_three_verdicts():
    """验收 #1：ValidatorResult 接受 pass/fail/review 三值."""
    for v in ("pass", "fail", "review"):
        r = ValidatorResult(
            verdict=v, evidence={"k": "v"}, confidence=Decimal("0.5"),
            validator_id="x", version="1.0.0",
        )
        assert r.verdict == v


def test_validator_result_rejects_invalid_verdict():
    """verdict 非 pass/fail/review 抛 ValueError."""
    with pytest.raises(ValueError, match="verdict"):
        ValidatorResult(
            verdict="rejected", evidence={}, confidence=Decimal("0.5"),
            validator_id="x", version="1.0.0",
        )


def test_validator_result_rejects_extra_fields():
    """extra='forbid'：拒绝未声明字段（契约冻结）."""
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.0"),
            validator_id="x", version="1.0.0",
            bogus_field="should_be_rejected",
        )


def test_validator_result_confidence_range():
    """confidence 边界 0.000~1.000."""
    # 合法边界
    ValidatorResult(
        verdict="pass", evidence={}, confidence=Decimal("0.000"),
        validator_id="x", version="1.0.0",
    )
    ValidatorResult(
        verdict="pass", evidence={}, confidence=Decimal("1.000"),
        validator_id="x", version="1.0.0",
    )
    # 越界
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.001"),
            validator_id="x", version="1.0.0",
        )
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("-0.001"),
            validator_id="x", version="1.0.0",
        )


def test_validator_result_cost_nonneg():
    """cost_ms / cost_tokens 必须 ≥0."""
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.0"),
            validator_id="x", version="1.0.0", cost_ms=-1,
        )
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.0"),
            validator_id="x", version="1.0.0", cost_tokens=-1,
        )


def test_validator_result_requires_validator_id_and_version():
    """validator_id / version 必填非空."""
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.0"),
            validator_id="", version="1.0.0",
        )
    with pytest.raises(ValidationError):
        ValidatorResult(
            verdict="pass", evidence={}, confidence=Decimal("1.0"),
            validator_id="x", version="",
        )


def test_validator_result_defaults():
    """默认值：evidence={} / confidence=1.000 / cost_ms=0 / cost_tokens=0."""
    r = ValidatorResult(
        verdict="review", validator_id="x", version="1.0.0",
    )
    assert r.evidence == {}
    assert r.confidence == Decimal("1.000")
    assert r.cost_ms == 0
    assert r.cost_tokens == 0


# ────────────────────────────────────────────────────────────────────
# §1 Validator 抽象基类
# ────────────────────────────────────────────────────────────────────

def test_validator_is_abstract():
    """Validator 不可直接实例化（须实现 validate）."""
    with pytest.raises(TypeError):
        Validator()  # type: ignore[abstract]


def test_validator_subclass_must_implement_validate():
    """子类未实现 validate 仍为抽象，不可实例化."""

    class _Incomplete(Validator):
        validator_id: str = "incomplete"
        version: str = "0.0.0"

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


def test_validator_default_classvars():
    """默认 blocking=True / cost_tier='cheap'."""
    assert Validator.blocking is True
    assert Validator.cost_tier == "cheap"


# ────────────────────────────────────────────────────────────────────
# §2 注册表：register_validator / get_validator
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_registry():
    """每测试独立注册表：保存/恢复，避免测试间污染.

    validator.py 模块加载时已注册 SchemaValidator('platform','schema')，
    测试中 reset 后可验证空注册表行为，再恢复示例注册。
    """
    reset_registry()
    register_validator("platform", SchemaValidator)
    yield
    reset_registry()
    register_validator("platform", SchemaValidator)


def test_register_and_get_validator():
    """验收 #2：注册后可按 (pack_id, validator_id) 取实例."""
    v = get_validator("platform", "schema")
    assert isinstance(v, SchemaValidator)
    assert v.validator_id == "schema"
    assert v.version == "0.1.0+example"


def test_get_validator_unknown_raises_keyerror():
    """未注册的 validator_id 抛 KeyError."""
    with pytest.raises(KeyError, match="未在 pack"):
        get_validator("platform", "does_not_exist")


def test_get_validator_unknown_pack_raises_keyerror():
    """未注册的 pack_id 抛 KeyError."""
    with pytest.raises(KeyError):
        get_validator("subject-math", "schema")


def test_register_validator_rejects_non_validator():
    """register_validator 拒绝非 Validator 子类."""
    with pytest.raises(TypeError):
        register_validator("platform", object)  # type: ignore[arg-type]


def test_register_validator_rejects_missing_id_or_version():
    """register_validator 拒绝缺 validator_id/version 类属性的子类."""

    class _NoId(Validator):
        version: str = "1.0.0"

        async def validate(self, artifact_ref, ctx):
            ...

    with pytest.raises(AttributeError):
        register_validator("platform", _NoId)

    class _NoVersion(Validator):
        validator_id: str = "x"

        async def validate(self, artifact_ref, ctx):
            ...

    with pytest.raises(AttributeError):
        register_validator("platform", _NoVersion)


def test_register_validator_per_pack_isolation():
    """同 validator_id 在不同 pack_id 下独立注册（平台/学科包各一份）."""

    class _MathValidator(Validator):
        validator_id: str = "schema"  # 同名
        version: str = "1.0.0+math"

        async def validate(self, artifact_ref, ctx):
            return self._timed_result("pass", {}, Decimal("1.0"), 0)

    register_validator("subject-math", _MathValidator)
    platform_v = get_validator("platform", "schema")
    math_v = get_validator("subject-math", "schema")
    assert isinstance(platform_v, SchemaValidator)
    assert isinstance(math_v, _MathValidator)


def test_list_validators():
    """list_validators 返回某 pack 下已注册 id 列表."""
    assert "schema" in list_validators("platform")
    assert list_validators("subject-math") == []


# ────────────────────────────────────────────────────────────────────
# §3 示例 SchemaValidator 返回 pass/fail/review
# ────────────────────────────────────────────────────────────────────

async def test_schema_validator_pass():
    """验收 #3：SchemaValidator 全部必填键存在 → pass."""
    v = SchemaValidator()
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": {}, "content": {}, "scoring_ref": {}},
        required_keys=["objective", "content", "scoring_ref"],
    )
    r = await v.validate("sha256:item-v1", ctx)
    assert r.verdict == "pass"
    assert r.validator_id == "schema"
    assert r.version == "0.1.0+example"
    assert r.confidence == Decimal("1.000")
    assert r.cost_ms >= 0
    assert r.cost_tokens == 0
    assert r.evidence["checked_keys"] == ["objective", "content", "scoring_ref"]


async def test_schema_validator_fail_missing_keys():
    """验收 #3：缺键 → fail."""
    v = SchemaValidator()
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": {}},  # 缺 content / scoring_ref
        required_keys=["objective", "content", "scoring_ref"],
    )
    r = await v.validate("sha256:item-v2", ctx)
    assert r.verdict == "fail"
    assert "content" in r.evidence["missing_keys"]
    assert "scoring_ref" in r.evidence["missing_keys"]
    assert r.confidence == Decimal("1.000")


async def test_schema_validator_review_when_payload_none():
    """验收 #3：payload 为 None → review（转人工）."""
    v = SchemaValidator()
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload=None,
        required_keys=["objective"],
    )
    r = await v.validate("sha256:item-v3", ctx)
    assert r.verdict == "review"
    assert r.confidence == Decimal("0.000")
    assert "reason" in r.evidence


async def test_schema_validator_returns_contract_fields():
    """验收 #4：返回值含全部契约字段（schema 校验）."""
    v = SchemaValidator()
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"a": 1},
        required_keys=["a"],
    )
    r = await v.validate("ref", ctx)
    # 全部契约字段存在且类型正确
    assert r.verdict in VALID_VERDICTS
    assert isinstance(r.evidence, dict)
    assert isinstance(r.confidence, Decimal)
    assert isinstance(r.validator_id, str) and r.validator_id
    assert isinstance(r.version, str) and r.version
    assert isinstance(r.cost_ms, int) and r.cost_ms >= 0
    assert isinstance(r.cost_tokens, int) and r.cost_tokens >= 0


async def test_schema_validator_via_registry():
    """通过注册表取实例并运行（端到端契约闭环）."""
    v = get_validator("platform", "schema")
    ctx = GateContext(
        artifact_type="material",
        pack_id="platform",
        artifact_payload={"content_ref": "s3://...", "license_id": "lic-1"},
        required_keys=["content_ref", "license_id"],
    )
    r = await v.validate("sha256:mat-v1", ctx)
    assert r.verdict == "pass"
    assert r.validator_id == "schema"


# ────────────────────────────────────────────────────────────────────
# §5 核心域不 import 学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_gate_validator():
    """宪法 A5/X6：src/core/gate/validator.py 不 import 任何学科包/学段包."""
    fpath = os.path.join("src", "core", "gate", "validator.py")
    with open(fpath, encoding="utf-8") as f:
        content = f.read()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_|gradeband)|import\s+(?:packs|subject_|gradeband))",
        re.MULTILINE,
    )
    assert not pattern.findall(content), (
        "validator.py 存在学科包/学段包 import（违反 A5/X6）"
    )


def test_gate_context_accepts_extra_fields():
    """GateContext extra='allow'：学科验证器可注入自定义字段."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        sympy_cache={"expr": "x**2"},  # 学科扩展字段
    )
    dumped = ctx.model_dump()
    assert dumped["sympy_cache"] == {"expr": "x**2"}


# ────────────────────────────────────────────────────────────────────
# 自定义验证器通过基类闭环（证明框架可扩展）
# ────────────────────────────────────────────────────────────────────

async def test_custom_validator_registers_and_runs():
    """自定义 Validator 子类可注册、可运行、返回契约结果."""

    class _AlwaysPassValidator(Validator):
        validator_id: str = "always_pass"
        version: str = "1.0.0-test"
        blocking = False
        cost_tier = "expensive"

        async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
            return self._timed_result(
                verdict="pass",
                evidence={"note": "stub for orchestrator test"},
                confidence=Decimal("0.500"),
                elapsed_ms=5,
                cost_tokens=42,
            )

    register_validator("platform", _AlwaysPassValidator)
    v = get_validator("platform", "always_pass")
    r = await v.validate("ref", GateContext(artifact_type="item", pack_id="platform"))
    assert r.verdict == "pass"
    assert r.validator_id == "always_pass"
    assert r.version == "1.0.0-test"
    assert r.cost_tokens == 42
    assert r.confidence == Decimal("0.500")
