"""T-W4-017 量规模板数据化（D 线命题工坊）.

落地架构 v2 §4.5「AI 维度量规评分器」的数据侧契约：**量规即数据**。
量规模板是可序列化为 JSON 的纯数据结构，被 ``AIRubricScorer``（T-W4-019）
直接解析执行——评分器不感知量规语义，只按量规维度/锚点/分值带让强模型打分。

与 ``specs/contracts/registries/scorer.yaml`` 的 ``ai_rubric`` 契约对齐：
    params_schema.rubric.dimensions[*] = {id, name, anchors, score_bands, error_type_rules}

本模块只定义数据模型（Pydantic），不涉及 DB 读写（持久化由迁移 0018 + 调用方
经写入服务承载），也不感知任何学科语义（宪法 A5/X6：核心域零学科特判）。

宪法 D4：评分器与量规结构只能来自注册表；本模块是量规**数据**的运行时载体，
``rubric_id`` 在 ``scorer.yaml`` 的 ``ai_rubric`` 契约下被评分器消费。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 学段覆盖标记：L=低段 / M=中段 / H=高段（与 GradeBandPack 三档对齐）
GradeBand = Literal["L", "M", "H"]


class RubricLevel(BaseModel):
    """量规单等级（如 优秀/良好/合格/待改进).

    - level：等级序号，1=最高档（约定，便于排序与一致率计算时取分）。
    - label：等级名（教研展示用）。
    - description：该等级的行为锚点描述（**非空**——AI 评分器据此判定该维
      作答落在哪一档；空描述会让强模型无锚点可比，验收②「等级描述非空」）。
    - score：该等级对应分值（满分=最高档 score；低档可 <= 满分，通常递减）。
    """

    model_config = ConfigDict(extra="forbid")

    level: int = Field(..., ge=1, description="等级序号，1=最高档")
    label: str = Field(..., min_length=1, description="等级名（如 优秀）")
    description: str = Field(..., min_length=1, description="行为锚点描述（非空）")
    score: float = Field(..., description="该等级分值")


class RubricDimension(BaseModel):
    """量规单维度（如 内容/结构/语言/书写）.

    - id：维度 id（snake_case，评分器按 id 落 dimension_scores 键）。
    - name：维度中文名（教研展示 + AI prompt 中呈现）。
    - max_score：该维度满分（= max(levels.score)；验收③「分值合计正确」）。
    - levels：等级列表（≥2 档，否则无区分度；按 level 升序排列）。
    - error_type_rules：维度得分模式 → 错误类型规则表（对齐 scorer.yaml），
      评分器据此产 error_inferences（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="维度 id（snake_case）")
    name: str = Field(..., min_length=1, description="维度中文名")
    max_score: float = Field(..., ge=0, description="维度满分")
    levels: list[RubricLevel] = Field(..., min_length=2, description="等级列表（≥2）")
    error_type_rules: list[dict[str, Any]] = Field(
        default_factory=list, description="维度得分→错误类型规则（可空）"
    )

    @model_validator(mode="after")
    def _check_max_score_matches_levels(self) -> RubricDimension:
        """max_score 必须等于最高档 score（分值带一致性，验收③）.

        为什么校验 max == max(levels.score) 而非 sum：单维度满分是该维度能拿
        的最高分，即最高档的 score；levels 是「同一维度的不同档位」，不是
        「多个子项」。sum 会把各档分值相加，语义错误。
        """
        top = max(lvl.score for lvl in self.levels)
        if abs(top - self.max_score) > 1e-9:
            raise ValueError(
                f"维度 {self.id!r} max_score={self.max_score} 不等于最高档"
                f" score={top}（分值带不一致）"
            )
        return self

    @model_validator(mode="after")
    def _check_levels_ordered_unique(self) -> RubricDimension:
        """等级 level 唯一且建议升序（不强制排序，但 level 不可重复）."""
        levels_seen = [lvl.level for lvl in self.levels]
        if len(set(levels_seen)) != len(levels_seen):
            raise ValueError(
                f"维度 {self.id!r} 等级 level 重复：{levels_seen}"
            )
        return self


class RubricTemplate(BaseModel):
    """量规模板（可序列化为 JSON 被评分器直接解析执行）.

    - rubric_id：量规 id（内容寻址，sha256 of payload；版本化时新 id）。
    - grade_band：学段覆盖标记（L/M/H）；同学科可有同学段不同主题的多套量规。
    - dimensions：维度列表（≥1）。
    - total_max_score：分值合计（= sum(dimensions.max_score)；验收③）。
    - version：量规版本串（随题版本化，重判时据此写平行账）。

    ``to_scorer_params()`` 输出对齐 scorer.yaml ``ai_rubric.params_schema.rubric``
    的结构，被 ``AIRubricScorer`` 直接消费（验收③「可序列化为 JSON 被评分器解析」）。
    """

    model_config = ConfigDict(extra="forbid")

    rubric_id: str = Field(..., min_length=1, description="量规 id（内容寻址）")
    name: str = Field(..., min_length=1, description="量规模板名")
    grade_band: GradeBand = Field(..., description="学段覆盖标记 L/M/H")
    dimensions: list[RubricDimension] = Field(
        ..., min_length=1, description="维度列表（≥1）"
    )
    total_max_score: float = Field(..., ge=0, description="分值合计")
    version: str = Field(..., min_length=1, description="量规版本串")

    @model_validator(mode="after")
    def _check_total_score(self) -> RubricTemplate:
        """分值合计校验：total_max_score == sum(dimensions.max_score)（验收③）."""
        actual = sum(dim.max_score for dim in self.dimensions)
        if abs(actual - self.total_max_score) > 1e-9:
            raise ValueError(
                f"total_max_score={self.total_max_score} 不等于维度满分合计"
                f" {actual}（分值合计不一致）"
            )
        return self

    @model_validator(mode="after")
    def _check_dimension_ids_unique(self) -> RubricTemplate:
        """维度 id 唯一（评分器按 id 落 dimension_scores 键，重复会覆盖）."""
        ids = [dim.id for dim in self.dimensions]
        if len(set(ids)) != len(ids):
            dupes = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"维度 id 重复：{sorted(set(dupes))}")
        return self

    def to_scorer_params(self) -> dict[str, Any]:
        """序列化为 scorer.yaml ``ai_rubric.params_schema.rubric`` 结构.

        量规即数据：本方法把内部强类型模型（levels[]）映射为评分器契约要求的
        ``{dimensions:[{id,name,anchors,score_bands,error_type_rules}]}``，
        使 ``AIRubricScorer`` 无需感知量规内部结构即可消费（验收③）。

        映射约定：
        - anchors ← levels[].description（各档行为锚点描述，按 level 升序）；
        - score_bands ← levels[]（保留 level/label/score，供评分器落档）；
        - error_type_rules 原样透传。
        """
        return {
            "dimensions": [
                {
                    "id": dim.id,
                    "name": dim.name,
                    "anchors": [
                        lvl.description
                        for lvl in sorted(dim.levels, key=lambda l: l.level)
                    ],
                    "score_bands": [
                        {"level": lvl.level, "label": lvl.label, "score": lvl.score}
                        for lvl in sorted(dim.levels, key=lambda l: l.level)
                    ],
                    "error_type_rules": dim.error_type_rules,
                }
                for dim in self.dimensions
            ],
            "total_max_score": self.total_max_score,
        }


__all__ = [
    "GradeBand",
    "RubricDimension",
    "RubricLevel",
    "RubricTemplate",
]
