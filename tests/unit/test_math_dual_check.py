"""T-W2-026 数学双实现验算与可解性采样单元测试.

覆盖任务卡验收 §3 四种情况：
  1. 一致：引擎答案 == SymPy 答案 → pass
  2. 不一致：引擎答案 != SymPy 答案 → fail
  3. 除零：SymPy 检测除零 → review（优雅处理，不崩溃）
  4. 干扰项碰撞：可解性采样检测碰撞 → 报告

验收 §4：验证器已注册为 subject-math 学科验证器。
宪法 X6 反向：测试文件可 import 引擎（比对用），但验证器模块不可。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from src.core.gate.validator import GateContext, get_validator, list_validators

# ────────────────────────────────────────────────────────────────────
# 加载被测模块（subject-math 含连字符，无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
_VALIDATORS_DIR = _ROOT / "src" / "packs" / "subject-math" / "validators"
_DC_PATH = _VALIDATORS_DIR / "dual_check.py"
_SOL_PATH = _VALIDATORS_DIR / "solvability.py"

# 模块名须与 solvability.py 内部 _DC_MODULE_NAME 一致，
# 保证 solvability 加载的 dual_check 与此处加载的是同一实例。
_DC_MODULE_NAME = "subject_math_dual_check"
_SOL_MODULE_NAME = "subject_math_solvability"


def _load_module(path: Path, name: str) -> Any:
    """以 importlib 加载 .py 文件为独立模块.

    已加载则复用 sys.modules 中的实例（关键：保证 solvability.py 内部
    加载的 dual_check 与此处加载的是同一对象）。
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dc = _load_module(_DC_PATH, _DC_MODULE_NAME)
sol = _load_module(_SOL_PATH, _SOL_MODULE_NAME)

DualCheckValidator = dc.DualCheckValidator
SolvabilityValidator = sol.SolvabilityValidator
sample_solvability = sol.sample_solvability
SolvabilityReport = sol.SolvabilityReport


# ────────────────────────────────────────────────────────────────────
# autouse fixture：保证 subject-math 验证器在每个测试前已注册
# ────────────────────────────────────────────────────────────────────
# 为什么需要：test_gate_validator_base.py 的 fixture 调用 reset_registry()
# 清空整个注册表后只恢复 platform.SchemaValidator，subject-math 验证器丢失。
# 模块级 register_validator 只在首次 import 时执行，sys.modules 缓存导致
# 后续 import 不会重新注册。本 fixture 在每个测试前显式重注册，保证
# list_validators('subject-math') 在任何测试顺序下都能拿到 dual_check/solvability。


@pytest.fixture(autouse=True)
def _ensure_subject_math_validators_registered() -> Any:
    """每个测试前重注册 subject-math 验证器，规避 reset_registry 跨测试污染."""
    from src.core.gate.validator import register_validator

    register_validator("subject-math", DualCheckValidator)
    register_validator("subject-math", SolvabilityValidator)
    yield


# ────────────────────────────────────────────────────────────────────
# 辅助：构建最小 spec + 引擎正解计算
# ────────────────────────────────────────────────────────────────────


