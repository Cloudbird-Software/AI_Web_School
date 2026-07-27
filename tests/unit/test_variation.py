"""T-W2-005 受控变式引擎与 VariantCertificate 单元测试.

验收对照：
  §1 generate_variants 按轴重采样，返回 n 个实例 + VariantCertificate
  §2 VariantCertificate 含 invariant_evidence + operator_id
  §3 对 objective 依赖变更槽的变式，拒绝发证并标记 UNPROVEN
  §4 单元测试覆盖合法发证、拒绝发证、AI 自由改写标记三种情况
  §5 不 import 任何学科包/学段包
"""
from __future__ import annotations

from typing import Any

import pytest

from src.core.instantiation.variation import (
    VariantCertificate,
    compute_objective_signature,
    generate_variants,
    mark_ai_free_rewrite,
)
from src.core.instantiation.variation.certificate import (
    CONTROLLED_VARIATION_OPERATOR,
)
from src.core.instantiation.engine import instantiate


# ────────────────────────────────────────────────────────────────────
# 测试夹具
# ────────────────────────────────────────────────────────────────────

def _make_template(
    *,
    slots: dict[str, dict[str, Any]],
    variation_axes: list[dict[str, Any]],
    expression: str = "a + b",
    template_version_id: str = "sha256:fixture-template-variation-test",
    template_id: str = "tpl-variation-test",
) -> dict[str, Any]:
    """构造测试用母题版本 dict.

    默认 slots 含 a/b（int, difficulty_relevant=True）+ 可选额外槽；
    expression 默认 "a + b"；variation_axes 由参数传入。
    """
    return {
        "template_version_id": template_version_id,
        "template_id": template_id,
        "dsl_version": "1",
        "spec": {
            "objective": {
                "kp_set": [{"dimension": "kp", "code": "math.nal.int.add"}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": "L",
                "graph_release": "2026.1",
            },
            "slots": slots,
            "variation_axes": {"axes": variation_axes},
            "presentation": {
                "blocks": [{"kind": "text", "template": "{a} + {b} = ?"}]
            },
            "answer_program": {"expression": expression, "returns": "number"},
            "distractor_rules": {"rules": []},
        },
    }


def _base_template_with_c() -> dict[str, Any]:
    """三槽母题：a/b 可变（轴内），c 冻结。表达式 a + b."""
    return _make_template(
        slots={
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
            "c": {"type": "int", "difficulty_relevant": False},
        },
        variation_axes=[
            {"axis_id": "numbers", "slots": ["a", "b"]},
        ],
    )


def _two_slot_template() -> dict[str, Any]:
    """两槽母题：a/b 全在轴内（全槽变式 → 拒绝发证）。"""
    return _make_template(
        slots={
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
        },
        variation_axes=[
            {"axis_id": "all", "slots": ["a", "b"]},
        ],
    )


def _choice_in_expr_template() -> dict[str, Any]:
    """含 choice 槽进表达式的母题（choice 槽进表达式 → 拒绝发证）.

    expression 引用 a/b/op 三个槽名；op 是 choice 类型，出现在表达式中
    触发 choice-in-expr 检测规则（choice 槽在表达式中通常选择运算类型）。
    注意：此表达式在运行时不可求值（int+str），但 objective 依赖检测在
    实例化之前执行，检测命中即返回 UNPROVEN，不会到达求值阶段。
    """
    return _make_template(
        slots={
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
            "op": {
                "type": "choice",
                "difficulty_relevant": False,
                "choices": ["add", "sub"],
            },
        },
        variation_axes=[
            {"axis_id": "op-axis", "slots": ["op"]},
        ],
        # op 作为变量引用出现在表达式中（触发 choice-in-expr 规则）
        expression="a + b + op",
    )


_BASE_PARAMS_3SLOT = {"a": 3, "b": 4, "c": 10}
_BASE_PARAMS_2SLOT = {"a": 3, "b": 4}
_BASE_PARAMS_CHOICE = {"a": 3, "b": 4, "op": "add"}


# ────────────────────────────────────────────────────────────────────
# §2 VariantCertificate 结构校验
# ────────────────────────────────────────────────────────────────────

class TestVariantCertificateStructure:
    """验收 §2：VariantCertificate 含 invariant_evidence + operator_id."""

    def test_legal_certification_has_required_fields(self):
        """合法发证：certified=True，含所有必填字段。"""
        variants, cert = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=3,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
            scorer_params={"answer": 7},
        )
        assert cert.certified is True
        assert cert.operator_id == CONTROLLED_VARIATION_OPERATOR
        assert cert.axis_id == "numbers"
        assert cert.certificate_id.startswith("sha256:")
        assert len(cert.variant_ids) == 3
        # invariant_evidence 必含 5 个键
        ev = cert.invariant_evidence
        assert "objective_signature" in ev
        assert "kp_set_unchanged" in ev
        assert "skill_set_unchanged" in ev
        assert "axis_slots" in ev
        assert "frozen_slots" in ev
        # 合法发证：不变性证据全为 True
        assert ev["kp_set_unchanged"] is True
        assert ev["skill_set_unchanged"] is True
        assert ev["objective_signature"].startswith("sha256:")
        # 轴槽与冻结槽正确
        assert ev["axis_slots"] == ["a", "b"]
        assert ev["frozen_slots"] == ["c"]


