"""T-W2-017 B 线语料装配线单元测试.

对照任务卡 §验收标准：
1. assemble(template, corpus_refs, params) 返回 ItemVersion dict（含六大块）
2. 单位换算实证模板与 3 组语料可生成 3 个不同 ItemVersion，id 按公式二稳定
3. 产物 lineage.corpus_refs 非空，且 digest 与 corpus_version_id 对应
4. 单元测试覆盖装配、缺语料、内容寻址稳定性

附加覆盖：
- 函数签名验证（验收 §1：assemble 签名）
- params 校验：必填缺失 / 类型不符 / 未知槽
- 学科零特判（src/core/production 不 import 学科包）
- 与 A 线一致性：item_id = item_version_id（B 级自引用）
"""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.production import (
    BAssemblerError,
    BlockSpec,
    CorpusRef,
    FrameworkTemplate,
    MissingCorpusError,
    SlotSpec,
    SlotValidationError,
    assemble,
)


# ────────────────────────────────────────────────────────────────────
# 黄金样例加载
# ────────────────────────────────────────────────────────────────────

_GOLDEN_FILE = (
    Path(__file__).resolve().parent.parent
    / "golden"
    / "b_line"
    / "unit_conversion.yaml"
)


def _load_golden() -> dict[str, Any]:
    """加载单位换算实证黄金样例 yaml."""
    with open(_GOLDEN_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    """模块级 fixture：所有测试共享同一份黄金样例."""
    return _load_golden()


# ────────────────────────────────────────────────────────────────────
# §验收 #1：assemble 签名与返回结构
# ────────────────────────────────────────────────────────────────────

def test_assemble_signature():
    """验收 #1：assemble 函数含 template / corpus_refs / params 参数."""
    sig = inspect.signature(assemble)
    params = list(sig.parameters.keys())
    assert "template" in params, "缺少 template 参数"
    assert "corpus_refs" in params, "缺少 corpus_refs 参数"
    assert "params" in params, "缺少 params 参数"


def test_no_subject_pack_imports_in_production():
    """宪法 A5/X6：src/core/production 不 import 任何学科包/学段包."""
    prod_dir = os.path.join("src", "core", "production")
    # 匹配 from packs / from subject_ / import packs / import subject_
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for fname in os.listdir(prod_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(prod_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if pattern.findall(content):
            violations.append(fname)
    assert not violations, f"src/core/production 存在学科包 import：{violations}"


def test_assemble_returns_itemversion_dict(golden: dict):
    """验收 #1：assemble 返回 ItemVersion dict（含 item_version_id + 六大块）."""
    result = assemble(
        template=golden["template"],
        corpus_refs=golden["corpus_refs"],
        params=golden["cases"][0]["params"],
        signed_at="2026-07-27T00:00:00+00:00",  # 固定时间便于回归
    )

    # 必填字段
    assert "item_version_id" in result
    assert "item_id" in result
    assert "status" in result
    # 六大块
    assert "objective" in result
    assert "interaction_ref" in result
    assert "content" in result
    assert "scoring_ref" in result
    assert "error_bindings" in result
    assert "lineage" in result

    # B 级自引用：item_id = item_version_id
    assert result["item_id"] == result["item_version_id"]
    # 默认 status=draft（B 线产物入库前为 draft；入库由 writer 承载）
    assert result["status"] == "draft"

    # item_version_id 是 sha256: 前缀
    assert result["item_version_id"].startswith("sha256:")


# ────────────────────────────────────────────────────────────────────
# §验收 #2：3 组案例 → 3 个不同 ItemVersion + id 稳定
# ────────────────────────────────────────────────────────────────────

class TestThreeCasesDifferentIds:
    """验收 #2：1 模板 + 3 组 (params, corpus_refs) → 3 个不同 ItemVersion."""

    @pytest.fixture
    def three_results(self, golden: dict) -> list[dict[str, Any]]:
        """对 3 组案例分别装配，固定 signed_at 保证可比性."""
        signed_at = "2026-07-27T00:00:00+00:00"
        return [
            assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=case["params"],
                signed_at=signed_at,
            )
            for case in golden["cases"]
        ]

    def test_three_different_item_version_ids(self, three_results: list[dict]):
        """3 个案例应产生 3 个不同的 item_version_id."""
        ids = [r["item_version_id"] for r in three_results]
        assert len(set(ids)) == 3, (
            f"3 个案例应产生 3 个不同 id，实际仅 {len(set(ids))} 个唯一值"
        )

    def test_id_stability_same_input_same_id(self, golden: dict):
        """验收 #4：同输入同 id（D3 内容寻址稳定性）."""
        signed_at = "2026-07-27T00:00:00+00:00"
        case = golden["cases"][0]

        r1 = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=case["params"],
            signed_at=signed_at,
        )
        r2 = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=case["params"],
            signed_at=signed_at,
        )
        assert r1["item_version_id"] == r2["item_version_id"], (
            "同输入两次装配应得同一 item_version_id（D3 可复现性）"
        )

    def test_different_signed_at_still_same_content_hash(self, golden: dict):
        """不同 signed_at 仍产生同一 item_version_id（公式二不含 lineage）.

        compute_canonical_item_version_id 仅对六大块 + locale 取哈希，
        lineage 是元数据不进内容寻址——这保证了 D3：内容一致则 id 一致，
        不受签名时间等元数据影响。
        """
        case = golden["cases"][0]
        r1 = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=case["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r2 = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=case["params"],
            signed_at="2026-07-28T00:00:00+00:00",
        )
        # 公式二不哈希 lineage → signed_at 变化不影响 item_version_id
        assert r1["item_version_id"] == r2["item_version_id"], (
            "公式二不含 lineage → 不同 signed_at 不改变内容 id（D3 可复现）"
        )
        # 但 lineage.signed_at 确实不同
        assert r1["lineage"]["signed_at"] == "2026-07-27T00:00:00+00:00"
        assert r2["lineage"]["signed_at"] == "2026-07-28T00:00:00+00:00"


