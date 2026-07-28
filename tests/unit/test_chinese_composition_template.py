"""T-W4-018 作文/看图写话题型模板单元测试.

覆盖任务卡五条验收：
  §1 两个模板均通过 DSL Linter 校验，可被实例化引擎解析。
  §2 实例化产物 interaction_ref 指向开放式作答（writing），scoring_ref 指向 ai_rubric。
  §3 学段参数化正确：低段 50–100 / 中段 150–250 / 高段 300–400。
  §4 make accept 全绿（由 make_accept.sh 覆盖；本文件验模板 + 实例化）。
  §5 核心域零特判：模板位于学科包内，核心域仅通过注册表消费。

实现策略：
  - 模板 YAML 直接按文件路径加载（与 test_chinese_pinyin_to_word.py 一致）；
  - 实例化调用 instantiate()，传入 interaction_id='writing' / scorer_id='ai_rubric'，
    验收②契约对齐 interaction.yaml / scorer.yaml 注册表。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.instantiation.dsl.linter import lint
from src.core.instantiation.engine import instantiate

# ────────────────────────────────────────────────────────────────────
# 路径常量
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACK_DIR = _PROJECT_ROOT / "src" / "packs" / "subject-chinese"
_COMPOSITION_TEMPLATE_PATH = _PACK_DIR / "templates" / "composition.yaml"
_PICTURE_WRITING_TEMPLATE_PATH = _PACK_DIR / "templates" / "picture_writing.yaml"


# ────────────────────────────────────────────────────────────────────
# 加载模板
# ────────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


_COMPOSITION_TEMPLATE = _load_yaml(_COMPOSITION_TEMPLATE_PATH)
_PICTURE_WRITING_TEMPLATE = _load_yaml(_PICTURE_WRITING_TEMPLATE_PATH)


# ────────────────────────────────────────────────────────────────────
# 共享 fixture
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def composition_template() -> dict[str, Any]:
    """返回作文母题模板版本 dict."""
    return _COMPOSITION_TEMPLATE


@pytest.fixture
def picture_writing_template() -> dict[str, Any]:
    """返回看图写话母题模板版本 dict."""
    return _PICTURE_WRITING_TEMPLATE


# ────────────────────────────────────────────────────────────────────
# §1 模板 lint 通过 + 可被实例化引擎解析
# ────────────────────────────────────────────────────────────────────


class TestTemplateLint:
    """验收 §1：两个模板经 DSL Linter 校验通过."""

    def test_composition_lint_passes(self, composition_template: dict) -> None:
        """作文模板 spec 经 lint 返回 valid=True."""
        result = lint(composition_template["spec"])
        assert result.valid, (
            f"composition lint 失败：{[e.model_dump() for e in result.errors]}"
        )

    def test_picture_writing_lint_passes(
        self, picture_writing_template: dict
    ) -> None:
        """看图写话模板 spec 经 lint 返回 valid=True."""
        result = lint(picture_writing_template["spec"])
        assert result.valid, (
            f"picture_writing lint 失败：{[e.model_dump() for e in result.errors]}"
        )

    def test_composition_has_required_top_level_fields(
        self, composition_template: dict
    ) -> None:
        """模板顶层含 template_version_id / template_id / dsl_version / spec."""
        assert composition_template["template_version_id"]
        assert composition_template["template_id"]
        assert composition_template["dsl_version"]
        assert isinstance(composition_template["spec"], dict)

    def test_picture_writing_has_required_top_level_fields(
        self, picture_writing_template: dict
    ) -> None:
        """模板顶层含 template_version_id / template_id / dsl_version / spec."""
        assert picture_writing_template["template_version_id"]
        assert picture_writing_template["template_id"]
        assert picture_writing_template["dsl_version"]
        assert isinstance(picture_writing_template["spec"], dict)

    def test_linter_catches_missing_block(self) -> None:
        """lint 能拦截缺块（反向验证：linter 真的在跑）."""
        bad_spec = {"objective": _COMPOSITION_TEMPLATE["spec"]["objective"]}
        result = lint(bad_spec)
        assert not result.valid
        codes = [e.code for e in result.errors]
        assert "missing_block" in codes


# ────────────────────────────────────────────────────────────────────
# §2 实例化产物 interaction_ref / scoring_ref 指向开放式作答 + ai_rubric
# ────────────────────────────────────────────────────────────────────


class TestInstantiationContract:
    """验收 §2：interaction_ref→writing / scoring_ref→ai_rubric."""

    def test_composition_interaction_and_scoring_refs(
        self, composition_template: dict
    ) -> None:
        """作文实例化产物 interaction_ref=writing, scoring_ref=ai_rubric."""
        result = instantiate(
            composition_template,
            {
                "topic": "春天",
                "word_count_min": 300,
                "word_count_max": 400,
                "time_limit_minutes": 40,
            },
            pack_digest="sha256:pack-subject-chinese-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={
                "rubric": {
                    "dimensions": [
                        {
                            "id": "content",
                            "name": "内容",
                            "anchors": ["主题明确", "主题基本明确", "主题模糊"],
                            "score_bands": [
                                {"level": 1, "label": "优秀", "score": 5},
                                {"level": 2, "label": "合格", "score": 3},
                                {"level": 3, "label": "待改进", "score": 1},
                            ],
                            "error_type_rules": [],
                        }
                    ],
                    "total_max_score": 5,
                }
            },
            signed_at="2026-07-27T00:00:00+00:00",
        )
        d = result.model_dump()
        assert d["interaction_ref"]["interaction_id"] == "writing"
        assert d["scoring_ref"]["scorer_id"] == "ai_rubric"
        # 开放式作答：answer_program=None 返回 None
        assert d["lineage"]["template_version_id"]

    def test_picture_writing_interaction_and_scoring_refs(
        self, picture_writing_template: dict
    ) -> None:
        """看图写话实例化产物 interaction_ref=writing, scoring_ref=ai_rubric."""
        result = instantiate(
            picture_writing_template,
            {
                "picture_ref": "sha256:asset-spring-scene-v1",
                "prompt": "图上画的是什么季节？你在图中看到了什么？",
                "word_count_min": 50,
                "word_count_max": 100,
                "time_limit_minutes": 20,
            },
            pack_digest="sha256:pack-subject-chinese-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={
                "rubric": {
                    "dimensions": [
                        {
                            "id": "content",
                            "name": "内容",
                            "anchors": ["观察细致", "观察基本到位", "观察粗略"],
                            "score_bands": [
                                {"level": 1, "label": "优秀", "score": 5},
                                {"level": 2, "label": "合格", "score": 3},
                                {"level": 3, "label": "待改进", "score": 1},
                            ],
                            "error_type_rules": [],
                        }
                    ],
                    "total_max_score": 5,
                }
            },
            signed_at="2026-07-27T00:00:00+00:00",
        )
        d = result.model_dump()
        assert d["interaction_ref"]["interaction_id"] == "writing"
        assert d["scoring_ref"]["scorer_id"] == "ai_rubric"

    def test_writing_interaction_registered(self) -> None:
        """writing 交互类型已在 interaction.yaml 注册（D4：只能复用注册表）."""
        from src.registry.loader import load_interaction_registry

        reg = load_interaction_registry()
        interaction = reg.get_interaction("writing")
        assert interaction.status == "active"
        # writing 必须兼容 ai_rubric 评分器
        assert "ai_rubric" in interaction.compatible_scorers

    def test_ai_rubric_scorer_registered(self) -> None:
        """ai_rubric 评分器已在 scorer.yaml 注册（D4：只能复用注册表）."""
        from src.registry.loader import load_scorer_registry

        reg = load_scorer_registry()
        scorer = reg.get_scorer("ai_rubric")
        assert scorer.status == "active"
        assert scorer.deterministic is False  # AI 量规非确定性


# ────────────────────────────────────────────────────────────────────
# §3 学段参数化正确：低段 50–100 / 中段 150–250 / 高段 300–400
# ────────────────────────────────────────────────────────────────────


class TestGradeBandParameterization:
    """验收 §3：三学段字数区间注入后 content 正确插值."""

    @pytest.mark.parametrize(
        "band,wmin,wmax,expected_text",
        [
            ("L", 50, 100, "字数要求：50–100 字。"),
            ("M", 150, 250, "字数要求：150–250 字。"),
            ("H", 300, 400, "字数要求：300–400 字。"),
        ],
    )
    def test_composition_word_count_by_grade_band(
        self,
        composition_template: dict,
        band: str,
        wmin: int,
        wmax: int,
        expected_text: str,
    ) -> None:
        """作文模板按学段注入字数区间，content 必含对应字数要求文本."""
        result = instantiate(
            composition_template,
            {
                "topic": "春天",
                "word_count_min": wmin,
                "word_count_max": wmax,
                "time_limit_minutes": 30,
            },
            pack_digest="sha256:pack-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        rendered_texts = [b["rendered"] for b in result.content["blocks"]]
        assert expected_text in rendered_texts, (
            f"学段 {band} 字数要求未正确插值：{rendered_texts}"
        )

    @pytest.mark.parametrize(
        "band,wmin,wmax",
        [("L", 50, 100), ("M", 150, 250), ("H", 300, 400)],
    )
    def test_picture_writing_word_count_by_grade_band(
        self,
        picture_writing_template: dict,
        band: str,
        wmin: int,
        wmax: int,
    ) -> None:
        """看图写话模板按学段注入字数区间，content 必含对应字数要求文本."""
        result = instantiate(
            picture_writing_template,
            {
                "picture_ref": "sha256:asset-test-v1",
                "prompt": "看一看，写一写。",
                "word_count_min": wmin,
                "word_count_max": wmax,
                "time_limit_minutes": 20,
            },
            pack_digest="sha256:pack-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        rendered_texts = [b["rendered"] for b in result.content["blocks"]]
        assert f"字数要求：{wmin}–{wmax} 字。" in rendered_texts, (
            f"学段 {band} 字数要求未正确插值：{rendered_texts}"
        )

    def test_word_count_bounds_consistent_with_blueprint_defaults(self) -> None:
        """模板字数区间与 T-W4-017 Blueprint 默认值一致（D 线契约对齐）."""
        from src.core.production.blueprint_schema import make_blueprint

        bp = make_blueprint(
            blueprint_id="sha256:test-bp-composition-v1",
            writing_type="composition",
            pack_id="subject-chinese",
            template_version_id="sha256:tpl-chinese-composition-v1",
            rubric_template_id="sha256:test-rubric-composition-M-v1",
            topic_pool=["春天"],
            time_limit_minutes=30,
        )
        by_band = {s.grade_band: s for s in bp.grade_band_specs}
        # 与验收③约定一致
        assert (by_band["L"].word_count_min, by_band["L"].word_count_max) == (50, 100)
        assert (by_band["M"].word_count_min, by_band["M"].word_count_max) == (150, 250)
        assert (by_band["H"].word_count_min, by_band["H"].word_count_max) == (300, 400)


# ────────────────────────────────────────────────────────────────────
# §5 核心域零特判：模板位于学科包内，核心域仅通过注册表消费
# ────────────────────────────────────────────────────────────────────


class TestNoCoreDomainSubjectLogic:
    """验收 §5：模板在学科包内；核心域 instantiation engine 不 import 学科包."""

    def test_templates_located_in_subject_pack(self) -> None:
        """两个模板文件均位于 src/packs/subject-chinese/templates/ 下."""
        assert _COMPOSITION_TEMPLATE_PATH.is_file()
        assert _PICTURE_WRITING_TEMPLATE_PATH.is_file()
        # 路径包含 src/packs/subject-chinese（学科包隔离，X6）
        assert "src" in _COMPOSITION_TEMPLATE_PATH.parts
        assert "packs" in _COMPOSITION_TEMPLATE_PATH.parts
        assert "subject-chinese" in _COMPOSITION_TEMPLATE_PATH.parts

    def test_core_instantiation_engine_no_pack_import(self) -> None:
        """核心域 src/core/instantiation/ 禁止 import 学科包（宪法 A5/X6）."""
        import re as _re

        core_dir = (
            _PROJECT_ROOT / "src" / "core" / "instantiation"
        )
        assert core_dir.is_dir(), f"目录不存在：{core_dir}"
        pattern = _re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            _re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(core_dir)))
        assert not violations, (
            f"core/instantiation 存在学科包 import（违反 A5/X6）：{violations}"
        )


# ────────────────────────────────────────────────────────────────────
# 补充：实例化确定性 + content 插值
# ────────────────────────────────────────────────────────────────────


class TestInstantiationDeterminism:
    """同输入必得同 item_version_id（D3 可复现）."""

    def test_same_input_same_id(self, composition_template: dict) -> None:
        """两次相同参数实例化必得同一 item_version_id."""
        params = {
            "topic": "春天",
            "word_count_min": 300,
            "word_count_max": 400,
            "time_limit_minutes": 40,
        }
        common = dict(
            pack_digest="sha256:pack-subject-chinese-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            locale="zh-CN",
            seed=0,
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(composition_template, params, **common)
        r2 = instantiate(composition_template, params, **common)
        assert r1.item_version_id == r2.item_version_id

    def test_different_topic_different_id(self, composition_template: dict) -> None:
        """不同 topic 参数必得不同 item_version_id."""
        common = dict(
            pack_digest="sha256:pack-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(
            composition_template,
            {
                "topic": "春天",
                "word_count_min": 300,
                "word_count_max": 400,
                "time_limit_minutes": 40,
            },
            **common,
        )
        r2 = instantiate(
            composition_template,
            {
                "topic": "我的好朋友",
                "word_count_min": 300,
                "word_count_max": 400,
                "time_limit_minutes": 40,
            },
            **common,
        )
        assert r1.item_version_id != r2.item_version_id

    def test_composition_content_has_topic(self, composition_template: dict) -> None:
        """作文 content.blocks 含主题文本（presentation 插值成功）."""
        result = instantiate(
            composition_template,
            {
                "topic": "春天",
                "word_count_min": 300,
                "word_count_max": 400,
                "time_limit_minutes": 40,
            },
            pack_digest="sha256:pack-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        rendered_texts = [b["rendered"] for b in result.content["blocks"]]
        assert any("春天" in t for t in rendered_texts), (
            f"主题未出现在 content.blocks：{rendered_texts}"
        )

    def test_picture_writing_content_has_prompt(
        self, picture_writing_template: dict
    ) -> None:
        """看图写话 content.blocks 含提示语文本（presentation 插值成功）."""
        result = instantiate(
            picture_writing_template,
            {
                "picture_ref": "sha256:asset-test-v1",
                "prompt": "图上画的是什么季节？",
                "word_count_min": 50,
                "word_count_max": 100,
                "time_limit_minutes": 20,
            },
            pack_digest="sha256:pack-test",
            interaction_id="writing",
            scorer_id="ai_rubric",
            scorer_params={"rubric": {"dimensions": [], "total_max_score": 0}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        rendered_texts = [b["rendered"] for b in result.content["blocks"]]
        assert any("图上画的是什么季节" in t for t in rendered_texts), (
            f"提示语未出现在 content.blocks：{rendered_texts}"
        )
