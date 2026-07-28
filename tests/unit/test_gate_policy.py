"""T-W2-008 门策略矩阵 schema 与加载器 单元测试.

对照任务卡验收标准逐条覆盖：
1. specs/contracts/gate/policy-schema.yaml 定义策略矩阵 schema（artifact_type 域）。
2. GatePolicy.load(path) 返回策略对象，校验必填字段与 validator_id 存在性。
3. W2 默认策略至少包含 generic 链：schema/linter → license → duplicate_placeholder。
4. 单元测试覆盖策略加载、版本校验、缺失 validator 报错。

宪法 A5/X6：核心域零学科特判。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.core.gate.policy.loader import (
    DEFAULT_POLICY_PATH,
    VALID_ARTIFACT_TYPES,
    ChainEntry,
    GatePolicy,
    ValidatorStep,
    load_default_policy,
)
from src.core.gate.validator import (
    SchemaValidator,
    list_validators,
    register_validator,
    reset_registry,
    Validator,
)

SCHEMA_PATH = Path("specs/contracts/gate/policy-schema.yaml")
DEFAULT_POLICY = Path("specs/contracts/gate/policy.default.yaml")


# ────────────────────────────────────────────────────────────────────
# 测试隔离：每测试恢复注册表到「T-W2-007 + T-W2-008 桩」基线
# ────────────────────────────────────────────────────────────────────
# loader.py 模块加载时已注册 license/duplicate_placeholder 桩 + T-W2-007 的 schema。
# 测试中 reset 后须重新声明桩，否则默认策略加载会因 validator_id 未声明而失败。
from src.core.gate.policy.loader import _ensure_generic_validator_stubs  # noqa: E402


@pytest.fixture(autouse=True)
def _baseline_registry():
    reset_registry()
    register_validator("platform", SchemaValidator)
    _ensure_generic_validator_stubs()
    yield
    reset_registry()
    register_validator("platform", SchemaValidator)
    _ensure_generic_validator_stubs()


# ────────────────────────────────────────────────────────────────────
# §1 policy-schema.yaml 定义策略矩阵 schema
# ────────────────────────────────────────────────────────────────────

def test_policy_schema_file_exists():
    """验收 #1：policy-schema.yaml 存在."""
    assert SCHEMA_PATH.is_file(), f"策略 schema 文件不存在：{SCHEMA_PATH}"


def test_policy_schema_defines_artifact_types():
    """验收 #1：schema 定义 artifact_type 域（含 7 种产物类型，T-W4-014 增 passage）."""
    data = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert data["registry"] == "gate_policy"
    types = set(data["artifact_types"])
    assert types == VALID_ARTIFACT_TYPES
    assert types == {"item", "material", "corpus", "group", "blueprint", "audio", "passage"}


def test_policy_schema_lists_required_fields():
    """验收 #1：schema 列出策略文件必备字段."""
    data = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    required = set(data["required_fields"])
    assert {"policy_version", "status", "chains"} <= required


# ────────────────────────────────────────────────────────────────────
# §3 默认策略含 generic 链
# ────────────────────────────────────────────────────────────────────

def test_default_policy_file_exists():
    """验收 #3：默认策略文件存在."""
    assert DEFAULT_POLICY.is_file()


def test_default_policy_loads_successfully():
    """验收 #2/3：默认策略可成功加载（字段 + validator_id 声明性校验通过）."""
    policy = load_default_policy()
    assert isinstance(policy, GatePolicy)
    assert policy.policy_version == "gate-policy-v1"
    assert len(policy.chains) >= 2  # 通用 + 数学包 skeleton


def test_default_policy_contains_generic_chain():
    """验收 #3：默认策略包含 generic 链 schema→license→duplicate_placeholder."""
    policy = load_default_policy()
    item_chain = policy.get_chain("platform", "item")
    ids = [v.validator_id for v in item_chain]
    # 廉价先行顺序：schema → license → duplicate_placeholder
    assert ids[:3] == ["schema", "license", "duplicate_placeholder"], (
        f"generic 链顺序错误：{ids}"
    )


def test_default_policy_generic_chain_order():
    """验收 #3：generic 链中 schema/license 为阻断，duplicate_placeholder 为非阻断."""
    policy = load_default_policy()
    item_chain = policy.get_chain("platform", "item")
    by_id = {v.validator_id: v for v in item_chain}
    assert by_id["schema"].blocking is True
    assert by_id["license"].blocking is True
    assert by_id["duplicate_placeholder"].blocking is False