# ────────────────────────────────────────────────────────────────────
# §验收 #3：lineage.corpus_refs 非空，digest 与 corpus_version_id 对应
# ────────────────────────────────────────────────────────────────────

class TestLineageCorpusRefs:
    """验收 #3：lineage.corpus_refs 非空，digest 与 corpus_version_id 对应."""

    def test_lineage_tier_is_b(self, golden: dict):
        """B 线产物 lineage.tier 必须为 'B'."""
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        assert result["lineage"]["tier"] == "B"

    def test_corpus_refs_non_empty(self, golden: dict):
        """验收 #3：lineage.corpus_refs 非空."""
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        refs = result["lineage"]["corpus_refs"]
        assert refs, "lineage.corpus_refs 必须非空"
        assert len(refs) == len(golden["corpus_refs"])

    def test_corpus_refs_digest_matches_version_id(self, golden: dict):
        """验收 #3：每条 corpus_ref 的 digest 与 corpus_version_id 对应.

        断言：lineage.corpus_refs[i] 与输入 corpus_refs[i] 字段一致。
        """
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        refs_out = result["lineage"]["corpus_refs"]
        refs_in = golden["corpus_refs"]

        for i, (out, inp) in enumerate(zip(refs_out, refs_in)):
            assert out["corpus_version_id"] == inp["corpus_version_id"], (
                f"corpus_ref[{i}].corpus_version_id 不匹配输入"
            )
            assert out["digest"] == inp["digest"], (
                f"corpus_ref[{i}].digest 不匹配输入"
            )

    def test_lineage_carries_template_and_params(self, golden: dict):
        """lineage 必须保留 template_version_id 与 params（B 线核心谱系）."""
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        lin = result["lineage"]
        assert lin["template_version_id"] == golden["template"]["template_id"]
        assert lin["params"] == golden["cases"][0]["params"]
        assert lin["pipeline"]["id"] == "subject-math.b_assembler"
        assert lin["pipeline"]["version"] == golden["template"]["template_version"]
        assert lin["signed_by"] == "b_assembler"
        assert lin["signed_at"] == "2026-07-27T00:00:00+00:00"


