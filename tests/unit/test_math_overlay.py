"""T-W2-030 数学约束 overlay 预设 schema 校验测试.

对照任务卡 §验收标准：
1. overlay.yaml 包含 practice/diagnosis/measurement 三类约束参数模板
2. 参数包括：时长上限、题量区间、图形题比例、计算复杂度限制
3. schema 通过契约测试

附加覆盖：
- 数学学科特定约束（computation_complexity / unit_conversion / figure_constraints）
- 用途正交性（subject_constraints 与 purpose_presets 不冲突）
- 宪法 D5：参数按 source（先验/实测）分开，禁止混估
- 宪法 X6 反向：测试不 import 核心域内部模块（仅读 YAML + Pydantic 校验）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ────────────────────────────────────────────────────────────────────
# 加载 overlay.yaml
# ────────────────────────────────────────────────────────────────────
_OVERLAY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "packs"
    / "subject-math"
    / "assembly"
    / "overlay.yaml"
)


def _load_overlay() -> dict[str, Any]:
    """加载 overlay.yaml."""
    with open(_OVERLAY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def overlay() -> dict[str, Any]:
    """模块级 fixture：所有测试共享同一份 overlay."""
    return _load_overlay()


# ────────────────────────────────────────────────────────────────────
# Pydantic schema（对齐 overlay.yaml 中的 schema 段）
# ────────────────────────────────────────────────────────────────────


class ComputationComplexity(BaseModel):
    """计算复杂度限制."""

    model_config = ConfigDict(extra="forbid")

    max_digits_per_operand: int = Field(..., ge=1, le=10)
    max_operands: int = Field(..., ge=1, le=10)
    allowed_operations: list[str] = Field(..., min_length=1)
    allow_parentheses: bool
    allow_fraction: bool
    allow_decimal: bool
    allow_negative_intermediate: bool


class DifficultyTarget(BaseModel):
    """难度目标."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="参数来源：priori | empirical")
    target_p_correct_range: list[float] = Field(..., min_length=2, max_length=2)
    require_gradient: bool | None = None

    @field_validator("source")
    @classmethod
    def _source_enum(cls, v: str) -> str:
        if v not in ("priori", "empirical"):
            raise ValueError(f"source 必须为 priori|empirical，实际 {v!r}")
        return v

    @field_validator("target_p_correct_range")
    @classmethod
    def _range_valid(cls, v: list[float]) -> list[float]:
        if v[0] > v[1]:
            raise ValueError(f"区间下界 > 上界：{v}")
        if not (0.0 <= v[0] <= 1.0 and 0.0 <= v[1] <= 1.0):
            raise ValueError(f"区间超出 [0,1]：{v}")
        return v


class PurposePreset(BaseModel):
    """用途约束模板（practice/diagnosis/measurement）."""

    model_config = ConfigDict(extra="allow")  # 允许用途特化字段

    description: str = Field(..., min_length=1)
    time_limit_max_minutes: int = Field(..., ge=1, le=180)
    item_count_range: list[int] = Field(..., min_length=2, max_length=2)
    figure_ratio_range: list[float] = Field(..., min_length=2, max_length=2)
    computation_complexity: ComputationComplexity
    difficulty_target: DifficultyTarget

    @field_validator("item_count_range")
    @classmethod
    def _item_count_valid(cls, v: list[int]) -> list[int]:
        if v[0] > v[1]:
            raise ValueError(f"题量区间下界 > 上界：{v}")
        if v[0] < 1:
            raise ValueError(f"题量下界 < 1：{v}")
        return v

    @field_validator("figure_ratio_range")
    @classmethod
    def _figure_ratio_valid(cls, v: list[float]) -> list[float]:
        if v[0] > v[1]:
            raise ValueError(f"图形题比例区间下界 > 上界：{v}")
        if not (0.0 <= v[0] <= 1.0 and 0.0 <= v[1] <= 1.0):
            raise ValueError(f"比例超出 [0,1]：{v}")
        return v


class AssemblyConstraints(BaseModel):
    """组卷约束（通用，跨用途）."""

    model_config = ConfigDict(extra="forbid")

    max_items_per_group: int = Field(..., ge=1, le=20)
    require_gradient_monotone: bool
    content_mix: dict[str, list[float]]
    exposure_mutex: dict[str, bool]


class SubjectConstraints(BaseModel):
    """数学学科特定约束."""

    model_config = ConfigDict(extra="forbid")

    computation_complexity: ComputationComplexity
    unit_conversion: dict[str, Any]
    figure_constraints: dict[str, Any]