def _make_spec(
    expression: str = "a + b",
    distractor_rules: list[dict[str, Any]] | None = None,
    slots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构建最小母题 spec dict（六块齐全，可通过 ItemTemplateSpec 校验）."""
    return {
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.test.dual"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": "L",
            "graph_release": "2026.1",
        },
        "slots": slots
        or {
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
        },
        "variation_axes": {"axes": []},
        "presentation": {
            "blocks": [{"kind": "text", "template": "{a} + {b} = ?"}]
        },
        "answer_program": {"expression": expression, "returns": "number"},
        "distractor_rules": {"rules": distractor_rules or []},
    }


def _compute_engine_answer(spec_dict: dict[str, Any], params: dict[str, Any]) -> Any:
    """用实例化引擎的求值路径计算正解.

    测试专用：复用引擎的 _eval_env + evaluate 代码路径产出 engine_answer，
    传给验证器做独立验算。验证器模块本身不共享此代码。
    """
    from src.core.instantiation.dsl.schema import ItemTemplateSpec
    from src.core.instantiation.engine.engine import _eval_env
    from src.core.instantiation.expr import evaluate as engine_evaluate

    spec = ItemTemplateSpec.model_validate(spec_dict)
    eval_env = _eval_env(params, spec.slots)
    return engine_evaluate(spec.answer_program.expression, env=eval_env)


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 1：一致 → pass
# ────────────────────────────────────────────────────────────────────


async def test_dual_check_consistent_pass() -> None:
    """引擎答案 == SymPy 独立重算答案 → pass."""
    spec = _make_spec("a + b")
    params = {"a": 3, "b": 4}
    # 用引擎实际求值路径计算正解（= 7）
    engine_answer = _compute_engine_answer(spec, params)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": engine_answer,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-consistent", ctx)

    assert result.verdict == "pass"
    assert result.validator_id == "dual_check"
    assert result.version == "1.0.0+subject-math"
    assert result.evidence["sympy_answer"] == str(engine_answer)
    # blocking / cost_tier 是 Validator 类属性，非 ValidatorResult 字段
    assert DualCheckValidator.blocking is True
    assert DualCheckValidator.cost_tier == "expensive"


async def test_dual_check_consistent_pass_multiplication() -> None:
    """乘法也一致 → pass（覆盖不同运算符）."""
    spec = _make_spec("a * b")
    params = {"a": 6, "b": 7}
    engine_answer = _compute_engine_answer(spec, params)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": engine_answer,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-mul", ctx)
    assert result.verdict == "pass"


async def test_dual_check_consistent_pass_floor_div() -> None:
    """整除一致 → pass（覆盖 // 运算符的 SymPy floor 转换）."""
    spec = _make_spec("a // b")
    params = {"a": 17, "b": 5}
    engine_answer = _compute_engine_answer(spec, params)  # 3

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": engine_answer,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-floordiv", ctx)
    assert result.verdict == "pass"
    assert result.evidence["sympy_answer"] == "3"


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 2：不一致 → fail
# ────────────────────────────────────────────────────────────────────


async def test_dual_check_inconsistent_fail() -> None:
    """引擎答案 != SymPy 独立重算答案 → fail（阻断）."""
    spec = _make_spec("a + b")
    params = {"a": 3, "b": 4}
    # 故意提供错误答案（正确值 = 7）
    wrong_answer = 999

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": wrong_answer,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-inconsistent", ctx)

    assert result.verdict == "fail"
    assert "不一致" in result.evidence["reason"]
    assert result.evidence["sympy_answer"] == "7"
    assert result.evidence["engine_answer"] == "999"
    assert result.confidence == 1.0  # pydantic Decimal 比较


async def test_dual_check_inconsistent_off_by_one_fail() -> None:
    """差 1 也不一致 → fail."""
    spec = _make_spec("a + b")
    params = {"a": 3, "b": 4}
    engine_answer = _compute_engine_answer(spec, params)  # 7
    wrong_answer = engine_answer + 1  # 8

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": wrong_answer,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-offby1", ctx)
    assert result.verdict == "fail"


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 3：除零 → review（优雅处理）
# ────────────────────────────────────────────────────────────────────


async def test_dual_check_division_by_zero_review() -> None:
    """SymPy 检测到除零 → review（不崩溃，优雅处理）."""
    spec = _make_spec("a / b")
    params = {"a": 6, "b": 0}
    # engine_answer 用占位值（引擎实际会抛错，但验证器独立重算时检测除零）

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": 0,  # 占位
        },
    )
    result = await DualCheckValidator().validate("sha256:test-divzero", ctx)

    assert result.verdict == "review"
    assert "除零" in result.evidence["reason"]
    assert result.confidence == 0.0


async def test_dual_check_floor_division_by_zero_review() -> None:
    """整除除零 // → review."""
    spec = _make_spec("a // b")
    params = {"a": 6, "b": 0}

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": 0,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-floordiv-zero", ctx)
    assert result.verdict == "review"
    assert "除零" in result.evidence["reason"]


