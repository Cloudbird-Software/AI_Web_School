"""§4.4 组卷候选筛选（T-W3-assembly S1）.

候选 = published 实例池 × 学段 × 用途许可 × 曝光历史（架构 v2 §4.4 求解段）。
本模块定义求解器消费的规范化候选模型 CandidateItem，以及从 serving 视图
加载候选的 DB 读取器。

为什么是独立规范化模型而非直接消费 item_version dict：
- 求解器是纯函数（确定性要求），不应感知 JSONB 六大块的嵌套结构；
- 用途许可/正确率先验等派生字段在加载期解析一次，求解期只做比较。

用途许可来源（v1 约定）：lineage.params.allowed_purposes（list[str]）；
缺失时默认全场景许可（向后兼容 W2 已发布实例）。目的许可的正式治理
（item_param.purpose_scope 分场景）属 S8 数据域，落地后加载器改读
item_param 表——本模块的 CandidateItem 契约不变。

正确率先验来源（v1 约定）：lineage.params.p_correct_prior（float, 0–1）；
缺失时 p_correct_prior=None，求解器按 Profile 的冷启动策略处理
（§4.4 冷启动降级：纯先验区间+保守宽度）。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.assembly.profile import Gradeband, Purpose

_ALL_PURPOSES: list[Purpose] = ["practice", "diagnosis", "measurement"]


class CandidateItem(BaseModel):
    """求解器消费的规范化候选题.

    字段全部从 item_version 六大块 + item 谱系派生；
    p_correct_prior / allowed_purposes / mix_tag 为 v1 先验元数据约定
    （见模块 docstring），S8 数据域落地后改由 item_param 供给。
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    item_id: str
    template_version_id: Optional[str] = None
    kp_codes: list[str] = Field(min_length=1)
    kp_set_mode: Literal["single", "all_required", "compensatory"]
    gradeband: Gradeband
    interaction_id: str
    p_correct_prior: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    allowed_purposes: list[Purpose] = Field(default_factory=lambda: list(_ALL_PURPOSES))
    # 内容配比标签（新学/复习/易混淆；None=未标注，不参与配比统计）
    mix_tag: Optional[Literal["new", "review", "confusable"]] = None
    # 题组 id（同一题组的题作为整体入选/排除；None=孤立题）
    group_id: Optional[str] = None

    @property
    def is_isolated(self) -> bool:
        """孤立题：单知识点且声明为 single（诊断归因的定位题，§4.5）."""
        return len(self.kp_codes) == 1 and self.kp_set_mode == "single"


def candidate_from_serving_row(row: Mapping[str, Any]) -> CandidateItem:
    """从 v_serving_item_version 行（dict/Mapping）构建候选.

    row 至少含：item_version_id / item_id / template_version_id /
    objective / interaction_ref / lineage（视图列，见 serving_views.sql §2）。
    """
    objective = row.get("objective") or {}
    kp_set = objective.get("kp_set") or []
    kp_codes = [str(k.get("code")) for k in kp_set if k.get("code")]
    if not kp_codes:
        raise ValueError(
            f"item_version {row.get('item_version_id')} 的 objective.kp_set 为空，无法组卷"
        )
    interaction_ref = row.get("interaction_ref") or {}
    lineage = row.get("lineage") or {}
    params = lineage.get("params") or {}

    purposes = params.get("allowed_purposes")
    if purposes is not None:
        unknown = set(purposes) - set(_ALL_PURPOSES)
        if unknown:
            raise ValueError(f"allowed_purposes 含未知场景 {sorted(unknown)}")

    p_prior = params.get("p_correct_prior")
    return CandidateItem(
        item_version_id=str(row["item_version_id"]),
        item_id=str(row["item_id"]),
        template_version_id=(
            str(row["template_version_id"]) if row.get("template_version_id") else None
        ),
        kp_codes=kp_codes,
        kp_set_mode=objective.get("kp_set_mode", "single"),
        gradeband=objective["gradeband"],
        interaction_id=str(interaction_ref.get("interaction_id", "")),
        p_correct_prior=float(p_prior) if p_prior is not None else None,
        allowed_purposes=list(purposes) if purposes else list(_ALL_PURPOSES),
        mix_tag=params.get("mix_tag"),
        group_id=params.get("group_id"),
    )


_SERVING_POOL_SQL = """
SELECT
    item_version_id,
    item_id,
    template_version_id,
    objective,
    interaction_ref,
    lineage
FROM v_serving_item_version
WHERE pack_id = :pack_id
  AND objective->>'gradeband' = :gradeband
"""


async def load_candidates(
    session: AsyncSession,
    *,
    subject_pack_id: str,
    gradeband: Gradeband,
) -> list[CandidateItem]:
    """从 serving 视图加载候选池（published 且未退役 × 学科 × 学段）.

    曝光历史与用途许可的过滤在求解期进行（曝光集随 队列/学生 变化，
    池加载保持与曝光无关，便于快照固化与确定性重放）。
    """
    result = await session.execute(
        text(_SERVING_POOL_SQL),
        {"pack_id": subject_pack_id, "gradeband": gradeband},
    )
    return [candidate_from_serving_row(dict(r)) for r in result.mappings().all()]


__all__ = [
    "CandidateItem",
    "candidate_from_serving_row",
    "load_candidates",
]
