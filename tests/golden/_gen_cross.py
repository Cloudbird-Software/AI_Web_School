"""T-W2-024 跨交互黄金母题生成器（临时工具，不作为 deliverable）.

补充 20 个跨交互类型黄金用例，覆盖 4 种新交互（short_answer/
stepwise_process/writing/drawing_operation），与 T-W2-023 合计覆盖
10 种现役交互。

设计要点：
  - short_answer / writing / drawing_operation：answer_program 仅作为
    引擎内部求值占位（无干扰项时 answer_value 不进入 content/id），
    实际评分由 scorer_params（keypoints/rubric/answer）承载。
  - stepwise_process：answer_program 返回最终数值答案；stepwise_rubric
    scorer_params 携带分步评分配置。
  - 所有用例的 expected_* 字段由实例化引擎实际计算后固定（验收 §2）.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# 让脚本能 import 项目 src
# _gen_cross.py → tests/golden/ → tests/ → worktree root（src/ 在此）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.instantiation.engine import instantiate  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "items" / "cross"
PACK_DIGEST = "sha256:" + hashlib.sha256(b"subject-math").hexdigest()


def _tvid(template_id: str, spec: dict[str, Any]) -> str:
    """计算 template_version_id = sha256(canonical(template_id, dsl_version, spec))."""
    payload = json.dumps(
        {"template_id": template_id, "dsl_version": "1", "spec": spec},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _obj(kp_code: str, cognitive: str = "apply", gradeband: str = "L") -> dict[str, Any]:
    """构造 objective 块."""
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


def _slot_str(name: str, dr: bool = False) -> dict[str, Any]:
    return {name: {"type": "string", "difficulty_relevant": dr}}


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
# 20 题定义（覆盖 short_answer/stepwise_process/writing/drawing_operation）
# ────────────────────────────────────────────────────────────────────
CASES: list[dict[str, Any]] = [
    # ── 1-5: short_answer 简答（scorer=keypoint_hit）──
    {
        "case_id": "sa_why_even",
        "description": "简答——为什么 8 是偶数",
        "template_id": "tpl-sa-why-even",
        "interaction_id": "short_answer",
        "scorer_id": "keypoint_hit",
        "scorer_params": {"keypoints": [
            {"text": "能被2整除", "required": True},
            {"text": "除以2", "required": False},
        ]},
        "spec": {
            "objective": _obj("math.nal.int.even", "understand"),
            "slots": {**_slot_int("num")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("为什么 {num} 是偶数？请简答。"),
            "answer_program": {"expression": "num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"num": 8},
    },
    {
        "case_id": "sa_why_prime",
        "description": "简答——为什么 7 是质数",
        "template_id": "tpl-sa-why-prime",
        "interaction_id": "short_answer",
        "scorer_id": "keypoint_hit",
        "scorer_params": {"keypoints": [
            {"text": "只能被1和自身整除", "required": True},
            {"text": "没有其他因数", "required": False},
        ]},
        "spec": {
            "objective": _obj("math.nal.int.prime", "understand"),
            "slots": {**_slot_int("num")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("为什么 {num} 是质数？请简答。"),
            "answer_program": {"expression": "num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"num": 7},
    },
    {
        "case_id": "sa_explain_frac",
        "description": "简答——解释分数大小比较",
        "template_id": "tpl-sa-explain-frac",
        "interaction_id": "short_answer",
        "scorer_id": "keypoint_hit",
        "scorer_params": {"keypoints": [
            {"text": "分子相同", "required": True},
            {"text": "分母小", "required": True},
            {"text": "分数大", "required": False},
        ]},
        "spec": {
            "objective": _obj("math.nal.fraction.compare", "understand"),
            "slots": {**_slot_int("a_num"), **_slot_int("a_den"), **_slot_int("b_den")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("解释为什么 {a_num}/{a_den} > {a_num}/{b_den}？"),
            "answer_program": {"expression": "a_num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a_num": 1, "a_den": 2, "b_den": 3},
    },
    {
        "case_id": "sa_unit_relation",
        "description": "简答——解释米与厘米关系",
        "template_id": "tpl-sa-unit-rel",
        "interaction_id": "short_answer",
        "scorer_id": "keypoint_hit",
        "scorer_params": {"keypoints": [
            {"text": "1米=100厘米", "required": True},
            {"text": "100倍", "required": False},
        ]},
        "spec": {
            "objective": _obj("math.meas.unit.convert", "understand"),
            "slots": {**_slot_int("meters"), **_slot_int("cm_per_m")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("解释 {meters} 米和 {cm_per_m} 厘米的关系。"),
            "answer_program": {"expression": "meters * cm_per_m", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"meters": 1, "cm_per_m": 100},
    },
    {
        "case_id": "sa_area_explain",
        "description": "简答——解释长方形面积公式",
        "template_id": "tpl-sa-area-explain",
        "interaction_id": "short_answer",
        "scorer_id": "keypoint_hit",
        "scorer_params": {"keypoints": [
            {"text": "长乘宽", "required": True},
            {"text": "面积=长×宽", "required": True},
        ]},
        "spec": {
            "objective": _obj("math.gm.area.rectangle", "understand"),
            "slots": {**_slot_int("len"), **_slot_int("wid")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("解释长方形面积为什么是 {len}×{wid}。"),
            "answer_program": {"expression": "len * wid", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"len": 6, "wid": 4},
    },

    # ── 6-10: stepwise_process 分步过程（scorer=stepwise_rubric）──
    {
        "case_id": "sp_two_step_buy",
        "description": "分步——两步计算购物找零",
        "template_id": "tpl-sp-two-step-buy",
        "interaction_id": "stepwise_process",
        "scorer_id": "stepwise_rubric",
        "scorer_params": {"steps": [
            {"step_id": "s1", "scorer": "exact_match", "scorer_params": {"answer": "6"}, "max_score": 5},
            {"step_id": "s2", "scorer": "exact_match", "scorer_params": {"answer": "4"}, "max_score": 5},
        ]},
        "spec": {
            "objective": _obj("math.nal.int.sub", "apply"),
            "slots": {**_slot_int("money"), **_slot_int("count"), **_slot_int("price")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("小明有 {money} 元，买 {count} 个本子每个 {price} 元。第一步：算总价。第二步：算剩余。"),
            "answer_program": {"expression": "money - count * price", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"money": 10, "count": 3, "price": 2},
    },
    {
        "case_id": "sp_verify_div",
        "description": "分步——验证除法",
        "template_id": "tpl-sp-verify-div",
        "interaction_id": "stepwise_process",
        "scorer_id": "stepwise_rubric",
        "scorer_params": {"steps": [
            {"step_id": "s1", "scorer": "exact_match", "scorer_params": {"answer": "120"}, "max_score": 5},
            {"step_id": "s2", "scorer": "exact_match", "scorer_params": {"answer": "24"}, "max_score": 5},
            {"step_id": "s3", "scorer": "exact_match", "scorer_params": {"answer": "144"}, "max_score": 5},
        ]},
        "spec": {
            "objective": _obj("math.nal.int.div", "analyze"),
            "slots": {**_slot_int("dividend"), **_slot_int("divisor"), **_slot_int("quotient")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("验证 {dividend} ÷ {divisor} = {quotient}。分三步：部分商×除数、余数×除数、相加。"),
            "answer_program": {"expression": "dividend", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"dividend": 144, "divisor": 6, "quotient": 24},
    },
    {
        "case_id": "sp_perimeter_area",
        "description": "分步——先周长后面积",
        "template_id": "tpl-sp-peri-area",
        "interaction_id": "stepwise_process",
        "scorer_id": "stepwise_rubric",
        "scorer_params": {"steps": [
            {"step_id": "s1", "scorer": "exact_match", "scorer_params": {"answer": "20"}, "max_score": 5},
            {"step_id": "s2", "scorer": "exact_match", "scorer_params": {"answer": "24"}, "max_score": 5},
        ]},
        "spec": {
            "objective": _obj("math.gm.perimeter.rectangle", "apply"),
            "slots": {**_slot_int("len"), **_slot_int("wid")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("长方形长 {len} 宽 {wid}。第一步：算周长。第二步：算面积。"),
            "answer_program": {"expression": "len * wid", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"len": 6, "wid": 4},
    },
    {
        "case_id": "sp_unit_convert",
        "description": "分步——复合单位换算",
        "template_id": "tpl-sp-unit-convert",
        "interaction_id": "stepwise_process",
        "scorer_id": "stepwise_rubric",
        "scorer_params": {"steps": [
            {"step_id": "s1", "scorer": "exact_match", "scorer_params": {"answer": "2000"}, "max_score": 5},
            {"step_id": "s2", "scorer": "exact_match", "scorer_params": {"answer": "2500"}, "max_score": 5},
        ]},
        "spec": {
            "objective": _obj("math.meas.unit.convert", "apply"),
            "slots": {**_slot_int("km"), **_slot_int("m")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("把 {km} 千米 {m} 米换算成米。第一步：千米化米。第二步：相加。"),
            "answer_program": {"expression": "km * 1000 + m", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"km": 2, "m": 500},
    },
    {
        "case_id": "sp_distrib",
        "description": "分步——分配律",
        "template_id": "tpl-sp-distrib",
        "interaction_id": "stepwise_process",
        "scorer_id": "stepwise_rubric",
        "scorer_params": {"steps": [
            {"step_id": "s1", "scorer": "exact_match", "scorer_params": {"answer": "40"}, "max_score": 5},
            {"step_id": "s2", "scorer": "exact_match", "scorer_params": {"answer": "24"}, "max_score": 5},
            {"step_id": "s3", "scorer": "exact_match", "scorer_params": {"answer": "64"}, "max_score": 5},
        ]},
        "spec": {
            "objective": _obj("math.nal.int.mul", "apply"),
            "slots": {**_slot_int("a"), **_slot_int("b"), **_slot_int("c")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("用分配律算 {a}×({b}+{c})。分三步。"),
            "answer_program": {"expression": "a * (b + c)", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 8, "b": 5, "c": 3},
    },

    # ── 11-15: writing 写作（scorer=ai_rubric）──
    {
        "case_id": "w_explain_commutative",
        "description": "写作——解释乘法交换律",
        "template_id": "tpl-w-commutative",
        "interaction_id": "writing",
        "scorer_id": "ai_rubric",
        "scorer_params": {"rubric": {"dimensions": [
            {"name": "正确性", "max_score": 5, "anchors": "a×b=b×a"},
            {"name": "举例", "max_score": 3, "anchors": "给出具体例子"},
        ]}},
        "spec": {
            "objective": _obj("math.nal.int.mul", "understand"),
            "slots": {**_slot_int("a"), **_slot_int("b")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("用 100 字以内解释乘法交换律，以 {a}×{b} 为例。"),
            "answer_program": {"expression": "a * b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"a": 3, "b": 4},
    },
    {
        "case_id": "w_solve_equation",
        "description": "写作——解方程并写过程",
        "template_id": "tpl-w-solve-eq",
        "interaction_id": "writing",
        "scorer_id": "ai_rubric",
        "scorer_params": {"rubric": {"dimensions": [
            {"name": "步骤", "max_score": 4, "anchors": "移项/合并/化简"},
            {"name": "结果", "max_score": 2, "anchors": "x=5"},
        ]}},
        "spec": {
            "objective": _obj("math.alg.equation.solve", "apply"),
            "slots": {**_slot_int("coef"), **_slot_int("const"), **_slot_int("result")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("解方程 {coef}x + {const} = {result}，写出完整解题过程。"),
            "answer_program": {"expression": "(result - const) // coef", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"coef": 3, "const": 5, "result": 20},
    },
    {
        "case_id": "w_real_division",
        "description": "写作——描述生活中的除法",
        "template_id": "tpl-w-real-div",
        "interaction_id": "writing",
        "scorer_id": "ai_rubric",
        "scorer_params": {"rubric": {"dimensions": [
            {"name": "场景", "max_score": 3, "anchors": "生活化场景"},
            {"name": "算式", "max_score": 3, "anchors": "正确除法算式"},
        ]}},
        "spec": {
            "objective": _obj("math.nal.int.div", "apply"),
            "slots": {**_slot_int("total"), **_slot_int("groups")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("描述一个生活中使用除法的场景（如 {total} 个物品平均分成 {groups} 组）。"),
            "answer_program": {"expression": "total // groups", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"total": 12, "groups": 4},
    },
    {
        "case_id": "w_pattern_observe",
        "description": "写作——观察数列规律",
        "template_id": "tpl-w-pattern",
        "interaction_id": "writing",
        "scorer_id": "ai_rubric",
        "scorer_params": {"rubric": {"dimensions": [
            {"name": "规律", "max_score": 4, "anchors": "每次+3"},
            {"name": "下一个数", "max_score": 2, "anchors": "13"},
        ]}},
        "spec": {
            "objective": _obj("math.nal.seq.pattern", "analyze"),
            "slots": {**_slot_int("first"), **_slot_int("step")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("观察数列 {first}, {first}+{step}, ... 写出规律和下一个数。"),
            "answer_program": {"expression": "first + step", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"first": 1, "step": 3},
    },
    {
        "case_id": "w_explain_true_fraction",
        "description": "写作——解释真分数",
        "template_id": "tpl-w-true-frac",
        "interaction_id": "writing",
        "scorer_id": "ai_rubric",
        "scorer_params": {"rubric": {"dimensions": [
            {"name": "定义", "max_score": 3, "anchors": "分子<分母"},
            {"name": "判断", "max_score": 3, "anchors": "2<5 所以为真分数"},
        ]}},
        "spec": {
            "objective": _obj("math.nal.fraction.proper", "understand"),
            "slots": {**_slot_int("num"), **_slot_int("den")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("解释为什么 {num}/{den} 是真分数。"),
            "answer_program": {"expression": "num", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"num": 2, "den": 5},
    },

    # ── 16-20: drawing_operation 作图操作（scorer=exact_match）──
    {
        "case_id": "do_color_circle",
        "description": "作图——给圆形涂色",
        "template_id": "tpl-do-color",
        "interaction_id": "drawing_operation",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["circle"]},
        "spec": {
            "objective": _obj("math.gm.shape.circle", "remember"),
            "slots": {**_slot_str("shape")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("给图中的{shape}涂上红色。"),
            "answer_program": {"expression": "shape", "returns": "string"},
            "distractor_rules": {"rules": []},
        },
        "params": {"shape": "圆形"},
    },
    {
        "case_id": "do_connect_triangle",
        "description": "作图——连接三点成三角形",
        "template_id": "tpl-do-connect-tri",
        "interaction_id": "drawing_operation",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["A", "B", "C"]},
        "spec": {
            "objective": _obj("math.gm.shape.triangle", "apply"),
            "slots": {**_slot_str("p1"), **_slot_str("p2"), **_slot_str("p3")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("连接 {p1}、{p2}、{p3} 三点形成三角形。"),
            "answer_program": {"expression": "p1", "returns": "string"},
            "distractor_rules": {"rules": []},
        },
        "params": {"p1": "A", "p2": "B", "p3": "C"},
    },
    {
        "case_id": "do_find_right_angle",
        "description": "作图——找出直角",
        "template_id": "tpl-do-right-angle",
        "interaction_id": "drawing_operation",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["angle2"]},
        "spec": {
            "objective": _obj("math.gm.angle.right", "understand"),
            "slots": {**_slot_int("count")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("在图中找出 {count} 个直角并标注。"),
            "answer_program": {"expression": "count", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
        "params": {"count": 1},
    },
    {
        "case_id": "do_complete_symmetry",
        "description": "作图——补全对称图形",
        "template_id": "tpl-do-symmetry",
        "interaction_id": "drawing_operation",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["p1", "p2", "p3"]},
        "spec": {
            "objective": _obj("math.gm.symmetry", "apply"),
            "slots": {**_slot_str("shape")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("补全{shape}的对称图形的另一半。"),
            "answer_program": {"expression": "shape", "returns": "string"},
            "distractor_rules": {"rules": []},
        },
        "params": {"shape": "三角形"},
    },
    {
        "case_id": "do_classify_shapes",
        "description": "作图——选出三角形",
        "template_id": "tpl-do-classify",
        "interaction_id": "drawing_operation",
        "scorer_id": "exact_match",
        "scorer_params": {"answer": ["shape1", "shape3"]},
        "spec": {
            "objective": _obj("math.gm.shape.triangle", "understand"),
            "slots": {**_slot_str("target_shape")},
            "variation_axes": {"axes": []},
            "presentation": _text_block("从图形中选出所有的{target_shape}。"),
            "answer_program": {"expression": "target_shape", "returns": "string"},
            "distractor_rules": {"rules": []},
        },
        "params": {"target_shape": "三角形"},
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