def test_default_policy_covers_artifact_types():
    """验收 #1/3：通用链覆盖全部 7 种产物类型（T-W4-014 增 passage）."""
    policy = load_default_policy()
    covered = {c.artifact_type for c in policy.chains if c.pack_id == "platform"}
    assert covered == VALID_ARTIFACT_TYPES


def test_default_policy_has_math_skeleton():
    """默认策略含数学包 skeleton 链（学科验证器位待追加）."""
    policy = load_default_policy()
    math_chain = policy.get_chain("subject-math", "item")
    assert math_chain, "数学包 skeleton 链缺失"
    ids = [v.validator_id for v in math_chain]
    # skeleton 至少含通用三件套，学科验证器位待数学包注册后追加
    assert {"schema", "license", "duplicate_placeholder"} <= set(ids)


# ────────────────────────────────────────────────────────────────────
# §2 GatePolicy.load 校验
# ────────────────────────────────────────────────────────────────────

def _write_policy(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "policy.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
    return p


def test_load_valid_policy(tmp_path: Path):
    """验收 #2：合法策略文件加载返回 GatePolicy 对象."""
    data = {
        "policy_version": "test-v1",
        "status": "frozen-candidate",
        "description": "测试策略",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [
                    {"validator_id": "schema"},
                    {"validator_id": "license", "blocking": True},
                ],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    policy = GatePolicy.load(p)
    assert policy.policy_version == "test-v1"
    assert len(policy.chains) == 1


def test_load_missing_file_raises(tmp_path: Path):
    """文件不存在抛 FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        GatePolicy.load(tmp_path / "nope.yaml")


def test_load_missing_required_field_raises(tmp_path: Path):
    """验收 #4：缺必填字段（policy_version）报错."""
    data = {
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "schema"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValidationError):
        GatePolicy.load(p)


def test_load_invalid_artifact_type_raises(tmp_path: Path):
    """验收 #4：artifact_type 越域报错."""
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "unknown_type",
                "validators": [{"validator_id": "schema"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValidationError, match="artifact_type"):
        GatePolicy.load(p)


def test_load_duplicate_chain_key_raises(tmp_path: Path):
    """验收 #4：(pack_id, artifact_type) 重复定义报错."""
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "schema"}],
            },
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "license"}],
            },
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValidationError, match="重复定义"):
        GatePolicy.load(p)


def test_load_duplicate_validator_in_chain_raises(tmp_path: Path):
    """验收 #4：同链内 validator_id 重复报错."""
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [
                    {"validator_id": "schema"},
                    {"validator_id": "schema"},
                ],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValidationError, match="validator_id 重复"):
        GatePolicy.load(p)


# ────────────────────────────────────────────────────────────────────
# §4 缺失 validator 报错
# ────────────────────────────────────────────────────────────────────

def test_load_missing_validator_id_raises(tmp_path: Path):
    """验收 #4：validator_id 未在注册表声明时报错."""
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "ghost_validator"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValueError, match="未在注册表声明"):
        GatePolicy.load(p)


def test_load_validates_validator_id_per_pack(tmp_path: Path):
    """validator_id 校验：平台通用验证器对学科包可用，学科专属验证器不反供平台.

    平台 schema 验证器对所有 pack 可用（架构 v2 §4.3：通用验证器平台提供），
    故 subject-math 链引用 schema 应通过；仅注册在 subject-math 的验证器
    不应被 platform 链引用（分桶隔离）。
    """

    class _MathOnly(Validator):
        validator_id = "math_dual_impl"
        version = "1.0.0-test"

        async def validate(self, artifact_ref, ctx):
            ...

    register_validator("subject-math", _MathOnly)

    # subject-math 链引用 platform 的 schema + 自己的 math_dual_impl：通过
    data_ok = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "subject-math",
                "artifact_type": "item",
                "validators": [
                    {"validator_id": "schema"},
                    {"validator_id": "math_dual_impl"},
                ],
            }
        ],
    }
    p_ok = _write_policy(tmp_path, data_ok)
    policy = GatePolicy.load(p_ok)
    assert policy is not None

    # platform 链引用 subject-math 专属 math_dual_impl：报错（分桶隔离）
    data_bad = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "math_dual_impl"}],
            }
        ],
    }
    p_bad = _write_policy(tmp_path, data_bad)
    with pytest.raises(ValueError, match="未在注册表声明"):
        GatePolicy.load(p_bad)