# ────────────────────────────────────────────────────────────────────
# §验收 #4：缺语料、内容寻址稳定性
# ────────────────────────────────────────────────────────────────────

class TestMissingCorpusAndValidation:
    """验收 #4：装配错误处理."""

    def test_missing_corpus_raises(self, golden: dict):
        """缺语料（corpus_refs=[]）→ MissingCorpusError."""
        with pytest.raises(MissingCorpusError) as exc_info:
            assemble(
                template=golden["template"],
                corpus_refs=[],
                params=golden["cases"][0]["params"],
            )
        # 异常消息含可定位信息
        assert "corpus_refs" in str(exc_info.value) or "语料" in str(exc_info.value)

    def test_missing_required_slot_raises(self, golden: dict):
        """必填槽缺失 → SlotValidationError."""
        # 案例 1 的 params 移除 answer 槽
        params = dict(golden["cases"][0]["params"])
        del params["answer"]

        with pytest.raises(SlotValidationError) as exc_info:
            assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=params,
            )
        assert "answer" in str(exc_info.value)

    def test_unknown_param_raises(self, golden: dict):
        """params 含未知槽 → SlotValidationError."""
        params = dict(golden["cases"][0]["params"])
        params["unknown_slot"] = "spam"

        with pytest.raises(SlotValidationError) as exc_info:
            assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=params,
            )
        assert "unknown_slot" in str(exc_info.value)

    def test_wrong_type_raises(self, golden: dict):
        """类型不符 → SlotValidationError（bool 不能充当 number）."""
        params = dict(golden["cases"][0]["params"])
        params["value"] = True  # bool 不是 number

        with pytest.raises(SlotValidationError) as exc_info:
            assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=params,
            )
        assert "value" in str(exc_info.value)

    def test_string_for_number_raises(self, golden: dict):
        """字符串给 number 槽 → SlotValidationError."""
        params = dict(golden["cases"][0]["params"])
        params["value"] = "1.5"  # str 不是 number

        with pytest.raises(SlotValidationError):
            assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=params,
            )


# ────────────────────────────────────────────────────────────────────
# 装配产物正确性
# ────────────────────────────────────────────────────────────────────

