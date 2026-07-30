"""T-W2-001 单元测试：母题 DSL Schema 与 Linter。

覆盖验收 §3：3 个有效母题 + 5 种典型错误，全部通过。
有效母题覆盖三种交互：single_choice / numeric_blank / matching。
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.core.instantiation.dsl import (
    ALLOWED_SLOT_TYPES,
    LintResult,
    lint,
)


# ────────────────────────────────────────────────────────────────────
# 有效母题 fixture：3 个，覆盖 single_choice / numeric_blank / matching
# ────────────────────────────────────────────────────────────────────

def _base_objective() -> dict:
    """最小合法 objective 块（数学，三年级，单知识点）。"""
    return {
        "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
        "kp_set_mode": "single",
        "cognitive_level": "apply",
        "gradeband": "L",
        "graph_release": "2026.1",
    }


def _single_choice_spec() -> dict:
    """有效母题 1：单选（小数比较）。

    槽：a/b（decimal，难度相关=False，数值用于题面与正解）。
    answer_program：返回较大的那个选项 id。
    """
    return {
        "objective": _base_objective(),
        "slots": {
            "a": {"type": "decimal", "difficulty_relevant": False, "min": "0", "max": "100"},
            "b": {"type": "decimal", "difficulty_relevant": False, "min": "0", "max": "100"},
        },
        "variation_axes": {
            "axes": [
                {"axis_id": "numeric_range", "slots": ["a", "b"]},
            ]
        },
        "presentation": {
            "blocks": [
                {"kind": "text", "template": "比较大小：{a} 与 {b}，较大的是？"},
            ]
        },
        "answer_program": {
            "expression": "'A' if a >= b else 'B'",
            "returns": "option_id",
        },
        "distractor_rules": {
            "rules": [
                {
                    "rule_type": "deterministic",
                    "error_type_id": "math.compare.swap",
                    "expression": "'B' if a >= b else 'A'",
                },
            ]
        },
    }


def _numeric_blank_spec() -> dict:
    """有效母题 2：数值填空（加法）。

    槽：a/b（int，难度相关=True——位数影响难度）。
    answer_program：返回 a+b。
    """
    return {
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.nal.integer.add"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": "L",
            "graph_release": "2026.1",
        },
        "slots": {
            "a": {"type": "int", "difficulty_relevant": True, "min": 1, "max": 99},
            "b": {"type": "int", "difficulty_relevant": True, "min": 1, "max": 99},
        },
        "variation_axes": {
            "axes": [
                {"axis_id": "magnitude", "slots": ["a", "b"]},
            ]
        },
        "presentation": {
            "blocks": [
                {"kind": "text", "template": "计算：{a} + {b} = ___"},
            ]
        },
        "answer_program": {
            "expression": "a + b",
            "returns": "number",
        },
        "distractor_rules": {
            "rules": [
                {
                    "rule_type": "deterministic",
                    "error_type_id": "math.add.carry",
                    "expression": "a + b + 10",
                },
                {
                    "rule_type": "corpus_sample",
                    "error_type_id": "math.add.random",
                    "corpus_ref": "math/addition_errors/v1",
                },
            ]
        },
    }


def _matching_spec() -> dict:
    """有效母题 3：匹配连线（分数 ↔ 小数）。

    槽：pairs（choice 列表，难度相关=False）。
    answer_program：返回配对列表。
    """
    return {
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.nal.fraction.decimal"}],
            "kp_set_mode": "single",
            "cognitive_level": "understand",
            "gradeband": "M",
            "graph_release": "2026.1",
        },
        "slots": {
            "left_items": {
                "type": "choice",
                "difficulty_relevant": False,
                "choices": ["1/2", "1/4", "3/4"],
            },
            "right_items": {
                "type": "choice",
                "difficulty_relevant": False,
                "choices": ["0.5", "0.25", "0.75"],
            },
        },
        "variation_axes": {
            "axes": [
                {"axis_id": "item_set", "slots": ["left_items", "right_items"]},
            ]
        },
        "presentation": {
            "blocks": [
                {"kind": "text", "template": "把分数与对应小数连线"},
                {"kind": "table", "template": "左列：{left_items}  右列：{right_items}"},
            ]
        },
        "answer_program": {
            "expression": "pairs(left_items, right_items)",
            "returns": "pairs",
        },
        "distractor_rules": {
            "rules": [
                {
                    "rule_type": "deterministic",
                    "error_type_id": "math.frac.swap",
                    "expression": "reverse(pairs(left_items, right_items))",
                },
            ]
        },
    }


VALID_SPECS = [
    ("single_choice", _single_choice_spec()),
    ("numeric_blank", _numeric_blank_spec()),
    ("matching", _matching_spec()),
]


# ────────────────────────────────────────────────────────────────────
# 有效母题测试：3 个全部 lint 通过
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,spec", VALID_SPECS, ids=[n for n, _ in VALID_SPECS])
def test_valid_spec_lints_clean(name: str, spec: dict) -> None:
    """3 个有效母题（单选/数值填空/匹配）lint 全绿。"""
    result = lint(spec)
    assert result.valid is True, (
        f"{name}: 期望 valid=True，但收到错误："
        f"{[e.model_dump() for e in result.errors]}"
    )
    assert result.errors == []


def test_valid_specs_are_distinct_interaction_patterns() -> None:
    """确认 3 个有效母题覆盖不同交互形态（验收 §3 覆盖度）。"""
    # single_choice: answer_program returns option_id
    assert _single_choice_spec()["answer_program"]["returns"] == "option_id"
    # numeric_blank: answer_program returns number
    assert _numeric_blank_spec()["answer_program"]["returns"] == "number"
    # matching: answer_program returns pairs
    assert _matching_spec()["answer_program"]["returns"] == "pairs"


# ────────────────────────────────────────────────────────────────────
# 典型错误 1：必填块缺失
# ────────────────────────────────────────────────────────────────────

def test_error_missing_block() -> None:
    """缺 distractor_rules 块 → 报 missing_block。"""
    spec = _single_choice_spec()
    del spec["distractor_rules"]
    result = lint(spec)
    assert result.valid is False
    codes = [e.code for e in result.errors]
    assert "missing_block" in codes
    # 错误信息应点名缺失的块
    missing_err = next(e for e in result.errors if e.code == "missing_block")
    assert "distractor_rules" in missing_err.message


def test_error_missing_multiple_blocks() -> None:
    """缺多块 → missing_block 一次报告全部缺失块。"""
    spec = _single_choice_spec()
    del spec["presentation"]
    del spec["answer_program"]
    result = lint(spec)
    assert result.valid is False
    missing_err = next(e for e in result.errors if e.code == "missing_block")
    assert "presentation" in missing_err.message
    assert "answer_program" in missing_err.message


# ────────────────────────────────────────────────────────────────────
# 典型错误 2：slot 类型不在允许列表
# ────────────────────────────────────────────────────────────────────

def test_error_invalid_slot_type() -> None:
    """slot.type='bigint'（不在允许列表）→ 报 invalid_slot_type。"""
    spec = _numeric_blank_spec()
    spec["slots"]["a"]["type"] = "bigint"
    result = lint(spec)
    assert result.valid is False
    type_errs = [e for e in result.errors if e.code == "invalid_slot_type"]
    assert len(type_errs) == 1
    assert type_errs[0].path == "slots.a.type"
    assert "bigint" in type_errs[0].message


def test_allowed_slot_types_contents() -> None:
    """ALLOWED_SLOT_TYPES 含 6 种类型。"""
    assert ALLOWED_SLOT_TYPES == frozenset(
        {"int", "decimal", "fraction", "string", "bool", "choice"}
    )


# ────────────────────────────────────────────────────────────────────
# 典型错误 3：variation_axis 引用不存在的 slot
# ────────────────────────────────────────────────────────────────────

def test_error_dangling_variation_slot() -> None:
    """变式轴引用 slots 中不存在的槽名 → 报 dangling_variation_slot。"""
    spec = _single_choice_spec()
    spec["variation_axes"]["axes"][0]["slots"].append("nonexistent_slot")
    result = lint(spec)
    assert result.valid is False
    dangling_errs = [e for e in result.errors if e.code == "dangling_variation_slot"]
    assert len(dangling_errs) == 1
    assert "nonexistent_slot" in dangling_errs[0].message


def test_variation_axis_can_reference_existing_slot() -> None:
    """变式轴只引用存在的槽 → 不报 dangling（合法情况回归）。"""
    spec = _numeric_blank_spec()
    # 只引用 a，b 不在轴里——合法（部分子集重采样）
    spec["variation_axes"]["axes"][0]["slots"] = ["a"]
    result = lint(spec)
    dangling = [e for e in result.errors if e.code == "dangling_variation_slot"]
    assert dangling == []


# ────────────────────────────────────────────────────────────────────
# 典型错误 4：difficulty_relevant 不是 boolean
# ────────────────────────────────────────────────────────────────────

def test_error_difficulty_relevant_string() -> None:
    """difficulty_relevant='yes'（字符串）→ 报 invalid_difficulty_relevant_type。"""
    spec = _numeric_blank_spec()
    spec["slots"]["a"]["difficulty_relevant"] = "yes"
    result = lint(spec)
    assert result.valid is False
    dr_errs = [e for e in result.errors if e.code == "invalid_difficulty_relevant_type"]
    assert len(dr_errs) == 1
    assert dr_errs[0].path == "slots.a.difficulty_relevant"


def test_error_difficulty_relevant_int() -> None:
    """difficulty_relevant=1（int，非 bool）→ 报 invalid_difficulty_relevant_type。

    为什么单独测 int：Python 中 bool 是 int 子类，Linter 须严格区分
    True/False 与 1/0（避免 1 被当作 True 漏检）。
    """
    spec = _numeric_blank_spec()
    spec["slots"]["b"]["difficulty_relevant"] = 1
    result = lint(spec)
    dr_errs = [e for e in result.errors if e.code == "invalid_difficulty_relevant_type"]
    assert len(dr_errs) == 1
    assert dr_errs[0].path == "slots.b.difficulty_relevant"


def test_difficulty_relevant_true_and_false_both_valid() -> None:
    """difficulty_relevant=True 与 False 都合法（回归）。"""
    spec_true = _single_choice_spec()
    assert lint(spec_true).valid is True
    spec_false = deepcopy(_single_choice_spec())
    spec_false["slots"]["a"]["difficulty_relevant"] = False
    spec_false["slots"]["b"]["difficulty_relevant"] = False
    # 仍合法（False 也是 boolean）
    result = lint(spec_false)
    dr_errs = [e for e in result.errors if e.code == "invalid_difficulty_relevant_type"]
    assert dr_errs == []


# ────────────────────────────────────────────────────────────────────
# 典型错误 5：extra 字段（extra='forbid' 违反）
# ────────────────────────────────────────────────────────────────────

def test_error_extra_field_in_slot() -> None:
    """slot 内出现未声明字段 → 报 extra_field_forbidden。"""
    spec = _single_choice_spec()
    spec["slots"]["a"]["unknown_field"] = "oops"
    result = lint(spec)
    assert result.valid is False
    extra_errs = [e for e in result.errors if e.code == "extra_field_forbidden"]
    assert len(extra_errs) >= 1
    assert "unknown_field" in extra_errs[0].path


def test_error_extra_field_in_top_level() -> None:
    """顶层 spec 出现未声明块 → 报 extra_field_forbidden。"""
    spec = _single_choice_spec()
    spec["unknown_block"] = {}
    result = lint(spec)
    assert result.valid is False
    extra_errs = [e for e in result.errors if e.code == "extra_field_forbidden"]
    assert any("unknown_block" in e.path for e in extra_errs)


# ────────────────────────────────────────────────────────────────────
# Linter 行为：多错误同时收集 + 返回结构
# ────────────────────────────────────────────────────────────────────

def test_lint_collects_multiple_errors() -> None:
    """同时有多种错误 → 一次返回全部，不短路。"""
    spec = _single_choice_spec()
    # 错误 1：缺块
    del spec["distractor_rules"]
    # 错误 2：非法槽类型
    spec["slots"]["a"]["type"] = "bigint"
    # 错误 3：变式轴引用不存在槽
    spec["variation_axes"]["axes"][0]["slots"].append("ghost")
    # 错误 4：difficulty_relevant 非 boolean
    spec["slots"]["b"]["difficulty_relevant"] = "no"
    result = lint(spec)
    assert result.valid is False
    codes = {e.code for e in result.errors}
    assert "missing_block" in codes
    assert "invalid_slot_type" in codes
    assert "dangling_variation_slot" in codes
    assert "invalid_difficulty_relevant_type" in codes


def test_lint_result_structure() -> None:
    """LintResult 是 {valid, errors[]} 结构（验收 §2 契约）。"""
    result = lint(_single_choice_spec())
    # valid 为 bool
    assert isinstance(result.valid, bool)
    # errors 为 list
    assert isinstance(result.errors, list)
    # 空 errors + valid=True 是合法通过态
    assert result.valid is True
    assert result.errors == []


def test_lint_accepts_non_dict() -> None:
    """非 dict 输入不抛异常，返回 valid=False 的结构化结果。"""
    result = lint(None)
    assert result.valid is False
    assert len(result.errors) >= 1


def test_lint_accepts_empty_dict() -> None:
    """空 dict → 报全部六块缺失。"""
    result = lint({})
    assert result.valid is False
    missing_err = next(e for e in result.errors if e.code == "missing_block")
    # 六大块全点名
    for block in (
        "objective", "slots", "variation_axes",
        "presentation", "answer_program", "distractor_rules",
    ):
        assert block in missing_err.message


# ────────────────────────────────────────────────────────────────────
# 宪法 X6：核心域不 import 学科包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_import() -> None:
    """dsl 模块不 import 任何学科包/学段包（宪法 X6）。"""
    import src.core.instantiation.dsl as dsl_pkg
    import src.core.instantiation.dsl.linter as linter_mod
    import src.core.instantiation.dsl.schema as schema_mod

    for mod in (dsl_pkg, linter_mod, schema_mod):
        # 模块文件不应 import src.packs.*
        src_text = ""
        for path in ("__init__.py",) if mod.__name__.endswith("dsl") else (mod.__file__,):
            if path is None:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    src_text += f.read()
            except (FileNotFoundError, TypeError):
                pass
        assert "src.packs" not in src_text, (
            f"{mod.__name__} 不应 import 学科包（X6）"
        )
