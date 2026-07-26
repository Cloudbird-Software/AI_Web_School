"""T-W1-004 注册表加载与校验单元测试.

对照任务卡验收标准逐条覆盖：
1. 包存在 + 加载仅依赖 pyyaml/pydantic
2. load_interaction_registry() 解析 12 种交互类型
3. load_scorer_registry() 解析 7 种评分器
4. get_interaction/get_scorer 可用，list_active() 计数正确（10 + 6）
5. compatible_scorers 交叉引用校验
6. 未知 id 抛异常 / 预留类型 status 正确 / 交叉引用完整
7. src/registry/ 不 import 任何学科包/学段包

宪法 D4：作答交互与评分器只能来自平台注册表。
宪法 X6：核心域代码禁止 import 学科包/学段包（本包虽不在 src/core/，
但同样适用此纪律，任务卡 §7 明文要求）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.registry import (
    DEFAULT_INTERACTION_PATH,
    DEFAULT_SCORER_PATH,
    InteractionRegistry,
    InteractionType,
    ScorerRegistry,
    ScorerType,
    get_interaction_registry,
    get_scorer_registry,
    load_interaction_registry,
    load_scorer_registry,
    reset_registries,
    validate_cross_references,
)


# ────────────────────────────────────────────────────────────────────
# §1 / §2 / §3 解析正确性
# ────────────────────────────────────────────────────────────────────

def test_load_interaction_registry_returns_pydantic_model():
    """load_interaction_registry 返回 InteractionRegistry 实例. 对齐验收 §2."""
    reg = load_interaction_registry()
    assert isinstance(reg, InteractionRegistry)
    assert reg.registry == "interaction"
    assert reg.contract_version  # 非空
    assert reg.status in ("frozen-candidate", "frozen")


def test_load_interaction_registry_parses_twelve_types():
    """验收 §2：解析 12 种交互类型（10 现役 + 2 预留）."""
    reg = load_interaction_registry()
    assert len(reg.types) == 12, f"应为 12 种，实际 {len(reg.types)}"
    active = [t for t in reg.types if t.status == "active"]
    reserved = [t for t in reg.types if t.status == "reserved"]
    assert len(active) == 10, f"现役应为 10 种，实际 {len(active)}"
    assert len(reserved) == 2, f"预留应为 2 种，实际 {len(reserved)}"


def test_load_scorer_registry_returns_pydantic_model():
    """load_scorer_registry 返回 ScorerRegistry 实例. 对齐验收 §3."""
    reg = load_scorer_registry()
    assert isinstance(reg, ScorerRegistry)
    assert reg.registry == "scorer"
    assert reg.contract_version
    assert reg.status in ("frozen-candidate", "frozen")


def test_load_scorer_registry_parses_seven_scorers():
    """验收 §3：解析 7 种评分器（6 现役 + 1 预留）."""
    reg = load_scorer_registry()
    assert len(reg.scorers) == 7, f"应为 7 种，实际 {len(reg.scorers)}"
    active = [s for s in reg.scorers if s.status == "active"]
    reserved = [s for s in reg.scorers if s.status == "reserved"]
    assert len(active) == 6, f"现役应为 6 种，实际 {len(active)}"
    assert len(reserved) == 1, f"预留应为 1 种，实际 {len(reserved)}"


def test_default_paths_point_to_existing_contracts():
    """默认路径常量指向真实契约文件. 对齐验收 §1（包仅依赖 pyyaml/pydantic）."""
    assert DEFAULT_INTERACTION_PATH.is_file(), (
        f"默认 interaction.yaml 不存在: {DEFAULT_INTERACTION_PATH}"
    )
    assert DEFAULT_SCORER_PATH.is_file(), (
        f"默认 scorer.yaml 不存在: {DEFAULT_SCORER_PATH}"
    )


# ────────────────────────────────────────────────────────────────────
# §4 查询方法 + 计数
# ────────────────────────────────────────────────────────────────────

def test_get_interaction_returns_expected_type():
    """get_interaction(id) 返回匹配实例且字段完整. 对齐验收 §4."""
    reg = load_interaction_registry()
    single = reg.get_interaction("single_choice")
    assert isinstance(single, InteractionType)
    assert single.id == "single_choice"
    assert single.name == "单选"
    assert single.status == "active"
    assert single.render_component == "platform/RadioGroup"
    assert "exact_match" in single.compatible_scorers


def test_get_scorer_returns_expected_type():
    """get_scorer(id) 返回匹配实例且字段完整. 对齐验收 §4."""
    reg = load_scorer_registry()
    em = reg.get_scorer("exact_match")
    assert isinstance(em, ScorerType)
    assert em.id == "exact_match"
    assert em.name == "精确匹配"
    assert em.status == "active"
    assert em.deterministic is True
    assert "single_choice" in em.input_contract


def test_list_active_returns_correct_counts():
    """list_active() 返回正确计数（10 交互 + 6 评分）. 对齐验收 §4."""
    ir = load_interaction_registry()
    sr = load_scorer_registry()
    assert len(ir.list_active()) == 10
    assert len(sr.list_active()) == 6


# ────────────────────────────────────────────────────────────────────
# §6 未知 id 抛异常 / 预留类型 status / 交叉引用完整
# ────────────────────────────────────────────────────────────────────

def test_get_interaction_unknown_id_raises_keyerror():
    """未知 id 抛 KeyError. 对齐验收 §6."""
    reg = load_interaction_registry()
    with pytest.raises(KeyError):
        reg.get_interaction("does_not_exist_xyz")


def test_get_scorer_unknown_id_raises_keyerror():
    """未知 id 抛 KeyError. 对齐验收 §6."""
    reg = load_scorer_registry()
    with pytest.raises(KeyError):
        reg.get_scorer("does_not_exist_xyz")


def test_reserved_interaction_status_correct():
    """预留交互类型 status 正确. 对齐验收 §6."""
    reg = load_interaction_registry()
    reserved = {t.id: t for t in reg.types if t.status == "reserved"}
    assert set(reserved.keys()) == {"handwriting_copy", "oral"}
    for t in reserved.values():
        assert t.status == "reserved"
        assert t.response_schema.get("type") == "object"


def test_reserved_scorer_status_correct():
    """预留评分器 status 正确（asr_oral 唯一预留）. 对齐验收 §6."""
    reg = load_scorer_registry()
    reserved = [s for s in reg.scorers if s.status == "reserved"]
    assert len(reserved) == 1
    assert reserved[0].id == "asr_oral"
    assert reserved[0].status == "reserved"
    assert reserved[0].deterministic is False


def test_deterministic_flag_is_bool():
    """deterministic 字段为布尔（R-D-05 重判可复现性前提）."""
    reg = load_scorer_registry()
    for s in reg.scorers:
        assert isinstance(s.deterministic, bool), (
            f"{s.id} 的 deterministic 应为 bool，实际 {type(s.deterministic)}"
        )
    # 现役确定性评分器集合
    det_ids = {s.id for s in reg.scorers if s.deterministic}
    assert {"exact_match", "math_equivalence", "stepwise_rubric", "keypoint_hit"} <= det_ids


def test_true_false_is_preset_not_type():
    """判断题是 single_choice 的预设，不单独注册. 架构 v2 §2.3."""
    reg = load_interaction_registry()
    ids = {t.id for t in reg.types}
    assert "true_false" not in ids
    single = reg.get_interaction("single_choice")
    assert single.presets is not None
    preset_ids = {p["id"] for p in single.presets}
    assert "true_false" in preset_ids


# ────────────────────────────────────────────────────────────────────
# §5 交叉引用校验
# ────────────────────────────────────────────────────────────────────

def test_validate_cross_references_passes_for_valid_registries():
    """交叉引用校验通过（验收 §5 / §6 完整性）."""
    ir = load_interaction_registry()
    sr = load_scorer_registry()
    # 不抛异常即通过
    validate_cross_references(ir, sr)


def test_validate_cross_references_detects_dangling_reference():
    """交叉引用校验失败：交互引用未注册的评分器时抛 ValueError."""
    ir = load_interaction_registry()
    sr = load_scorer_registry()
    # 构造断裂：给 single_choice 加一个不存在的 scorer id
    # InteractionType 是 frozen，必须用 model_copy 构造新实例
    broken_type = ir.types[0].model_copy(
        update={"compatible_scorers": ["__ghost_scorer__"]}
    )
    broken_types = list(ir.types)
    broken_types[0] = broken_type
    broken_ir = ir.model_copy(update={"types": broken_types})
    with pytest.raises(ValueError, match="compatible_scorers"):
        validate_cross_references(broken_ir, sr)


def test_compatible_scorers_all_exist_in_scorer_registry():
    """所有 compatible_scorers 必须在 scorer registry 中注册. 宪法 D4."""
    ir = load_interaction_registry()
    sr = load_scorer_registry()
    scorer_ids = {s.id for s in sr.scorers}
    for t in ir.types:
        for sid in t.compatible_scorers:
            assert sid in scorer_ids, (
                f"{t.id} 引用了未注册的评分器 {sid}"
            )


# ────────────────────────────────────────────────────────────────────
# §1 加载函数仅依赖 pyyaml + pydantic
# ────────────────────────────────────────────────────────────────────

def test_loader_module_imports_are_minimal():
    """loader.py 仅依赖 pyyaml/pydantic/标准库（验收 §1）.

    为什么用相对路径：与 test_no_subject_pack_imports_in_registry 一致，
    pytest 在项目根目录运行时相对路径即契约文件位置；避免 __file__
    在 Windows 中文路径下的编码陷阱。
    """
    loader_path = os.path.join("src", "registry", "loader.py")
    with open(loader_path, encoding="utf-8") as f:
        content = f.read()
    # 收集所有顶层 import（排除 from __future__）
    imports: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            if "from __future__" in stripped:
                continue
            imports.append(stripped)
    # 第三方依赖只能是 pyyaml（导入名为 yaml）和 pydantic
    allowed = {"yaml", "pydantic"}
    for imp in imports:
        # 取首个 token：import X / from X import Y
        if imp.startswith("import "):
            mod = imp[len("import "):].split(",")[0].split(" as ")[0].strip()
        else:  # from X import Y
            mod = imp[len("from "):].split(" import ")[0].strip()
        # 标准库/相对导入放行
        if mod.startswith(".") or mod in ("pathlib", "typing", "os", "sys"):
            continue
        # 取顶层包名
        top = mod.split(".")[0]
        assert top in allowed, (
            f"loader.py 不应依赖 {top}（仅允许 pyyaml + pydantic）"
        )


# ────────────────────────────────────────────────────────────────────
# §7 src/registry/ 不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_registry():
    """宪法 A5/X6 + 任务卡 §7：src/registry/ 不 import 任何学科包/学段包."""
    registry_dir = os.path.join("src", "registry")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[tuple[str, list[str]]] = []
    for fname in os.listdir(registry_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(registry_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        matches = pattern.findall(content)
        if matches:
            violations.append((fname, matches))
    assert not violations, f"src/registry/ 存在学科包 import：{violations}"


# ────────────────────────────────────────────────────────────────────
# Pydantic 模型校验：缺字段 / 不可变
# ────────────────────────────────────────────────────────────────────

def test_interaction_type_rejects_missing_required_fields():
    """InteractionType 缺必填字段抛 ValidationError."""
    with pytest.raises(ValidationError):
        InteractionType(
            id="x",
            name="x",
            status="active",
            summary="x",
            # 缺 response_schema / render_component / paper_spec /
            # scoring_input / compatible_scorers
        )


def test_scorer_type_rejects_missing_required_fields():
    """ScorerType 缺必填字段抛 ValidationError."""
    with pytest.raises(ValidationError):
        ScorerType(
            id="x",
            name="x",
            status="active",
            # 缺 deterministic / summary / input_contract /
            # params_schema / notes
        )


def test_interaction_type_rejects_invalid_status():
    """status 必须是 active/reserved 之一."""
    with pytest.raises(ValidationError):
        InteractionType(
            id="x", name="x", status="invalid_status", summary="x",
            response_schema={"type": "object"},
            render_component="platform/X",
            paper_spec="x", scoring_input="x",
            compatible_scorers=[],
        )


def test_scorer_type_rejects_invalid_status():
    """status 必须是 active/reserved 之一."""
    with pytest.raises(ValidationError):
        ScorerType(
            id="x", name="x", status="invalid_status",
            deterministic=True, summary="x",
            input_contract="x", params_schema={},
            notes="x",
        )


def test_interaction_registry_is_frozen():
    """frozen=True：单例加载后禁止运行时改写（宪法 D4）."""
    reg = load_interaction_registry()
    with pytest.raises(ValidationError):
        reg.types = []  # type: ignore[misc]


def test_interaction_type_instance_is_frozen():
    """InteractionType 实例不可变."""
    t = load_interaction_registry().get_interaction("single_choice")
    with pytest.raises(ValidationError):
        t.name = "改写测试"  # type: ignore[misc]


def test_scorer_registry_is_frozen():
    """frozen=True：评分器注册表不可变."""
    reg = load_scorer_registry()
    with pytest.raises(ValidationError):
        reg.scorers = []  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────
# 单例入口
# ────────────────────────────────────────────────────────────────────

def test_singleton_getters_return_same_instance():
    """单例入口：重复调用返回同一对象."""
    reset_registries()
    ir1 = get_interaction_registry()
    ir2 = get_interaction_registry()
    assert ir1 is ir2

    sr1 = get_scorer_registry()
    sr2 = get_scorer_registry()
    assert sr1 is sr2


def test_singleton_getters_load_both_registries():
    """任一 getter 首次访问都会触发双注册表加载 + 交叉引用校验."""
    reset_registries()
    # 仅访问 interaction getter，scorer 也应已就绪
    ir = get_interaction_registry()
    sr = get_scorer_registry()
    assert isinstance(ir, InteractionRegistry)
    assert isinstance(sr, ScorerRegistry)
    # 再次访问 scorer 应是同一实例（缓存共享）
    assert get_scorer_registry() is sr


def test_reset_registries_clears_cache():
    """reset_registries() 清空缓存：下次访问得到新实例."""
    reset_registries()
    ir1 = get_interaction_registry()
    reset_registries()
    ir2 = get_interaction_registry()
    assert ir1 is not ir2
    # 但内容应等价
    assert ir1.contract_version == ir2.contract_version
    assert {t.id for t in ir1.types} == {t.id for t in ir2.types}


def test_singleton_cross_reference_validation_runs_on_load():
    """单例加载时执行交叉引用校验（验收 §5）.

    构造一个断裂的 interaction.yaml 临时文件，验证 get_interaction_registry
    会因交叉引用校验失败而抛 ValueError。
    """
    reset_registries()
    # 复制合法 interaction.yaml，把 single_choice 的 compatible_scorers
    # 改成引用一个不存在的 scorer id
    with open(DEFAULT_INTERACTION_PATH, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    for t in data["types"]:
        if t["id"] == "single_choice":
            t["compatible_scorers"] = ["__ghost_scorer__"]
    tmp_path = Path(__file__).parent / "_tmp_broken_interaction.yaml"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)
        with pytest.raises(ValueError, match="compatible_scorers"):
            get_interaction_registry(
                interaction_path=tmp_path,
                scorer_path=DEFAULT_SCORER_PATH,
                reload=True,
            )
    finally:
        if tmp_path.is_file():
            tmp_path.unlink()
        reset_registries()


# ────────────────────────────────────────────────────────────────────
# 加载器对自定义路径的支持（验收 §1 灵活性）
# ────────────────────────────────────────────────────────────────────

def test_load_interaction_registry_accepts_custom_path(tmp_path: Path):
    """load_interaction_registry 接受自定义路径（测试隔离用）."""
    # 写一份最小合法 interaction.yaml
    minimal = {
        "registry": "interaction",
        "contract_version": "0.0.0-test",
        "status": "frozen-candidate",
        "source_sections": ["test"],
        "required_fields": [
            "id", "name", "status", "summary", "response_schema",
            "render_component", "paper_spec", "scoring_input",
            "compatible_scorers",
        ],
        "types": [
            {
                "id": "test_x",
                "name": "测试",
                "status": "active",
                "summary": "x",
                "response_schema": {"type": "object", "required": ["x"]},
                "render_component": "platform/X",
                "paper_spec": "x",
                "scoring_input": "x",
                "compatible_scorers": ["exact_match"],
            },
        ],
    }
    p = tmp_path / "interaction.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(minimal, f, allow_unicode=True)
    reg = load_interaction_registry(p)
    assert reg.contract_version == "0.0.0-test"
    assert len(reg.types) == 1
    assert reg.list_active()[0].id == "test_x"


def test_load_scorer_registry_accepts_custom_path(tmp_path: Path):
    """load_scorer_registry 接受自定义路径（测试隔离用）."""
    minimal = {
        "registry": "scorer",
        "contract_version": "0.0.0-test",
        "status": "frozen-candidate",
        "source_sections": ["test"],
        "unified_contract": {
            "signature": "score(...)",
            "inputs": {},
            "output_schema": {"type": "object", "required": []},
        },
        "required_fields": [
            "id", "name", "status", "deterministic",
            "input_contract", "params_schema", "notes",
        ],
        "scorers": [
            {
                "id": "test_s",
                "name": "测试",
                "status": "active",
                "deterministic": True,
                "summary": "x",
                "input_contract": "x",
                "params_schema": {"type": "object"},
                "notes": "x",
            },
        ],
    }
    p = tmp_path / "scorer.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(minimal, f, allow_unicode=True)
    reg = load_scorer_registry(p)
    assert reg.contract_version == "0.0.0-test"
    assert len(reg.scorers) == 1
    assert reg.list_active()[0].id == "test_s"