# ────────────────────────────────────────────────────────────────────
# §1 + §4-Case1 合法发证
# ────────────────────────────────────────────────────────────────────

class TestLegalCertification:
    """验收 §1 / §4：合法发证场景。"""

    def test_generate_n_variants(self):
        """生成 n 个变式，每个 item_version_id 不同（参数不同）。"""
        variants, cert = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=3,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
            scorer_params={"answer": 7},
        )
        assert len(variants) == 3
        # 3 个变式的 item_version_id 各不相同
        ids = [v.item_version_id for v in variants]
        assert len(set(ids)) == 3
        # 证书 variant_ids 与变式 id 一致
        assert cert.variant_ids == ids

    def test_frozen_slot_unchanged_across_variants(self):
        """冻结槽 c 在所有变式中保持基准值。"""
        variants, _ = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=2,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        for v in variants:
            # lineage.params.normalized 应含 c=10（冻结）
            assert v.lineage["params"]["normalized"]["c"] == 10

    def test_axis_slot_varies_across_variants(self):
        """轴槽 a/b 在变式中取不同值。"""
        variants, _ = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=3,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        a_values = [
            v.lineage["params"]["normalized"]["a"] for v in variants
        ]
        # 默认采样器：a = base + index + 1 → 3+1=4, 3+2=5, 3+3=6
        assert a_values == [4, 5, 6]

    def test_deterministic_same_input_same_output(self):
        """同一输入两次调用必得同一变式集 + 同一证书。"""
        kwargs = dict(
            template_version=_base_template_with_c(),
            axis_id="numbers",
            n=2,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        v1, c1 = generate_variants(**kwargs)
        v2, c2 = generate_variants(**kwargs)
        assert [v.item_version_id for v in v1] == [v.item_version_id for v in v2]
        assert c1.certificate_id == c2.certificate_id

    def test_custom_sampler(self):
        """自定义采样器：所有变式 a=100。"""
        def fixed_sampler(slot_name, slot, base_value, index):
            if slot_name == "a":
                return 100
            if slot_name == "b":
                return 200
            return base_value

        variants, cert = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=2,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
            sampler=fixed_sampler,
        )
        for v in variants:
            assert v.lineage["params"]["normalized"]["a"] == 100
            assert v.lineage["params"]["normalized"]["b"] == 200
        assert cert.certified is True


# ────────────────────────────────────────────────────────────────────
# §3 + §4-Case2 拒绝发证
# ────────────────────────────────────────────────────────────────────

