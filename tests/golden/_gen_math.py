"""T-W2-023 数学黄金母题生成器（临时工具，不作为 deliverable）.

定义 30 个数学 3-4 年级母题（覆盖计算/填空/比较/单位换算），
调用实例化引擎计算 expected_item_version_id 与 expected_content_snapshot，
输出符合 tests/golden/schema.yaml 的 YAML 文件到 tests/golden/items/math/。

为什么需要生成器：
  - expected_item_version_id 必须由实际引擎实例化确认后固定（验收 §2），
    手算易错；
  - expected_content_snapshot 必须与实际 content 逐字节一致；
  - 30 题手写 expected 字段不现实，用生成器保证一致性.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# 让脚本能 import 项目 src
# _gen_math.py → tests/golden/ → tests/ → worktree root（src/ 在此）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.instantiation.engine import instantiate  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "items" / "math"
PACK_DIGEST = "sha256:" + hashlib.sha256(b"subject-math").hexdigest()


def _tvid(template_id: str, spec: dict[str, Any]) -> str:
    """计算 template_version_id = sha256(canonical(template_id, dsl_version, spec))."""
    payload = json.dumps(
        {"template_id": template_id, "dsl_version": "1", "spec": spec},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _obj(kp_code: str, cognitive: str = "apply", gradeband: str = "L") -> dict[str, Any]:
    """构造 objective 块（单一知识点，3-4 年级默认 L 段）."""
    return {
        "kp_set": [{"dimension": "kp", "code": kp_code}],
        "kp_set_mode": "single",
        "cognitive_level": cognitive,
        "gradeband": gradeband,
        "graph_release": "2026.1",
    }


def _text_block(template: str) -> dict[str, Any]:
    return {"blocks": [{"kind": "text", "template": template}]}


def _slot_int(name: str, dr: bool = True) -> dict[str, Any]:
    return {name: {"type": "int", "difficulty_relevant": dr}}


def _det_rule(eid: str, expr: str, label: str) -> dict[str, Any]:
    return {"rule_type": "deterministic", "error_type_id": eid, "expression": expr, "label": label}


def gen(case: dict[str, Any]) -> dict[str, Any]:
    """生成单条黄金用例的完整 YAML dict（含 expected_*）."""
    spec = case["spec"]
    tvid = _tvid(case["template_id"], spec)
    tv = {
        "template_version_id": tvid,
        "template_id": case["template_id"],
        "dsl_version": "1",
        "spec": spec,
    }
    result = instantiate(
        tv,
        case["params"],
        pack_digest=PACK_DIGEST,
        interaction_id=case["interaction_id"],
        scorer_id=case["scorer_id"],
        scorer_params=case["scorer_params"],
        locale="zh-CN",
        corpus_digests=[],
        seed=0,
    )
    return {
        "case_id": case["case_id"],
        "description": case["description"],
        "template_version": tv,
        "params": case["params"],
        "pack_digest": PACK_DIGEST,
        "interaction_id": case["interaction_id"],
        "scorer_id": case["scorer_id"],
        "scorer_params": case["scorer_params"],
        "locale": "zh-CN",
        "corpus_digests": [],
        "seed": 0,
        "expected_item_version_id": result.item_version_id,
        "expected_content_snapshot": result.content,
    }


# ────────────────────────────────────────────────────────────────────
# 30 题定义
# ────────────────────────────────────────────────────────────────────
CASES: list[dict[str, Any]] = [
    # 1-8: single_choice 计算/比较
    {
        "case_id": "sc_int_add",
        "description": "单选——整数加法",
        "template_id": "tpl-sc-int-add",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "68"},
        "spec": {
            "objective": _obj("math.nal.int.add"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} + {b} = ?"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.calc.add.off-by-one", "a + b + 1", "多1"),
                _det_rule("err.calc.add.minus-one", "a + b - 1", "少1"),
                _det_rule("err.calc.add.swap", "a + b + 10", "进位错"),
            ]},
        },
        "params": {"a": 23, "b": 45},
    },
    {
        "case_id": "sc_int_sub",
        "description": "单选——整数减法",
        "template_id": "tpl-sc-int-sub",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "44"},
        "spec": {
            "objective": _obj("math.nal.int.sub"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} - {b} = ?"),
            "answer_program": {"expression": "a - b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.calc.sub.off-by-one", "a - b + 1", "多1"),
                _det_rule("err.calc.sub.minus-one", "a - b - 1", "少1"),
                _det_rule("err.calc.sub.add", "a + b", "加减混"),
            ]},
        },
        "params": {"a": 78, "b": 34},
    },
    {
        "case_id": "sc_int_mul",
        "description": "单选——整数乘法",
        "template_id": "tpl-sc-int-mul",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "42"},
        "spec": {
            "objective": _obj("math.nal.int.mul"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} × {b} = ?"),
            "answer_program": {"expression": "a * b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.calc.mul.add", "a + b", "加代乘"),
                _det_rule("err.calc.mul.off-by-one", "a * b + 1", "多1"),
                _det_rule("err.calc.mul.minus-one", "a * b - 1", "少1"),
            ]},
        },
        "params": {"a": 6, "b": 7},
    },
    {
        "case_id": "sc_int_div",
        "description": "单选——整数除法",
        "template_id": "tpl-sc-int-div",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "7"},
        "spec": {
            "objective": _obj("math.nal.int.div"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} ÷ {b} = ?"),
            "answer_program": {"expression": "a // b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.calc.div.mul", "a * b", "乘代除"),
                _det_rule("err.calc.div.add", "a + b", "加代除"),
                _det_rule("err.calc.div.off-by-one", "a // b + 1", "多1"),
            ]},
        },
        "params": {"a": 56, "b": 8},
    },
    {
        "case_id": "sc_equiv_fraction",
        "description": "单选——等价分数（1/2 = ?/6）",
        "template_id": "tpl-sc-equiv-frac",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "3"},
        "spec": {
            "objective": _obj("math.nal.fraction.equivalent"),
            "slots": {"num": {"type": "int", "difficulty_relevant": True},
                      "den": {"type": "int", "difficulty_relevant": True},
                      "target_den": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{num}/{den} = ? / {target_den}"),
            "answer_program": {"expression": "num * target_den // den", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.frac.equiv.plus_one", "num * target_den // den + 1", "多1"),
                _det_rule("err.frac.equiv.minus_one", "num * target_den // den - 1", "少1"),
                _det_rule("err.frac.equiv.add_num", "num + target_den", "相加"),
            ]},
        },
        "params": {"num": 1, "den": 2, "target_den": 6},
    },
    {
        "case_id": "sc_decimal_sub",
        "description": "单选——小数减法",
        "template_id": "tpl-sc-dec-sub",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "0.5"},
        "spec": {
            "objective": _obj("math.nal.decimal.sub"),
            "slots": {"a": {"type": "decimal", "difficulty_relevant": True},
                      "b": {"type": "decimal", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} - {b} = ?"),
            "answer_program": {"expression": "a - b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.dec.sub.add", "a + b", "加减混"),
                _det_rule("err.dec.sub.b", "b", "选了减数"),
                _det_rule("err.dec.sub.a", "a", "选了被减数"),
            ]},
        },
        "params": {"a": "0.8", "b": "0.3"},
    },
    {
        "case_id": "sc_multiple_relation",
        "description": "单选——倍数关系",
        "template_id": "tpl-sc-multiple",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "3"},
        "spec": {
            "objective": _obj("math.nal.int.multiple"),
            "slots": {**_slot_int("big"), **_slot_int("small")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{big} 是 {small} 的几倍？"),
            "answer_program": {"expression": "big // small", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.multiple.add", "big + small", "相加"),
                _det_rule("err.multiple.sub", "big - small", "相减"),
                _det_rule("err.multiple.mul", "big * small", "相乘"),
            ]},
        },
        "params": {"big": 12, "small": 4},
    },
    {
        "case_id": "sc_shopping_change",
        "description": "单选——购物找零应用题",
        "template_id": "tpl-sc-shopping",
        "interaction_id": "single_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "18"},
        "spec": {
            "objective": _obj("math.nal.int.sub", "apply"),
            "slots": {"pay": {"type": "int", "difficulty_relevant": True},
                      "price": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("小明付了 {pay} 元，买文具花了 {price} 元，应找回多少元？"),
            "answer_program": {"expression": "pay - price", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.shop.add", "pay + price", "相加"),
                _det_rule("err.shop.off-by-one", "pay - price + 1", "多1"),
                _det_rule("err.shop.mul", "pay * price", "相乘"),
            ]},
        },
        "params": {"pay": 50, "price": 32},
    },
    # 9-18: numeric_blank 计算/单位换算
    {
        "case_id": "nb_int_add",
        "description": "数值填空——整数加法",
        "template_id": "tpl-nb-int-add",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "63"},
        "spec": {
            "objective": _obj("math.nal.int.add"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} + {b} = （  ）"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 36, "b": 27},
    },
    {
        "case_id": "nb_int_sub",
        "description": "数值填空——整数减法",
        "template_id": "tpl-nb-int-sub",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "47"},
        "spec": {
            "objective": _obj("math.nal.int.sub"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} - {b} = （  ）"),
            "answer_program": {"expression": "a - b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 92, "b": 45},
    },
    {
        "case_id": "nb_int_mul",
        "description": "数值填空——整数乘法",
        "template_id": "tpl-nb-int-mul",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "72"},
        "spec": {
            "objective": _obj("math.nal.int.mul"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} × {b} = （  ）"),
            "answer_program": {"expression": "a * b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 8, "b": 9},
    },
    {
        "case_id": "nb_int_div",
        "description": "数值填空——整数除法",
        "template_id": "tpl-nb-int-div",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "8"},
        "spec": {
            "objective": _obj("math.nal.int.div"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} ÷ {b} = （  ）"),
            "answer_program": {"expression": "a // b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 72, "b": 9},
    },
    {
        "case_id": "nb_unit_m_cm",
        "description": "数值填空——米化厘米（单位换算）",
        "template_id": "tpl-nb-unit-m-cm",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "300"},
        "spec": {
            "objective": _obj("math.meas.unit.convert"),
            "slots": {"m": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{m} 米 = （  ） 厘米"),
            "answer_program": {"expression": "m * 100", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"m": 3},
    },
    {
        "case_id": "nb_unit_kg_g",
        "description": "数值填空——千克化克（单位换算）",
        "template_id": "tpl-nb-unit-kg-g",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "2000"},
        "spec": {
            "objective": _obj("math.meas.unit.convert"),
            "slots": {"kg": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{kg} 千克 = （  ） 克"),
            "answer_program": {"expression": "kg * 1000", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"kg": 2},
    },
    {
        "case_id": "nb_unit_hour_min",
        "description": "数值填空——时化分（单位换算）",
        "template_id": "tpl-nb-unit-h-min",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "60"},
        "spec": {
            "objective": _obj("math.meas.unit.convert"),
            "slots": {"h": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{h} 时 = （  ） 分"),
            "answer_program": {"expression": "h * 60", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"h": 1},
    },
    {
        "case_id": "nb_frac_add",
        "description": "数值填空——同分母分数加法",
        "template_id": "tpl-nb-frac-add",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "1"},
        "spec": {
            "objective": _obj("math.nal.fraction.add"),
            "slots": {"num": {"type": "int", "difficulty_relevant": True},
                      "den": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{num}/{den} + {num}/{den} = （  ）"),
            "answer_program": {"expression": "(num + num) // den", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"num": 3, "den": 4},
    },
    {
        "case_id": "nb_decimal_add",
        "description": "数值填空——小数加法",
        "template_id": "tpl-nb-dec-add",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "0.8"},
        "spec": {
            "objective": _obj("math.nal.decimal.add"),
            "slots": {"a": {"type": "decimal", "difficulty_relevant": True},
                      "b": {"type": "decimal", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{a} + {b} = （  ）"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": "0.5", "b": "0.3"},
    },
    {
        "case_id": "nb_area_rect",
        "description": "数值填空——长方形面积",
        "template_id": "tpl-nb-area-rect",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "20"},
        "spec": {
            "objective": _obj("math.gm.area.rectangle"),
            "slots": {**_slot_int("len"), **_slot_int("wid")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("长方形长 {len} 厘米、宽 {wid} 厘米，面积是（  ）平方厘米"),
            "answer_program": {"expression": "len * wid", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"len": 5, "wid": 4},
    },
    {
        "case_id": "nb_perimeter_square",
        "description": "数值填空——正方形周长",
        "template_id": "tpl-nb-peri-square",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "12"},
        "spec": {
            "objective": _obj("math.gm.perimeter.square"),
            "slots": {"side": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("正方形边长 {side} 厘米，周长是（  ）厘米"),
            "answer_program": {"expression": "side * 4", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"side": 3},
    },
    # 19-22: text_blank 读法/符号/单位
    {
        "case_id": "tb_number_read",
        "description": "文本填空——数字读法",
        "template_id": "tpl-tb-num-read",
        "interaction_id": "text_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "一百二十三"},
        "spec": {
            "objective": _obj("math.nal.int.read", "remember"),
            "slots": {"num": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("数字 {num} 读作（  ）"),
            "answer_program": {"expression": "num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"num": 123},
    },
    {
        "case_id": "tb_money_convert",
        "description": "文本填空——元角换算",
        "template_id": "tpl-tb-money",
        "interaction_id": "text_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "35"},
        "spec": {
            "objective": _obj("math.meas.money.convert"),
            "slots": {"yuan": {"type": "int", "difficulty_relevant": True},
                      "jiao": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("{yuan} 元 {jiao} 角 = （  ） 角"),
            "answer_program": {"expression": "yuan * 10 + jiao", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"yuan": 3, "jiao": 5},
    },
    {
        "case_id": "tb_compare_symbol",
        "description": "文本填空——填写比较符号",
        "template_id": "tpl-tb-compare-symbol",
        "interaction_id": "text_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ">"},
        "spec": {
            "objective": _obj("math.nal.int.compare"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("在 ○ 里填 >、< 或 =：{a} ○ {b}"),
            "answer_program": {"expression": "a - b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 5, "b": 3},
    },
    {
        "case_id": "tb_odd_count",
        "description": "文本填空——奇数个数统计",
        "template_id": "tpl-tb-odd-count",
        "interaction_id": "text_blank",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": "2"},
        "spec": {
            "objective": _obj("math.nal.int.odd", "understand"),
            "slots": {"n1": {"type": "int", "difficulty_relevant": True},
                      "n2": {"type": "int", "difficulty_relevant": True},
                      "n3": {"type": "int", "difficulty_relevant": True},
                      "n4": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("在 {n1}、{n2}、{n3}、{n4} 中，奇数有几个？"),
            "answer_program": {"expression": "(n1 % 2) + (n2 % 2) + (n3 % 2) + (n4 % 2)", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"n1": 2, "n2": 3, "n3": 4, "n4": 5},
    },
    # 23-26: multi_choice 性质判断
    {
        "case_id": "mc_even_pick",
        "description": "多选——选出偶数",
        "template_id": "tpl-mc-even",
        "interaction_id": "multi_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["2", "4", "6"]},
        "spec": {
            "objective": _obj("math.nal.int.even", "understand"),
            "slots": {"a": {"type": "int", "difficulty_relevant": True},
                      "b": {"type": "int", "difficulty_relevant": True},
                      "c": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("下列哪些是偶数？{a}、{b}、{c}"),
            "answer_program": {"expression": "a + b + c", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.even.odd_one", "a - 1", "误选奇数1"),
                _det_rule("err.even.odd_two", "c + 1", "误选奇数7"),
            ]},
        },
        "params": {"a": 2, "b": 4, "c": 6},
    },
    {
        "case_id": "mc_multiple_3",
        "description": "多选——选出3的倍数",
        "template_id": "tpl-mc-mul3",
        "interaction_id": "multi_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["6", "9"]},
        "spec": {
            "objective": _obj("math.nal.int.multiple", "understand"),
            "slots": {"a": {"type": "int", "difficulty_relevant": True},
                      "b": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("下列哪些是 3 的倍数？{a}、{b}"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.mul3.not_one", "a + 1", "误选非倍数7"),
                _det_rule("err.mul3.not_two", "b + 1", "误选非倍数10"),
            ]},
        },
        "params": {"a": 6, "b": 9},
    },
    {
        "case_id": "mc_prime_pick",
        "description": "多选——选出质数",
        "template_id": "tpl-mc-prime",
        "interaction_id": "multi_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["5", "7"]},
        "spec": {
            "objective": _obj("math.nal.int.prime", "understand"),
            "slots": {"a": {"type": "int", "difficulty_relevant": True},
                      "b": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("下列哪些是质数？{a}、{b}"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.prime.composite", "a + 1", "误选合数"),
            ]},
        },
        "params": {"a": 5, "b": 7},
    },
    {
        "case_id": "mc_true_fraction",
        "description": "多选——选出真分数",
        "template_id": "tpl-mc-true-frac",
        "interaction_id": "multi_choice",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["1/3", "2/5"]},
        "spec": {
            "objective": _obj("math.nal.fraction.proper", "understand"),
            "slots": {"a_num": {"type": "int", "difficulty_relevant": True},
                      "a_den": {"type": "int", "difficulty_relevant": True},
                      "b_num": {"type": "int", "difficulty_relevant": True},
                      "b_den": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("下列哪些是真分数？{a_num}/{a_den}、{b_num}/{b_den}"),
            "answer_program": {"expression": "a_num + b_num", "returns": "number"},
            "distractor_rules": {"rules": [
                _det_rule("err.frac.improper", "a_num + 1", "误选假分数"),
            ]},
        },
        "params": {"a_num": 1, "a_den": 3, "b_num": 2, "b_den": 5},
    },
    # 27-28: matching
    {
        "case_id": "match_num_hanzi",
        "description": "匹配——数字与中文配对",
        "template_id": "tpl-match-num-han",
        "interaction_id": "matching",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": {"pairs": [{"left": "1", "right": "一"}, {"left": "2", "right": "二"}]}},
        "spec": {
            "objective": _obj("math.nal.int.recognize", "remember"),
            "slots": {"first_num": {"type": "int", "difficulty_relevant": False},
                      "first_text": {"type": "string", "difficulty_relevant": False},
                      "second_num": {"type": "int", "difficulty_relevant": False},
                      "second_text": {"type": "string", "difficulty_relevant": False}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("把数字与对应的中文连线：{first_num}—?, {second_num}—?"),
            "answer_program": {"expression": "first_num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"first_num": 1, "first_text": "一", "second_num": 2, "second_text": "二"},
    },
    {
        "case_id": "match_expr_result",
        "description": "匹配——算式与结果配对",
        "template_id": "tpl-match-expr-res",
        "interaction_id": "matching",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": {"pairs": [{"left": "2+3", "right": "5"}, {"left": "4×2", "right": "8"}]}},
        "spec": {
            "objective": _obj("math.nal.int.add"),
            "slots": {"a": {"type": "int", "difficulty_relevant": False},
                      "b": {"type": "int", "difficulty_relevant": False},
                      "c": {"type": "int", "difficulty_relevant": False},
                      "d": {"type": "int", "difficulty_relevant": False}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("把算式与结果连线：{a}+{b}—?, {c}×{d}—?"),
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 2, "b": 3, "c": 4, "d": 2},
    },
    # 29-30: ordering
    {
        "case_id": "order_ascending",
        "description": "排序——数字从小到大",
        "template_id": "tpl-order-asc",
        "interaction_id": "ordering",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["3", "5", "8"]},
        "spec": {
            "objective": _obj("math.nal.int.compare", "understand"),
            "slots": {"a": {"type": "int", "difficulty_relevant": True},
                      "b": {"type": "int", "difficulty_relevant": True},
                      "c": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("把 {a}、{b}、{c} 从小到大排列"),
            "answer_program": {"expression": "a + b + c", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 8, "b": 3, "c": 5},
    },
    {
        "case_id": "order_calc_result",
        "description": "排序——按计算结果从小到大",
        "template_id": "tpl-order-calc",
        "interaction_id": "ordering",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["2", "6", "12"]},
        "spec": {
            "objective": _obj("math.nal.int.mul", "understand"),
            "slots": {"a": {"type": "int", "difficulty_relevant": True},
                      "b": {"type": "int", "difficulty_relevant": True},
                      "c": {"type": "int", "difficulty_relevant": True}},
            "variation_axes": {"axes": []},
            "presentation": _text_block("把 1×{a}、2×{b}、3×{c} 按结果从小到大排列"),
            "answer_program": {"expression": "a + b + c", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 2, "b": 3, "c": 4},
    },
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        try:
            data = gen(case)
        except Exception as e:
            print(f"❌ {case['case_id']}: {e}", file=sys.stderr)
            raise
        out = OUT_DIR / f"{case['case_id']}.yaml"
        with open(out, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        print(f"✅ {case['case_id']} → {out.name}")
    print(f"\n生成 {len(CASES)} 题到 {OUT_DIR}")


if __name__ == "__main__":
    main()
