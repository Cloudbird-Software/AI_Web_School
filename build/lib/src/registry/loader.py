"""注册表加载与校验（T-W1-004）。

从 specs/contracts/registries/ 加载 interaction.yaml 与 scorer.yaml，
经 Pydantic 校验后转为不可变模型实例。提供查询接口与交叉引用校验。

宪法 D4（双类型注册表纪律）：作答交互与评分器只能来自平台注册表；
学科包只能复用与参数化，禁止私造。本模块是注册表在运行时的唯一入口。

宪法 A5/X6：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────
# 默认契约文件路径
# 为什么用 parents[2]：本文件位于 <root>/src/registry/loader.py，
# 向上两级 = 项目根。此路径计算不依赖 cwd，便于测试与生产共用。
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTERACTION_PATH: Path = (
    _PROJECT_ROOT / "specs" / "contracts" / "registries" / "interaction.yaml"
)
DEFAULT_SCORER_PATH: Path = (
    _PROJECT_ROOT / "specs" / "contracts" / "registries" / "scorer.yaml"
)


# ────────────────────────────────────────────────────────────────────
# 交互类型 Pydantic 模型
# ────────────────────────────────────────────────────────────────────

class InteractionType(BaseModel):
    """交互类型条目（interaction.yaml types[*]）。

    字段对齐 specs/contracts/registries/interaction.yaml required_fields：
    id/name/status/summary/response_schema/render_component/paper_spec/
    scoring_input/compatible_scorers。presets 为可选（仅 single_choice
    含 true_false 预设）。
    """

    id: str = Field(..., description="全局唯一标识（snake_case），注册后不可变更语义")
    name: str = Field(..., description="中文名（教研工作台显示）")
    status: Literal["active", "reserved"]
    summary: str
    response_schema: dict[str, Any] = Field(
        ..., description="作答采集 schema（JSON Schema draft 2020-12 子集）"
    )
    render_component: str = Field(
        ..., description="在线渲染组件（平台注册表组件名，学科包不得替换）"
    )
    paper_spec: str = Field(..., description="纸卷呈现规范")
    scoring_input: str = Field(..., description="评分输入契约说明")
    compatible_scorers: list[str] = Field(
        ..., description="允许搭配的评分器 id（见 scorer.yaml）"
    )
    presets: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="参数化预设（如 true_false 是 single_choice 的预设）",
    )

    # 不可变（frozen=True）：单例加载后禁止运行时改写（宪法 D4 注册表冻结纪律）
    # extra=allow：保留契约中新增的可选字段（如 cost_note），不破坏向前兼容
    model_config = {"frozen": True, "extra": "allow"}


class InteractionRegistry(BaseModel):
    """交互类型注册表（interaction.yaml 顶层）。

    查询方法：
        - get_interaction(id): 按 id 取交互类型；未知 id 抛 KeyError
        - list_active(): 列出所有 status=active 的交互类型
    """

    registry: Literal["interaction"]
    contract_version: str
    status: Literal["frozen-candidate", "frozen"]
    source_sections: list[str]
    required_fields: list[str]
    types: list[InteractionType]

    model_config = {"frozen": True}

    def get_interaction(self, interaction_id: str) -> InteractionType:
        """按 id 取交互类型。

        Args:
            interaction_id: interaction.yaml 中 types[*].id。

        Returns:
            匹配的 InteractionType 实例。

        Raises:
            KeyError: 未知 id（无匹配项）。
        """
        for t in self.types:
            if t.id == interaction_id:
                return t
        raise KeyError(f"未知交互类型 id: {interaction_id!r}")

    def list_active(self) -> list[InteractionType]:
        """列出所有 active 交互类型（架构 v2 §2.3：10 现役）。"""
        return [t for t in self.types if t.status == "active"]


# ────────────────────────────────────────────────────────────────────
# 评分器 Pydantic 模型
# ────────────────────────────────────────────────────────────────────

class ScorerType(BaseModel):
    """评分器条目（scorer.yaml scorers[*]）。

    字段对齐 specs/contracts/registries/scorer.yaml required_fields：
    id/name/status/deterministic/input_contract/params_schema/notes。
    统一契约 unified_contract 在 ScorerRegistry 顶层承载。
    """

    id: str
    name: str
    status: Literal["active", "reserved"]
    deterministic: bool = Field(
        ..., description="确定性标志：重判可复现性（R-D-05）的前提"
    )
    summary: str
    input_contract: str = Field(
        ..., description="接受的交互类型作答（文本描述，含交互 id）"
    )
    params_schema: dict[str, Any] = Field(
        ..., description="评分参数 JSON Schema"
    )
    notes: str

    model_config = {"frozen": True, "extra": "allow"}


class ScorerRegistry(BaseModel):
    """评分器注册表（scorer.yaml 顶层）。

    查询方法：
        - get_scorer(id): 按 id 取评分器；未知 id 抛 KeyError
        - list_active(): 列出所有 status=active 的评分器
    """

    registry: Literal["scorer"]
    contract_version: str
    status: Literal["frozen-candidate", "frozen"]
    source_sections: list[str]
    unified_contract: dict[str, Any] = Field(
        ..., description="统一评分契约（架构 v2 §2.3，所有评分器必须遵守）"
    )
    required_fields: list[str]
    scorers: list[ScorerType]

    model_config = {"frozen": True}

    def get_scorer(self, scorer_id: str) -> ScorerType:
        """按 id 取评分器。

        Args:
            scorer_id: scorer.yaml 中 scorers[*].id。

        Returns:
            匹配的 ScorerType 实例。

        Raises:
            KeyError: 未知 id（无匹配项）。
        """
        for s in self.scorers:
            if s.id == scorer_id:
                return s
        raise KeyError(f"未知评分器 id: {scorer_id!r}")

    def list_active(self) -> list[ScorerType]:
        """列出所有 active 评分器（架构 v2 §2.3：6 现役）。"""
        return [s for s in self.scorers if s.status == "active"]


# ────────────────────────────────────────────────────────────────────
# 加载函数
# ────────────────────────────────────────────────────────────────────

def load_interaction_registry(path: Optional[Path] = None) -> InteractionRegistry:
    """加载并校验 interaction.yaml。

    Args:
        path: 契约文件路径。None 时使用默认路径
            specs/contracts/registries/interaction.yaml。

    Returns:
        InteractionRegistry: Pydantic 校验后的不可变注册表实例。

    Raises:
        FileNotFoundError: 文件不存在。
        pydantic.ValidationError: schema 校验失败（缺字段/类型不符等）。

    为什么仅依赖 pyyaml + pydantic（任务卡验收 §1）：
        注册表是平台级类型系统的源头，依赖最小化避免循环依赖。
    """
    if path is None:
        path = DEFAULT_INTERACTION_PATH
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return InteractionRegistry.model_validate(data)


def load_scorer_registry(path: Optional[Path] = None) -> ScorerRegistry:
    """加载并校验 scorer.yaml。

    Args:
        path: 契约文件路径。None 时使用默认路径
            specs/contracts/registries/scorer.yaml。

    Returns:
        ScorerRegistry: Pydantic 校验后的不可变注册表实例。

    Raises:
        FileNotFoundError: 文件不存在。
        pydantic.ValidationError: schema 校验失败。
    """
    if path is None:
        path = DEFAULT_SCORER_PATH
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ScorerRegistry.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# 交叉引用校验（宪法 D4 / 验收标准 §5）
# ────────────────────────────────────────────────────────────────────

def validate_cross_references(
    interaction_registry: InteractionRegistry,
    scorer_registry: ScorerRegistry,
) -> None:
    """交叉引用校验：交互类型的 compatible_scorers 必须在 scorer registry 中存在。

    双向闭合由 tests/contract/registries/test_registry_bidirectional.py 强制；
    本函数提供运行时单向校验（interaction → scorer），用于单例加载时把关门。

    Args:
        interaction_registry: 已加载的交互类型注册表。
        scorer_registry: 已加载的评分器注册表。

    Raises:
        ValueError: 发现悬空引用（compatible_scorers 引用了未注册的评分器 id）。
    """
    scorer_ids = {s.id for s in scorer_registry.scorers}
    dangling: list[tuple[str, str]] = []
    for interaction in interaction_registry.types:
        for sid in interaction.compatible_scorers:
            if sid not in scorer_ids:
                dangling.append((interaction.id, sid))
    if dangling:
        details = ", ".join(f"{iid}->{sid}" for iid, sid in dangling)
        raise ValueError(
            f"compatible_scorers 交叉引用校验失败（引用了未注册的评分器）：{details}"
        )