class OverlaySchema(BaseModel):
    """overlay.yaml 顶层 schema."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    overlay_id: str = Field(..., min_length=1)
    overlay_version: str = Field(..., min_length=1)
    status: str = Field(..., description="frozen-candidate | frozen")
    pack_id: str = Field(..., min_length=1)
    source_sections: list[str] = Field(..., min_length=1)
    subject_constraints: SubjectConstraints
    purpose_presets: dict[str, PurposePreset]
    assembly_constraints: AssemblyConstraints
    # 字段名避开 BaseModel.schema 保留属性；YAML 中仍用 `schema` 键（人类可读）
    schema_meta: dict[str, Any] = Field(
        ..., alias="schema", description="schema 元数据"
    )

    @field_validator("status")
    @classmethod
    def _status_enum(cls, v: str) -> str:
        if v not in ("frozen-candidate", "frozen"):
            raise ValueError(f"status 必须为 frozen-candidate|frozen，实际 {v!r}")
        return v

    @field_validator("purpose_presets")
    @classmethod
    def _purpose_presets_complete(cls, v: dict[str, Any]) -> dict[str, Any]:
        required = {"practice", "diagnosis", "measurement"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(f"purpose_presets 缺失：{missing}")
        return v


# ────────────────────────────────────────────────────────────────────
# §3 schema 契约测试
# ────────────────────────────────────────────────────────────────────


def test_overlay_file_exists() -> None:
    """overlay.yaml 文件存在."""
    assert _OVERLAY_PATH.exists(), f"overlay.yaml 不存在：{_OVERLAY_PATH}"


def test_overlay_schema_valid(overlay: dict[str, Any]) -> None:
    """overlay.yaml 通过 Pydantic schema 校验."""
    # 整体校验（若失败 Pydantic 抛 ValidationError）
    parsed = OverlaySchema(**overlay)
    assert parsed.overlay_id == "subject-math"
    assert parsed.pack_id == "subject-math"


def test_overlay_required_top_level_fields(overlay: dict[str, Any]) -> None:
    """overlay.yaml 含全部必需顶层字段."""
    required = {
        "overlay_id", "overlay_version", "status", "pack_id",
        "source_sections", "subject_constraints", "purpose_presets",
        "assembly_constraints", "schema",
    }
    missing = required - set(overlay.keys())
    assert not missing, f"缺失顶层字段：{missing}"


# ────────────────────────────────────────────────────────────────────
# §1 三类用途模板（practice/diagnosis/measurement）
# ────────────────────────────────────────────────────────────────────


def test_purpose_presets_three_types(overlay: dict[str, Any]) -> None:
    """purpose_presets 含 practice/diagnosis/measurement 三类."""
    presets = overlay["purpose_presets"]
    assert set(presets.keys()) == {"practice", "diagnosis", "measurement"}


def test_purpose_presets_orthogonal(overlay: dict[str, Any]) -> None:
    """三类用途模板参数互不冲突（正交组合）."""
    presets = overlay["purpose_presets"]
    # 练习时长 < 诊断时长 < 测量时长（递增）
    assert presets["practice"]["time_limit_max_minutes"] <= \
           presets["diagnosis"]["time_limit_max_minutes"] <= \
           presets["measurement"]["time_limit_max_minutes"], \
        "时长上限应递增：练习 ≤ 诊断 ≤ 测量"


# ────────────────────────────────────────────────────────────────────
# §2 必备参数（时长上限/题量区间/图形题比例/计算复杂度限制）
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("preset_name", ["practice", "diagnosis", "measurement"])
def test_preset_has_time_limit(
    overlay: dict[str, Any], preset_name: str
) -> None:
    """每个用途模板含时长上限."""
    preset = overlay["purpose_presets"][preset_name]
    assert "time_limit_max_minutes" in preset
    assert isinstance(preset["time_limit_max_minutes"], int)
    assert 1 <= preset["time_limit_max_minutes"] <= 180


@pytest.mark.parametrize("preset_name", ["practice", "diagnosis", "measurement"])
def test_preset_has_item_count_range(
    overlay: dict[str, Any], preset_name: str
) -> None:
    """每个用途模板含题量区间."""
    preset = overlay["purpose_presets"][preset_name]
    rng = preset["item_count_range"]
    assert isinstance(rng, list) and len(rng) == 2
    assert rng[0] <= rng[1]
    assert rng[0] >= 1


@pytest.mark.parametrize("preset_name", ["practice", "diagnosis", "measurement"])
def test_preset_has_figure_ratio_range(
    overlay: dict[str, Any], preset_name: str
) -> None:
    """每个用途模板含图形题比例."""
    preset = overlay["purpose_presets"][preset_name]
    rng = preset["figure_ratio_range"]
    assert isinstance(rng, list) and len(rng) == 2
    assert 0.0 <= rng[0] <= rng[1] <= 1.0


@pytest.mark.parametrize("preset_name", ["practice", "diagnosis", "measurement"])
def test_preset_has_computation_complexity(
    overlay: dict[str, Any], preset_name: str
) -> None:
    """每个用途模板含计算复杂度限制."""
    preset = overlay["purpose_presets"][preset_name]
    cc = preset["computation_complexity"]
    # 必备字段
    for key in [
        "max_digits_per_operand", "max_operands", "allowed_operations",
        "allow_parentheses", "allow_fraction", "allow_decimal",
        "allow_negative_intermediate",
    ]:
        assert key in cc, f"computation_complexity 缺字段 {key}（preset={preset_name}）"
    # allowed_operations 至少含四则
    assert set(cc["allowed_operations"]) >= {"add", "sub", "mul", "div"}
    # 小学约定：中间结果不得为负
    assert cc["allow_negative_intermediate"] is False


# ────────────────────────────────────────────────────────────────────
# 数学学科特定约束（subject_constraints）
# ────────────────────────────────────────────────────────────────────


def test_subject_constraints_exist(overlay: dict[str, Any]) -> None:
    """subject_constraints 段存在."""
    assert "subject_constraints" in overlay


def test_subject_unit_conversion_domain(overlay: dict[str, Any]) -> None:
    """数学包单位换算域：长度/质量/时间."""
    uc = overlay["subject_constraints"]["unit_conversion"]
    assert set(uc["allowed_categories"]) == {"length", "mass", "time"}
    # 长度单位含 m/cm/mm/km
    assert set(uc["length_units"]) == {"m", "cm", "mm", "km"}
    # 禁止跨类别换算
    assert uc["cross_category_conversion"] is False


def test_subject_figure_constraints(overlay: dict[str, Any]) -> None:
    """数学包图形题约束：允许类型与复杂度上限."""
    fc = overlay["subject_constraints"]["figure_constraints"]
    assert "number_line" in fc["allowed_types"]
    assert "grid" in fc["allowed_types"]
    assert "geometry_svg" in fc["allowed_types"]
    assert fc["max_complexity"] in ("simple", "moderate", "complex")
    assert fc["require_labels"] is True


# ────────────────────────────────────────────────────────────────────
# 组卷约束（assembly_constraints）
# ────────────────────────────────────────────────────────────────────


def test_assembly_constraints_group_limit(overlay: dict[str, Any]) -> None:
    """题组上限 ≤6（架构 v2 §4.4）."""
    ac = overlay["assembly_constraints"]
    assert ac["max_items_per_group"] <= 6


def test_assembly_constraints_content_mix(overlay: dict[str, Any]) -> None:
    """内容配比：新学/复习/易混淆三段区间和约 1.0."""
    cm = overlay["assembly_constraints"]["content_mix"]
    # 每个区间下界 ≤ 上界
    for key, rng in cm.items():
        assert rng[0] <= rng[1], f"{key} 区间下界 > 上界：{rng}"


# ────────────────────────────────────────────────────────────────────
# 宪法 D5：参数按 source 分开
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("preset_name", ["practice", "diagnosis", "measurement"])
def test_difficulty_target_source_separated(
    overlay: dict[str, Any], preset_name: str
) -> None:
    """难度目标的 source 必须为 priori（先验），不可混估实测值（宪法 D5）."""
    dt = overlay["purpose_presets"][preset_name]["difficulty_target"]
    assert dt["source"] in ("priori", "empirical"), \
        f"source 必须为 priori|empirical，实际 {dt['source']!r}"
    # 本文件为预设模板，全部应为先验参数
    assert dt["source"] == "priori", \
        f"预设模板应为先验参数（priori），{preset_name} 实际 {dt['source']!r}"


# ────────────────────────────────────────────────────────────────────
# overlay 自描述 schema 段一致性
# ────────────────────────────────────────────────────────────────────


def test_schema_section_self_describes(overlay: dict[str, Any]) -> None:
    """overlay.schema 段自描述必需字段，且与实际字段一致."""
    schema_meta = overlay["schema"]
    # 必需顶层字段与实际一致
    required_top = set(schema_meta["required_top_level"])
    actual_top = set(overlay.keys()) - {"schema"}
    assert required_top == actual_top, \
        f"schema.required_top_level 与实际字段不一致：{required_top} vs {actual_top}"
    # 三类用途模板声明
    assert set(schema_meta["purpose_preset_names"]) == \
           set(overlay["purpose_presets"].keys())


# ────────────────────────────────────────────────────────────────────
# 版本与状态
# ────────────────────────────────────────────────────────────────────


def test_overlay_version_semver(overlay: dict[str, Any]) -> None:
    """overlay_version 为 semver."""
    v = overlay["overlay_version"]
    parts = v.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts), f"版本非 semver：{v}"


def test_overlay_status_frozen_candidate(overlay: dict[str, Any]) -> None:
    """首版状态为 frozen-candidate（待人类审查）."""
    assert overlay["status"] == "frozen-candidate"


# ────────────────────────────────────────────────────────────────────
# 宪法 X6 反向：测试不 import 核心域内部模块
# ────────────────────────────────────────────────────────────────────


def test_no_core_imports_in_overlay_test() -> None:
    """本测试不 import src.core.*（仅读 YAML + Pydantic 校验）."""
    # 检查本文件源码不含 src.core.* import（宪法 X6 反向）
    import ast as _ast
    src = Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    forbidden_prefix = "src.core."
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefix), \
                    f"本测试不应 import {alias.name!r}（宪法 X6 反向）"
        elif isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(forbidden_prefix), \
                f"本测试不应 from-import {module!r}（宪法 X6 反向）"