async def test_dual_check_modulo_by_zero_review() -> None:
    """取模除零 % → review."""
    spec = _make_spec("a % b")
    params = {"a": 6, "b": 0}

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "params": params,
            "engine_answer": 0,
        },
    )
    result = await DualCheckValidator().validate("sha256:test-mod-zero", ctx)
    assert result.verdict == "review"


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 4：干扰项碰撞 → 可解性采样报告
# ────────────────────────────────────────────────────────────────────


def test_solvability_distractor_collision_reported() -> None:
    """干扰项表达式 == 正解表达式 → 采样报告 distractor_collision."""
    spec = _make_spec(
        "a + b",
        distractor_rules=[
            {
                "rule_type": "deterministic",
                "error_type_id": "err.collision",
                "expression": "a + b",  # 与正解相同 → 必碰撞
            }
        ],
    )
    param_ranges = {"a": [1, 2, 3], "b": [4, 5, 6]}

    report = sample_solvability(spec, param_ranges, sample_count=100, seed=0)

    # 3×3=9 个组合，全部碰撞
    assert report.total_samples == 9
    assert report.degenerate_count == 9
    assert report.degeneration_rate == 1.0

    issue_types = {ex["issue_type"] for ex in report.degenerate_examples}
    assert "distractor_collision" in issue_types

    # 验证样例结构
    example = report.degenerate_examples[0]
    assert "params" in example
    assert "issue_type" in example
    assert "detail" in example
    assert example["issue_type"] == "distractor_collision"


def test_solvability_no_degeneration_pass() -> None:
    """无退化 → degenerate_count=0（对照组）."""
    spec = _make_spec(
        "a + b",
        distractor_rules=[
            {
                "rule_type": "deterministic",
                "error_type_id": "err.add.off-by-one",
                "expression": "a + b + 1",  # 与正解差 1，不碰撞
            },
            {
                "rule_type": "deterministic",
                "error_type_id": "err.add.minus-one",
                "expression": "a + b - 1",  # 与正解差 -1，不碰撞
            },
        ],
    )
    param_ranges = {"a": [1, 2, 3], "b": [4, 5, 6]}

    report = sample_solvability(spec, param_ranges, sample_count=100, seed=0)

    assert report.total_samples == 9
    assert report.degenerate_count == 0
    assert report.degeneration_rate == 0.0
    assert report.degenerate_examples == []


def test_solvability_division_by_zero_detected() -> None:
    """参数空间含 b=0 → 采样报告 division_by_zero."""
    spec = _make_spec("a // b")
    param_ranges = {"a": [6, 12], "b": [0, 2, 3]}

    report = sample_solvability(spec, param_ranges, sample_count=100, seed=0)

    # 2×3=6 个组合，其中 b=0 的 2 个组合除零
    assert report.total_samples == 6
    assert report.degenerate_count == 2
    assert report.degeneration_rate == pytest.approx(2 / 6)

    issue_types = {ex["issue_type"] for ex in report.degenerate_examples}
    assert "division_by_zero" in issue_types


def test_solvability_duplicate_options_detected() -> None:
    """两个干扰项表达式相同 → 选项重复."""
    spec = _make_spec(
        "a + b",
        distractor_rules=[
            {
                "rule_type": "deterministic",
                "error_type_id": "err.dup1",
                "expression": "a + b + 1",
            },
            {
                "rule_type": "deterministic",
                "error_type_id": "err.dup2",
                "expression": "a + b + 1",  # 与 err.dup1 相同 → 重复
            },
        ],
    )
    param_ranges = {"a": [1, 2], "b": [3, 4]}

    report = sample_solvability(spec, param_ranges, sample_count=100, seed=0)

    assert report.total_samples == 4
    assert report.degenerate_count == 4  # 全部重复
    issue_types = {ex["issue_type"] for ex in report.degenerate_examples}
    assert "duplicate_options" in issue_types