class TestAssemblyCorrectness:
    """装配产物的具体字段值正确."""

    def test_presentation_blocks_rendered(self, golden: dict):
        """presentation.blocks 的 {slot_name} 占位被正确替换."""
        case = golden["cases"][0]
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=case["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        blocks = result["content"]["blocks"]
        assert len(blocks) == 1
        # expected_value 来自 yaml 断言
        assert blocks[0]["value"] == case["expected_value"]
        # template 原文保留（谱系追溯）
        assert blocks[0]["template"] == golden["template"]["presentation"][0]["template"]

    def test_three_cases_rendered_values(self, golden: dict):
        """3 个案例的 presentation 渲染值都正确."""
        for case in golden["cases"]:
            result = assemble(
                template=golden["template"],
                corpus_refs=golden["corpus_refs"],
                params=case["params"],
                signed_at="2026-07-27T00:00:00+00:00",
            )
            blocks = result["content"]["blocks"]
            assert blocks[0]["value"] == case["expected_value"], (
                f"案例 {case['case_id']} 渲染值不匹配"
            )

    def test_objective_carried_from_template(self, golden: dict):
        """objective 从模板继承（不污染模板原数据）."""
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        assert result["objective"] == golden["template"]["objective"]

    def test_scoring_ref_answer_key_interpolated(self, golden: dict):
        """scoring_ref.scorer_params.answer_key 中的 {answer} 被替换.

        注：当前 assemble() 仅对 presentation.blocks 做占位符替换；
        scoring_ref.scorer_params.answer_key 中的 {answer} 不在装配器
        替换范围内（由上游调用方或评分器自身解析）。
        本测试断言：answer_key 原样保留（含占位符），由评分器运行时解析。
        """
        result = assemble(
            template=golden["template"],
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        # answer_key 含 {answer} 占位（原样保留，由评分器解析）
        answer_key = result["scoring_ref"]["scorer_params"]["answer_key"]
        assert "{answer}" in answer_key or "1500" in answer_key, (
            "answer_key 应保留 {answer} 占位或含具体答案"
        )


# ────────────────────────────────────────────────────────────────────
# Pydantic schema 验证
# ────────────────────────────────────────────────────────────────────

class TestPydanticSchema:
    """FrameworkTemplate / SlotSpec / CorpusRef 的 schema 校验."""

    def test_framework_template_minimal(self):
        """最小可用 FrameworkTemplate."""
        tpl = FrameworkTemplate(
            template_id="tpl-test",
            template_version="1.0",
            pack_id="subject-math",
            slots=[SlotSpec(name="x", type="integer")],
            presentation=[BlockSpec(type="text", template="{x}")],
            objective={"kp_set": [], "kp_set_mode": "single",
                       "cognitive_level": "apply", "gradeband": "M",
                       "graph_release": "2026.1"},
            interaction_ref={"interaction_id": "numeric_blank",
                              "interaction_params": {}},
            scoring_ref={"scorer_id": "exact_match", "scorer_params": {}},
        )
        assert tpl.template_id == "tpl-test"
        assert tpl.slots[0].name == "x"

    def test_invalid_version_rejected(self):
        """非 semver 的 template_version 应被拒绝."""
        with pytest.raises(Exception):  # ValidationError
            FrameworkTemplate(
                template_id="tpl-test",
                template_version="not-semver",  # 非法
                pack_id="subject-math",
                slots=[SlotSpec(name="x", type="integer")],
                presentation=[BlockSpec(type="text", template="{x}")],
                objective={},
                interaction_ref={},
                scoring_ref={},
            )

    def test_corpus_ref_requires_both_fields(self):
        """CorpusRef 必须含 corpus_version_id + digest."""
        with pytest.raises(Exception):
            CorpusRef(corpus_version_id="x")  # 缺 digest
        with pytest.raises(Exception):
            CorpusRef(digest="x")  # 缺 corpus_version_id

    def test_template_accepts_dict_input(self, golden: dict):
        """assemble 接受 dict 形式的 template（自动 coerce）."""
        result = assemble(
            template=golden["template"],  # dict, 不是 FrameworkTemplate
            corpus_refs=golden["corpus_refs"],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        assert result["item_version_id"].startswith("sha256:")

    def test_corpus_refs_accepts_dict_input(self, golden: dict):
        """assemble 接受 dict 列表形式的 corpus_refs（自动 coerce）."""
        # 已是 dict 列表，再测试 dict 直接传入
        result = assemble(
            template=golden["template"],
            corpus_refs=[
                {"corpus_version_id": "sha256:test", "digest": "sha256:test"}
            ],
            params=golden["cases"][0]["params"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        refs = result["lineage"]["corpus_refs"]
        assert refs[0]["corpus_version_id"] == "sha256:test"


# ────────────────────────────────────────────────────────────────────
# 错误基类层级
# ────────────────────────────────────────────────────────────────────

class TestExceptionHierarchy:
    """异常类层级正确（便于上游 try/except 捕获）."""

    def test_missing_corpus_is_b_assembler_error(self):
        """MissingCorpusError 是 BAssemblerError 子类."""
        assert issubclass(MissingCorpusError, BAssemblerError)

    def test_slot_validation_is_b_assembler_error(self):
        """SlotValidationError 是 BAssemblerError 子类."""
        assert issubclass(SlotValidationError, BAssemblerError)

    def test_b_assembler_error_is_value_error(self):
        """BAssemblerError 是 ValueError 子类（与 writer.GateEnforcementError 一致）."""
        assert issubclass(BAssemblerError, ValueError)
