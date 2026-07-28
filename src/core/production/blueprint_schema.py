"""T-W4-017 命题蓝图库 schema（D 线命题工坊）.

落地架构 v2 §4.1 D 线「命题蓝图库」与 §4.5 量规评分：命题蓝图是 D 线命题的
入口结构——声明写作类型/学段/主题池/字数区间/时间限制/量规模板引用，
被 ``run_d_pipeline``（T-W4-021）消费：选蓝图 → 按模板实例化 → 量规嵌入 →
校验门 → 签发入库。

学段参数化（验收③）：低段 50-100 字 / 中段 150-250 字 / 高段 300-400 字，
由 ``GradeBandSpec`` 承载；蓝图含三学段 specs，实例化时按学生学段取对应区间。

宪法 A5/X6：本模块不 import 任何学科包/学段包；``pack_id`` 是字符串字段，
核心域仅通过注册表 id 字符串引用学科，不感知学科语义。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.production.rubric_template import GradeBand

# 写作类型（与 interaction.yaml 的 writing 交互 + 学科包模板对齐）
WritingType = Literal["composition", "picture_writing"]


class GradeBandSpec(BaseModel):
    """学段参数（字数区间/时间限制/评分宽松度）.

    - grade_band：学段 L/M/H。
    - word_count_min/max：字数区间（验收③：低段50-100/中段150-250/高段300-400）。
    - time_limit_minutes：建议作答时长（分钟）。
    - rubric_leniency：评分宽松度 0-1（低段更宽松；透传给评分器作上下文提示，
      不改变量规分值——量规是数据，宽松度是提示而非硬规则）。

    约束：word_count_min < word_count_max（空区间无意义）。
    """

    model_config = ConfigDict(extra="forbid")

    grade_band: GradeBand = Field(..., description="学段 L/M/H")
    word_count_min: int = Field(..., ge=0, description="字数下限")
    word_count_max: int = Field(..., ge=0, description="字数上限")
    time_limit_minutes: int = Field(..., ge=1, description="作答时长（分钟）")
    rubric_leniency: float = Field(
        ..., ge=0.0, le=1.0, description="评分宽松度 0-1"
    )

    @model_validator(mode="after")
    def _check_word_count_range(self) -> GradeBandSpec:
        """字数下限必须严格小于上限（空区间无意义）."""
        if self.word_count_min >= self.word_count_max:
            raise ValueError(
                f"学段 {self.grade_band!r} word_count_min={self.word_count_min}"
                f" 须 < word_count_max={self.word_count_max}"
            )
        return self


class Blueprint(BaseModel):
    """命题蓝图（写作类型/学段/主题池/字数区间/时间限制/量规模板引用）.

    - blueprint_id：蓝图 id（版本化时新 id）。
    - writing_type：写作类型（composition=作文 / picture_writing=看图写话）。
    - pack_id：学科包 id（如 "subject-chinese"）；核心域仅字符串引用，不 import 包。
    - template_version_id：A 线母题模板版本引用（实例化时定位模板）。
    - rubric_template_id：量规模板引用（→ ``RubricTemplate.rubric_id``）。
    - grade_band_specs：三学段参数化（须覆盖 L/M/H 三档）。
    - topic_pool：主题池（实例化时按主题注入）。
    - time_limit_minutes：默认时间限制（学段 spec 未指定时回退）。
    - version：蓝图版本串。

    验收①：写作类型/学段/主题池/字数区间/时间限制/量规模板引用 齐全。
    """

    model_config = ConfigDict(extra="forbid")

    blueprint_id: str = Field(..., min_length=1, description="蓝图 id")
    writing_type: WritingType = Field(..., description="写作类型")
    pack_id: str = Field(..., min_length=1, description="学科包 id（字符串引用）")
    template_version_id: str = Field(..., min_length=1, description="A 线模板版本引用")
    rubric_template_id: str = Field(..., min_length=1, description="量规模板引用")
    grade_band_specs: list[GradeBandSpec] = Field(
        ..., min_length=1, description="学段参数化 specs"
    )
    topic_pool: list[str] = Field(..., min_length=1, description="主题池（≥1）")
    time_limit_minutes: int = Field(..., ge=1, description="默认作答时长（分钟）")
    version: str = Field(..., min_length=1, description="蓝图版本串")

    @model_validator(mode="after")
    def _check_grade_band_coverage(self) -> Blueprint:
        """学段 specs 须覆盖 L/M/H 三档且不重复（D 线题目需全学段可用）.

        为什么要求三档齐全：命题蓝图是「可复用题目骨架」，应能在三学段下产出
        合规实例；缺学段会让该学段学生无法消费。如某学段不适用，应另起蓝图
        而非留空。
        """
        bands = [spec.grade_band for spec in self.grade_band_specs]
        if len(set(bands)) != len(bands):
            raise ValueError(f"学段 specs 重复：{bands}")
        required = {"L", "M", "H"}
        missing = required - set(bands)
        if missing:
            raise ValueError(f"学段 specs 未覆盖：缺 {sorted(missing)}")
        return self


def make_blueprint(
    *,
    blueprint_id: str,
    writing_type: WritingType,
    pack_id: str,
    template_version_id: str,
    rubric_template_id: str,
    topic_pool: list[str],
    time_limit_minutes: int,
    version: str = "1",
) -> Blueprint:
    """便捷构造：用默认三学段字数区间（验收③）建蓝图.

    默认字数区间：低段50-100/中段150-250/高段300-400（任务卡验收③约定）。
    默认宽松度：低段0.8/中段0.6/高段0.5（低段更宽容）。
    默认时长：低段20/中段30/高段40 分钟。
    """
    defaults: dict[str, dict[str, Any]] = {
        "L": {"wmin": 50, "wmax": 100, "tmin": 20, "leniency": 0.8},
        "M": {"wmin": 150, "wmax": 250, "tmin": 30, "leniency": 0.6},
        "H": {"wmin": 300, "wmax": 400, "tmin": 40, "leniency": 0.5},
    }
    specs = [
        GradeBandSpec(
            grade_band=band,  # type: ignore[arg-type]
            word_count_min=defaults[band]["wmin"],
            word_count_max=defaults[band]["wmax"],
            time_limit_minutes=defaults[band]["tmin"],
            rubric_leniency=defaults[band]["leniency"],
        )
        for band in ("L", "M", "H")
    ]
    return Blueprint(
        blueprint_id=blueprint_id,
        writing_type=writing_type,
        pack_id=pack_id,
        template_version_id=template_version_id,
        rubric_template_id=rubric_template_id,
        grade_band_specs=specs,
        topic_pool=topic_pool,
        time_limit_minutes=time_limit_minutes,
        version=version,
    )


__all__ = [
    "Blueprint",
    "GradeBandSpec",
    "WritingType",
    "make_blueprint",
]