def test_solvability_random_sampling_reproducible() -> None:
    """参数空间 > sample_count 时随机采样，同 seed 可复现."""
    spec = _make_spec("a + b")
    param_ranges = {"a": list(range(20)), "b": list(range(20))}  # 400 组合

    r1 = sample_solvability(spec, param_ranges, sample_count=50, seed=42)
    r2 = sample_solvability(spec, param_ranges, sample_count=50, seed=42)

    assert r1.total_samples == 50
    assert r1.total_samples == r2.total_samples
    assert r1.degenerate_count == r2.degenerate_count
    assert r1.degenerate_examples == r2.degenerate_examples


def test_solvability_report_model_validation() -> None:
    """SolvabilityReport 字段约束（extra='forbid', rate ∈ [0,1]）."""
    report = sample_solvability(
        _make_spec("a + b"), {"a": [1], "b": [2]}, sample_count=100, seed=0
    )
    assert isinstance(report, SolvabilityReport)
    assert 0.0 <= report.degeneration_rate <= 1.0
    assert report.degenerate_count <= report.total_samples


# ────────────────────────────────────────────────────────────────────
# 验收 §4：验证器已注册为 subject-math 学科验证器
# ────────────────────────────────────────────────────────────────────


def test_validators_registered_for_subject_math() -> None:
    """dual_check 与 solvability 已注册到 subject-math pack."""
    registered = set(list_validators("subject-math"))
    assert "dual_check" in registered
    assert "solvability" in registered

    v_dc = get_validator("subject-math", "dual_check")
    assert isinstance(v_dc, DualCheckValidator)
    assert v_dc.validator_id == "dual_check"
    assert v_dc.blocking is True
    assert v_dc.cost_tier == "expensive"

    v_sol = get_validator("subject-math", "solvability")
    assert isinstance(v_sol, SolvabilityValidator)
    assert v_sol.validator_id == "solvability"
    assert v_sol.cost_tier == "expensive"


async def test_solvability_validator_pass_no_degeneration() -> None:
    """SolvabilityValidator 无退化 → pass."""
    spec = _make_spec(
        "a + b",
        distractor_rules=[
            {
                "rule_type": "deterministic",
                "error_type_id": "err.x",
                "expression": "a + b + 1",
            }
        ],
    )
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "param_ranges": {"a": [1, 2], "b": [3, 4]},
        },
    )
    result = await SolvabilityValidator().validate("sha256:test-sol-pass", ctx)
    assert result.verdict == "pass"
    assert result.validator_id == "solvability"


async def test_solvability_validator_review_with_degeneration() -> None:
    """SolvabilityValidator 有退化但 <50% → review."""
    spec = _make_spec(
        "a // b",
        distractor_rules=[
            {
                "rule_type": "deterministic",
                "error_type_id": "err.x",
                "expression": "a // b + 1",
            }
        ],
    )
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "spec": spec,
            "param_ranges": {"a": [6, 12], "b": [0, 2, 3, 4]},  # 8 组合，2 个除零
        },
    )
    result = await SolvabilityValidator().validate("sha256:test-sol-review", ctx)
    assert result.verdict == "review"
    assert "退化" in result.evidence["reason"]


# ────────────────────────────────────────────────────────────────────
# 宪法 X6 反向：验证器模块不 import src.core.instantiation
# ────────────────────────────────────────────────────────────────────


def test_no_instantiation_imports_in_validators() -> None:
    """验证器模块不 import src.core.instantiation（双实现独立性）.

    用 AST 解析检查实际 import 语句，而非字符串匹配——避免模块 docstring
    中提及「不 import src.core.instantiation」被误判为违规。
    """
    import ast as _ast

    forbidden_prefixes = (
        "src.core.instantiation",
        "src.instantiation",
    )
    for mod_name, path in [
        (_DC_MODULE_NAME, _DC_PATH),
        (_SOL_MODULE_NAME, _SOL_PATH),
    ]:
        mod = sys.modules.get(mod_name)
        assert mod is not None, f"模块 {mod_name} 未加载"
        src_text = inspect.getsource(mod)
        tree = _ast.parse(src_text)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"{path.name} 含禁用 import：{alias.name!r}"
                        "（双实现独立性要求）"
                    )
            elif isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(forbidden_prefixes), (
                    f"{path.name} 含禁用 from-import：{module!r}"
                    "（双实现独立性要求）"
                )