class TestRejectCertification:
    """验收 §3 / §4：objective 依赖槽被变更 → 拒绝发证。"""

    def test_all_slots_varied_rejects(self):
        """全槽变式（无冻结槽）→ 拒绝发证，返回空变式列表。"""
        variants, cert = generate_variants(
            _two_slot_template(),
            axis_id="all",
            n=2,
            base_params=_BASE_PARAMS_2SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        assert variants == []
        assert cert.certified is False
        assert cert.is_unproven is True
        assert "objective 依赖" in cert.reason or "UNPROVEN" in cert.reason

    def test_choice_slot_in_expression_rejects(self):
        """choice 槽进表达式 → 拒绝发证。"""
        variants, cert = generate_variants(
            _choice_in_expr_template(),
            axis_id="op-axis",
            n=2,
            base_params=_BASE_PARAMS_CHOICE,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        assert variants == []
        assert cert.certified is False
        assert cert.is_unproven is True

    def test_reject_certificate_has_invariant_evidence(self):
        """拒绝发证的证书仍含 invariant_evidence（kp_set_unchanged=False）。"""
        _, cert = generate_variants(
            _two_slot_template(),
            axis_id="all",
            n=2,
            base_params=_BASE_PARAMS_2SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        ev = cert.invariant_evidence
        assert ev["kp_set_unchanged"] is False
        assert ev["skill_set_unchanged"] is False
        assert ev["objective_signature"].startswith("sha256:")


# ────────────────────────────────────────────────────────────────────
# §4-Case3 AI 自由改写标记
# ────────────────────────────────────────────────────────────────────

class TestAIFreeRewrite:
    """验收 §4：AI 自由改写 → 永远 UNPROVEN。"""

    def test_ai_rewrite_marks_unproven(self):
        """AI 自由改写产出的 ItemVersion 标记为 UNPROVEN。"""
        # 先用引擎实例化一个基准 ItemVersion（模拟 AI 改写的产物）
        result = instantiate(
            _base_template_with_c(),
            _BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
            scorer_params={"answer": 7},
        )
        cert = mark_ai_free_rewrite(
            result,
            ai_operator_id="ai:deepseek-v3",
        )
        assert cert.certified is False
        assert cert.is_unproven is True
        assert cert.operator_id == "ai:deepseek-v3"
        assert cert.variant_ids == [result.item_version_id]
        assert "AI 自由改写" in cert.reason or "UNPROVEN" in cert.reason

    def test_ai_rewrite_no_axis(self):
        """AI 自由改写默认无变式轴。"""
        result = instantiate(
            _base_template_with_c(),
            _BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        cert = mark_ai_free_rewrite(result, ai_operator_id="ai:test")
        assert cert.axis_id == ""

    def test_ai_rewrite_with_known_objective(self):
        """AI 改写时可传入已知 objective 签名（用于审计留痕）。"""
        result = instantiate(
            _base_template_with_c(),
            _BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        obj_sig = compute_objective_signature(
            _base_template_with_c()["spec"]["objective"]
        )
        cert = mark_ai_free_rewrite(
            result,
            ai_operator_id="ai:test",
            objective_signature=obj_sig,
        )
        assert cert.invariant_evidence["objective_signature"] == obj_sig
        # 即便有签名，AI 改写仍为 UNPROVEN（无法证明不变）
        assert cert.certified is False


# ────────────────────────────────────────────────────────────────────
# 边界与错误路径
# ────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """边界条件与错误路径。"""

    def test_unknown_axis_raises(self):
        """不存在的 axis_id 抛 ValueError。"""
        with pytest.raises(ValueError, match="不存在"):
            generate_variants(
                _base_template_with_c(),
                axis_id="nonexistent",
                n=1,
                base_params=_BASE_PARAMS_3SLOT,
                pack_digest="sha256:pack-math-fixture",
                interaction_id="single_choice",
                scorer_id="exact_match",
            )

    def test_n_zero_raises(self):
        """n=0 抛 ValueError。"""
        with pytest.raises(ValueError, match="正整数"):
            generate_variants(
                _base_template_with_c(),
                axis_id="numbers",
                n=0,
                base_params=_BASE_PARAMS_3SLOT,
                pack_digest="sha256:pack-math-fixture",
                interaction_id="single_choice",
                scorer_id="exact_match",
            )

    def test_missing_base_param_raises(self):
        """基准参数缺槽抛 ValueError。"""
        with pytest.raises(ValueError, match="缺少槽"):
            generate_variants(
                _base_template_with_c(),
                axis_id="numbers",
                n=1,
                base_params={"a": 3, "b": 4},  # 缺 c
                pack_digest="sha256:pack-math-fixture",
                interaction_id="single_choice",
                scorer_id="exact_match",
            )

    def test_single_variant_certified(self):
        """n=1 也能正常发证。"""
        variants, cert = generate_variants(
            _base_template_with_c(),
            axis_id="numbers",
            n=1,
            base_params=_BASE_PARAMS_3SLOT,
            pack_digest="sha256:pack-math-fixture",
            interaction_id="single_choice",
            scorer_id="exact_match",
        )
        assert len(variants) == 1
        assert cert.certified is True


# ────────────────────────────────────────────────────────────────────
# §5 学科无关校验
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """验收 §5：不 import 任何学科包/学段包。"""

    def test_variation_module_does_not_import_subject_packs(self):
        """variation 子包不 import 任何 subject/grade 包。"""
        import src.core.instantiation.variation as var_pkg
        import inspect

        source = inspect.getsource(var_pkg)
        # 检查 certificate 与 engine 源码
        from src.core.instantiation.variation import certificate, engine
        cert_src = inspect.getsource(certificate)
        eng_src = inspect.getsource(engine)
        combined = source + cert_src + eng_src
        # 不应出现 subject_pack / gradeband_pack / 学科包导入
        forbidden = [
            "import subject",
            "import gradeband",
            "from subject",
            "from gradeband",
            "import packs",
        ]
        for token in forbidden:
            assert token not in combined, f"variation 模块不得包含 {token!r}"
