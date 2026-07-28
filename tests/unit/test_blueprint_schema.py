"""T-W4-017 命题蓝图库 + 量规模板数据化 单元测试.

验收对齐：
1. Blueprint 含 写作类型/学段/主题池/字数区间/时间限制/量规模板引用。
2. RubricTemplate 含 维度名/等级数/各等级描述/分值/学段覆盖标记。
3. 量规可序列化为 JSON 被 AIRubricScorer 解析执行（to_scorer_params）。
4. make accept 全绿；迁移可升降级（make migrate-check 覆盖；本文件验表结构 + 触发器）。
5. 不 import 任何学科包/学段包（A5/X6 静态扫描）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from src.core.production.blueprint_schema import (
    Blueprint,
    GradeBandSpec,
    make_blueprint,
)
from src.core.production.rubric_template import (
    RubricDimension,
    RubricLevel,
    RubricTemplate,
)


# ────────────────────────────────────────────────────────────────────
# 测试夹具：一份合法量规 + 蓝图
# ────────────────────────────────────────────────────────────────────


def _make_rubric(grade_band: str = "M") -> RubricTemplate:
    """构造一份四维量规（内容/结构/语言/书写，各 5 分满分，3 档）."""
    return RubricTemplate(
        rubric_id=f"sha256:test-rubric-composition-{grade_band}-v1",
        name=f"作文量规-{grade_band}段",
        grade_band=grade_band,  # type: ignore[arg-type]
        dimensions=[
            RubricDimension(
                id="content",
                name="内容",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="主题明确，内容充实具体", score=5),
                    RubricLevel(level=2, label="合格", description="主题基本明确，内容较具体", score=3),
                    RubricLevel(level=3, label="待改进", description="主题模糊或内容空泛", score=1),
                ],
            ),
            RubricDimension(
                id="structure",
                name="结构",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="段落清晰，过渡自然", score=5),
                    RubricLevel(level=2, label="合格", description="段落较清晰，过渡略显生硬", score=3),
                    RubricLevel(level=3, label="待改进", description="段落混乱，无过渡", score=1),
                ],
            ),
            RubricDimension(
                id="language",
                name="语言",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="语句通顺，用词准确丰富", score=5),
                    RubricLevel(level=2, label="合格", description="语句基本通顺，用词一般", score=3),
                    RubricLevel(level=3, label="待改进", description="语句不通，用词不当", score=1),
                ],
            ),
            RubricDimension(
                id="handwriting",
                name="书写",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="字迹工整，无错别字", score=5),
                    RubricLevel(level=2, label="合格", description="字迹较工整，偶有错别字", score=3),
                    RubricLevel(level=3, label="待改进", description="字迹潦草，错别字多", score=1),
                ],
            ),
        ],
        total_max_score=20,
        version="1.0.0",
    )


def _make_blueprint(writing_type: str = "composition") -> Blueprint:
    """构造一份合法蓝图（三学段参数化，验收③字数区间）."""
    return make_blueprint(
        blueprint_id="sha256:test-blueprint-composition-v1",
        writing_type=writing_type,  # type: ignore[arg-type]
        pack_id="subject-chinese",
        template_version_id="sha256:tpl-chinese-composition-v1",
        rubric_template_id="sha256:test-rubric-composition-M-v1",
        topic_pool=["春天", "我的好朋友", "一次难忘的旅行"],
        time_limit_minutes=30,
    )


# ────────────────────────────────────────────────────────────────────
# 验收②：RubricTemplate 含 维度名/等级数/各等级描述/分值/学段覆盖标记
# ────────────────────────────────────────────────────────────────────


class TestRubricTemplate:
    def test_dimensions_have_name_levels_scores(self) -> None:
        """维度含名称/等级数/各等级描述/分值."""
        rubric = _make_rubric("H")
        assert len(rubric.dimensions) == 4
        for dim in rubric.dimensions:
            assert dim.name, "维度名非空"
            assert len(dim.levels) >= 2, "等级数≥2"
            for lvl in dim.levels:
                assert lvl.description, "等级描述非空"
                assert lvl.score >= 0, "分值≥0"

    def test_grade_band_coverage_marker(self) -> None:
        """学段覆盖标记 L/M/H."""
        for band in ("L", "M", "H"):
            rubric = _make_rubric(band)
            assert rubric.grade_band == band

    def test_total_max_score_validation_pass(self) -> None:
        """分值合计正确时通过."""
        rubric = _make_rubric()
        assert rubric.total_max_score == sum(d.max_score for d in rubric.dimensions)

    def test_total_max_score_mismatch_raises(self) -> None:
        """分值合计不匹配 → ValidationError（验收③分值合计正确）."""
        with pytest.raises(Exception):  # ValidationError
            RubricTemplate(
                rubric_id="r1",
                name="bad",
                grade_band="M",
                dimensions=[
                    RubricDimension(
                        id="x", name="X", max_score=5,
                        levels=[
                            RubricLevel(level=1, label="a", description="d1", score=5),
                            RubricLevel(level=2, label="b", description="d2", score=3),
                        ],
                    ),
                ],
                total_max_score=99,  # 不等于 5
                version="1",
            )

    def test_dimension_max_score_must_equal_top_level(self) -> None:
        """维度 max_score 必须等于最高档 score（分值带一致性）."""
        with pytest.raises(Exception):
            RubricDimension(
                id="x", name="X", max_score=10,  # 不等于 max(5,3)=5
                levels=[
                    RubricLevel(level=1, label="a", description="d1", score=5),
                    RubricLevel(level=2, label="b", description="d2", score=3),
                ],
            )

    def test_level_description_must_be_non_empty(self) -> None:
        """等级描述非空（验收②）."""
        with pytest.raises(Exception):
            RubricLevel(level=1, label="a", description="", score=5)

    def test_dimension_levels_min_two(self) -> None:
        """等级数≥2（单档无区分度）."""
        with pytest.raises(Exception):
            RubricDimension(
                id="x", name="X", max_score=5,
                levels=[RubricLevel(level=1, label="a", description="d", score=5)],
            )

    def test_dimension_id_unique(self) -> None:
        """维度 id 不可重复（评分器按 id 落键）."""
        with pytest.raises(Exception):
            RubricTemplate(
                rubric_id="r1", name="dup", grade_band="M",
                dimensions=[
                    RubricDimension(
                        id="x", name="X", max_score=5,
                        levels=[
                            RubricLevel(level=1, label="a", description="d1", score=5),
                            RubricLevel(level=2, label="b", description="d2", score=3),
                        ],
                    ),
                    RubricDimension(
                        id="x", name="Y", max_score=5,
                        levels=[
                            RubricLevel(level=1, label="a", description="d1", score=5),
                            RubricLevel(level=2, label="b", description="d2", score=3),
                        ],
                    ),
                ],
                total_max_score=10,
                version="1",
            )


# ────────────────────────────────────────────────────────────────────
# 验收③：量规可序列化为 JSON 被评分器解析执行
# ────────────────────────────────────────────────────────────────────


class TestRubricSerialization:
    def test_to_scorer_params_aligns_ai_rubric_contract(self) -> None:
        """to_scorer_params 输出对齐 scorer.yaml ai_rubric.params_schema.rubric.

        契约要求 dimensions[*] = {id, name, anchors, score_bands, error_type_rules}。
        """
        rubric = _make_rubric("M")
        params = rubric.to_scorer_params()
        assert "dimensions" in params
        for dim in params["dimensions"]:
            assert {"id", "name", "anchors", "score_bands", "error_type_rules"} <= set(dim.keys())

    def test_anchors_from_level_descriptions(self) -> None:
        """anchors ← levels[].description（按 level 升序）."""
        rubric = _make_rubric()
        params = rubric.to_scorer_params()
        content_dim = next(d for d in params["dimensions"] if d["id"] == "content")
        assert content_dim["anchors"] == [
            "主题明确，内容充实具体",
            "主题基本明确，内容较具体",
            "主题模糊或内容空泛",
        ]

    def test_score_bands_carry_level_label_score(self) -> None:
        """score_bands 含 level/label/score（评分器落档用）."""
        rubric = _make_rubric()
        params = rubric.to_scorer_params()
        content_dim = next(d for d in params["dimensions"] if d["id"] == "content")
        assert len(content_dim["score_bands"]) == 3
        top = content_dim["score_bands"][0]
        assert top["level"] == 1 and top["label"] == "优秀" and top["score"] == 5

    def test_serializable_to_json_roundtrip(self) -> None:
        """to_scorer_params 可 json 序列化与反序列化（验收③）."""
        import json
        rubric = _make_rubric()
        params = rubric.to_scorer_params()
        s = json.dumps(params, ensure_ascii=False)
        restored = json.loads(s)
        assert restored == params


# ────────────────────────────────────────────────────────────────────
# 验收①：Blueprint 含 写作类型/学段/主题池/字数区间/时间限制/量规模板引用
# ────────────────────────────────────────────────────────────────────


class TestBlueprint:
    def test_contains_all_required_fields(self) -> None:
        """验收①：写作类型/学段/主题池/字数区间/时间限制/量规模板引用 齐全."""
        bp = _make_blueprint()
        assert bp.writing_type == "composition"
        assert bp.topic_pool  # 主题池非空
        assert bp.rubric_template_id  # 量规模板引用
        assert bp.template_version_id  # A 线模板引用
        assert bp.time_limit_minutes >= 1
        assert len(bp.grade_band_specs) == 3  # 三学段

    def test_grade_band_word_counts(self) -> None:
        """验收③：低段50-100/中段150-250/高段300-400."""
        bp = _make_blueprint()
        by_band = {s.grade_band: s for s in bp.grade_band_specs}
        assert (by_band["L"].word_count_min, by_band["L"].word_count_max) == (50, 100)
        assert (by_band["M"].word_count_min, by_band["M"].word_count_max) == (150, 250)
        assert (by_band["H"].word_count_min, by_band["H"].word_count_max) == (300, 400)

    def test_grade_band_coverage_required(self) -> None:
        """学段 specs 须覆盖 L/M/H 三档."""
        with pytest.raises(Exception):
            Blueprint(
                blueprint_id="b1", writing_type="composition", pack_id="p",
                template_version_id="t", rubric_template_id="r",
                grade_band_specs=[
                    GradeBandSpec(grade_band="L", word_count_min=50, word_count_max=100,
                                  time_limit_minutes=20, rubric_leniency=0.8),
                ],
                topic_pool=["t1"], time_limit_minutes=30, version="1",
            )

    def test_word_count_range_min_lt_max(self) -> None:
        """字数下限 < 上限（空区间无意义）."""
        with pytest.raises(Exception):
            GradeBandSpec(grade_band="L", word_count_min=100, word_count_max=100,
                          time_limit_minutes=20, rubric_leniency=0.8)

    def test_writing_type_domain(self) -> None:
        """写作类型限于 composition/picture_writing."""
        bp = _make_blueprint("picture_writing")
        assert bp.writing_type == "picture_writing"
        with pytest.raises(Exception):
            make_blueprint(
                blueprint_id="b", writing_type="invalid",  # type: ignore[arg-type]
                pack_id="p", template_version_id="t", rubric_template_id="r",
                topic_pool=["x"], time_limit_minutes=30,
            )


# ────────────────────────────────────────────────────────────────────
# 验收④：迁移表结构 + append-only 触发器（迁移可逆由 make migrate-check 覆盖）
# ────────────────────────────────────────────────────────────────────


class TestMigrationSchema:
    """验证 0018 迁移建的表结构与 append-only 强制."""

    @pytest.mark.asyncio
    async def test_rubric_template_table_exists(self, async_session: Any) -> None:
        """rubric_template 表存在."""
        result = await async_session.execute(
            text(
                "SELECT to_regclass('public.rubric_template')"
            )
        )
        assert result.scalar() == "rubric_template"

    @pytest.mark.asyncio
    async def test_blueprint_table_exists(self, async_session: Any) -> None:
        """blueprint 表存在."""
        result = await async_session.execute(
            text("SELECT to_regclass('public.blueprint')")
        )
        assert result.scalar() == "blueprint"

    @pytest.mark.asyncio
    async def test_append_only_rejects_update(self, async_session: Any) -> None:
        """rubric_template 触发器拒绝 UPDATE（append-only）."""
        rubric = _make_rubric("L")
        await async_session.execute(
            text(
                "INSERT INTO rubric_template "
                "(rubric_id, name, grade_band, version, payload, total_max_score) "
                "VALUES (:id, :name, :gb, :ver, CAST(:payload AS JSONB), :tms)"
            ),
            {
                "id": rubric.rubric_id, "name": rubric.name,
                "gb": rubric.grade_band, "ver": rubric.version,
                "payload": rubric.model_dump_json(),
                "tms": rubric.total_max_score,
            },
        )
        with pytest.raises(Exception, match="append-only"):
            await async_session.execute(
                text("UPDATE rubric_template SET name='changed' WHERE rubric_id=:id"),
                {"id": rubric.rubric_id},
            )

    @pytest.mark.asyncio
    async def test_append_only_rejects_delete(self, async_session: Any) -> None:
        """blueprint 触发器拒绝 DELETE（append-only）."""
        await async_session.execute(
            text(
                "INSERT INTO rubric_template "
                "(rubric_id, name, grade_band, version, payload, total_max_score) "
                "VALUES ('r-del', 'n', 'L', '1', '{}'::jsonb, 0)"
            ),
        )
        await async_session.execute(
            text(
                "INSERT INTO blueprint "
                "(blueprint_id, writing_type, pack_id, template_version_id, "
                "rubric_template_id, payload, version) "
                "VALUES ('b-del', 'composition', 'p', 't', 'r-del', '{}'::jsonb, '1')"
            ),
        )
        with pytest.raises(Exception, match="append-only"):
            await async_session.execute(
                text("DELETE FROM blueprint WHERE blueprint_id='b-del'")
            )

    @pytest.mark.asyncio
    async def test_fk_blocks_orphan_blueprint(self, async_session: Any) -> None:
        """blueprint.rubric_template_id FK→rubric_template：悬空引用被 RESTRICT."""
        with pytest.raises(Exception):
            await async_session.execute(
                text(
                    "INSERT INTO blueprint "
                    "(blueprint_id, writing_type, pack_id, template_version_id, "
                    "rubric_template_id, payload, version) "
                    "VALUES ('b-orphan', 'composition', 'p', 't', "
                    "'nonexistent-rubric', '{}'::jsonb, '1')"
                ),
            )


# ────────────────────────────────────────────────────────────────────
# 验收⑤：不 import 任何学科包/学段包（A5/X6 静态扫描）
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """核心域 src/core/production/ 禁止 import 学科包/学段包（宪法 A5/X6）."""

    def test_no_packs_import(self) -> None:
        prod_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "core"
            / "production"
        )
        assert prod_dir.is_dir(), f"目录不存在：{prod_dir}"
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(prod_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(prod_dir)))
        assert not violations, f"production 存在学科包 import（违反 A5）：{violations}"