def test_load_passes_when_validator_declared_for_pack(tmp_path: Path):
    """validator_id 在对应 pack 声明时加载通过."""

    class _MathValidator(Validator):
        validator_id = "math_dual_impl"
        version = "1.0.0-test"

        async def validate(self, artifact_ref, ctx):
            ...

    register_validator("subject-math", _MathValidator)
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "subject-math",
                "artifact_type": "item",
                "validators": [{"validator_id": "math_dual_impl"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    policy = GatePolicy.load(p)
    assert policy.get_chain("subject-math", "item")[0].validator_id == "math_dual_impl"


# ────────────────────────────────────────────────────────────────────
# get_chain 查询
# ────────────────────────────────────────────────────────────────────

def test_get_chain_exact_match(tmp_path: Path):
    """get_chain 精确匹配 (pack_id, artifact_type)."""
    policy = load_default_policy()
    chain = policy.get_chain("platform", "material")
    assert [v.validator_id for v in chain][:3] == [
        "schema", "license", "duplicate_placeholder"
    ]


def test_get_chain_fallback_to_platform(tmp_path: Path):
    """get_chain 学科包未配置时回退 platform 通用链."""
    policy = load_default_policy()
    # subject-english 未配置，回退 platform
    chain = policy.get_chain("subject-english", "item")
    assert chain  # 非空（platform 有 item 链）
    assert chain[0].validator_id == "schema"


def test_get_chain_invalid_artifact_type_raises():
    """get_chain 非法 artifact_type 抛 ValueError."""
    policy = load_default_policy()
    with pytest.raises(ValueError, match="不在合法域"):
        policy.get_chain("platform", "bogus")


def test_get_chain_empty_when_no_platform_fallback(tmp_path: Path):
    """无 platform 链也无学科链时返回空列表."""
    data = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "subject-math",
                "artifact_type": "item",
                "validators": [{"validator_id": "schema"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    # subject-math 没注册 schema，会报错——重新构造一个 platform-only 策略
    data2 = {
        "policy_version": "v1",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "schema"}],
            }
        ],
    }
    p2 = _write_policy(tmp_path, data2)
    policy = GatePolicy.load(p2)
    # subject-math 未配置且 artifact_type=corpus 无 platform 链
    assert policy.get_chain("subject-math", "corpus") == []


# ────────────────────────────────────────────────────────────────────
# 版本校验
# ────────────────────────────────────────────────────────────────────

def test_policy_version_non_empty(tmp_path: Path):
    """验收 #4：policy_version 空串报错."""
    data = {
        "policy_version": "",
        "status": "frozen-candidate",
        "chains": [
            {
                "pack_id": "platform",
                "artifact_type": "item",
                "validators": [{"validator_id": "schema"}],
            }
        ],
    }
    p = _write_policy(tmp_path, data)
    with pytest.raises(ValidationError):
        GatePolicy.load(p)


def test_default_policy_version_is_stable():
    """默认策略版本串稳定（gate-policy-v1），编排器落库用."""
    policy = load_default_policy()
    assert policy.policy_version == "gate-policy-v1"


# ────────────────────────────────────────────────────────────────────
# 核心域不 import 学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_policy_loader():
    """宪法 A5/X6：src/core/gate/policy/ 不 import 任何学科包/学段包."""
    policy_dir = os.path.join("src", "core", "gate", "policy")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_|gradeband)|import\s+(?:packs|subject_|gradeband))",
        re.MULTILINE,
    )
    violations: list[tuple[str, list[str]]] = []
    for fname in os.listdir(policy_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(policy_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        matches = pattern.findall(content)
        if matches:
            violations.append((fname, matches))
    assert not violations, f"src/core/gate/policy/ 存在学科包 import：{violations}"


# ────────────────────────────────────────────────────────────────────
# Pydantic 模型：extra='forbid'
# ────────────────────────────────────────────────────────────────────

def test_validator_step_rejects_extra_fields():
    """ValidatorStep extra='forbid'."""
    with pytest.raises(ValidationError):
        ValidatorStep(validator_id="x", bogus=True)


def test_chain_entry_rejects_extra_fields():
    """ChainEntry extra='forbid'."""
    with pytest.raises(ValidationError):
        ChainEntry(
            pack_id="platform", artifact_type="item",
            validators=[{"validator_id": "schema"}],
            unexpected=True,
        )


def test_gate_policy_rejects_extra_fields():
    """GatePolicy extra='forbid'."""
    with pytest.raises(ValidationError):
        GatePolicy(
            policy_version="v1", status="x", chains=[],
            bogus_field=True,
        )
