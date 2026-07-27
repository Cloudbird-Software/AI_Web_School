"""母题 DSL v1 六大块 Pydantic Schema（T-W2-001）。

六大块对齐架构 v2 §4.1 A 线与 specs/contracts/db/item-model.md §2.3：
  objective / slots / variation_axes / presentation / answer_program / distractor_rules

所有模型 extra='forbid'（验收 §1）：DSL 结构冻结，未声明字段一律拒绝，
新增字段必须升级 dsl_version。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ────────────────────────────────────────────────────────────────────
# 槽类型允许列表（验收 §2：slot 类型不在允许列表 → lint 报错）
# 为什么这 6 种：覆盖数学/语文/英语三科母题的参数化需求——
#   int/fraction/decimal 服务数学数值槽（定点/分数运算，禁浮点漂移，D2）；
#   string 服务语文/英语文本槽与通用标识；
#   bool 服务判断/开关槽；
#   choice 服务有限选项枚举（与 single_choice 交互的 options 不同——
#   choice 槽是母题参数空间里的枚举维度，options 是实例化后的作答选项）。
# ────────────────────────────────────────────────────────────────────
ALLOWED_SLOT_TYPES: frozenset[str] = frozenset(
    {"int", "decimal", "fraction", "string", "bool", "choice"}
)


# ────────────────────────────────────────────────────────────────────
# objective 块（对齐 item-model.md §5.1 objective schema）
# ────────────────────────────────────────────────────────────────────
class KPPoint(BaseModel):
    """知识点标注项：维度 × 编码。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(..., description="知识点维度（如 kp）")
    code: str = Field(..., description="知识点编码（如 math.nal.decimal.compare）")


class ObjectiveStep(BaseModel):
    """分步过程题的步骤级知识点标注（R-Q-15）。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    kp: list[str] = Field(..., description="该步骤关联的知识点编码列表")


class Objective(BaseModel):
    """objective 块：知识标注集 + 认知层级 + 多点关系声明 + 学段。"""

    model_config = ConfigDict(extra="forbid")

    kp_set: list[KPPoint] = Field(..., min_length=1)
    kp_set_mode: Literal["single", "all_required", "compensatory"]
    cognitive_level: Literal[
        "remember", "understand", "apply", "analyze", "evaluate", "create"
    ]
    gradeband: Literal["L", "M", "H"]
    graph_release: str
    steps: Optional[list[ObjectiveStep]] = None


# ────────────────────────────────────────────────────────────────────
# slots 块：槽名 → Slot 定义
# ────────────────────────────────────────────────────────────────────
class Slot(BaseModel):
    """单个槽定义。

    type 必须在 ALLOWED_SLOT_TYPES 内（Linter 强制，验收 §2）；
    difficulty_relevant 为布尔标志，标记该槽变更是否触发难度重估（T-W2-006）。
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="槽类型，必须在 ALLOWED_SLOT_TYPES 内")
    difficulty_relevant: bool = Field(
        ..., description="该槽变更是否影响难度（验收 §2：必须为 boolean）"
    )
    # 取值域（可选，按 type 语义解释）
    min: Optional[Any] = Field(default=None, description="下界（数值类型有效）")
    max: Optional[Any] = Field(default=None, description="上界（数值类型有效）")
    choices: Optional[list[Any]] = Field(
        default=None, description="choice 类型的候选枚举"
    )
    unit: Optional[str] = Field(default=None, description="单位 id（数值槽可选）")


# ────────────────────────────────────────────────────────────────────
# variation_axes 块：六变式轴 → 槽子集
# ────────────────────────────────────────────────────────────────────
class VariationAxis(BaseModel):
    """单条变式轴：按 axis_id 取 slots 子集重采样，其余槽冻结。

    slots 列表中的槽名必须存在于 spec.slots（Linter 跨块校验，验收 §2）。
    """

    model_config = ConfigDict(extra="forbid")

    axis_id: str
    slots: list[str] = Field(
        ..., description="该轴可重采样的槽名子集（必须存在于 slots 块）"
    )


class VariationAxes(BaseModel):
    """variation_axes 块：变式轴集合。"""

    model_config = ConfigDict(extra="forbid")

    axes: list[VariationAxis] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# presentation 块：无逻辑插值 + 图形 DSL
# ────────────────────────────────────────────────────────────────────
class PresentationBlock(BaseModel):
    """单个呈现块：纯插值模板，禁止控制流（架构 v2 §4.1 无逻辑插值）。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., description="块类型（text/image/table/graphic 等）")
    template: str = Field(
        ..., description="插值模板，用 {slot_name} 引用槽值；禁止 if/for"
    )


class Presentation(BaseModel):
    """presentation 块：题面语义 AST（块序列）。"""

    model_config = ConfigDict(extra="forbid")

    blocks: list[PresentationBlock] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# answer_program 块：安全表达式 + 学科函数库
# ────────────────────────────────────────────────────────────────────
class AnswerProgram(BaseModel):
    """answer_program 块：计算正解的安全表达式。

    expression 由 T-W2-002 的安全求值器执行；学科函数库通过白名单注入
    （架构 v2 §4.1：学科复杂运算进学科函数库，DSL 语法保持稳定）。
    本块只存文本，不执行求值（求值在 instantiation/engine，T-W2-004）。
    """

    model_config = ConfigDict(extra="forbid")

    expression: str = Field(..., description="安全表达式（纯函数，无 IO/无循环）")
    returns: str = Field(..., description="返回值类型描述（如 'number'/'string'）")


# ────────────────────────────────────────────────────────────────────
# distractor_rules 块：干扰项 = f(错误类型, 槽值)，选项绑 error_type_id
# ────────────────────────────────────────────────────────────────────
class DistractorRule(BaseModel):
    """单条干扰项规则。

    rule_type=deterministic：用 expression（安全表达式）计算干扰项值；
    rule_type=corpus_sample：返回带 corpus_ref 的占位，等待 B 线语料装配（T-W2-017）。
    每条规则绑定一个 error_type_id（选项→错误类型确定映射，§4.5）。
    """

    model_config = ConfigDict(extra="forbid")

    rule_type: Literal["deterministic", "corpus_sample"]
    error_type_id: str = Field(..., description="该干扰项绑定的错误类型 id")
    expression: Optional[str] = Field(
        default=None,
        description="deterministic 规则的安全表达式（求值产生干扰项值）",
    )
    corpus_ref: Optional[str] = Field(
        default=None,
        description="corpus_sample 规则的语料库引用（B 线接入前为占位）",
    )
    label: Optional[str] = Field(
        default=None, description="干扰项显示标签（可选，由生成器填充）"
    )


class DistractorRules(BaseModel):
    """distractor_rules 块：干扰项规则集合。"""

    model_config = ConfigDict(extra="forbid")

    rules: list[DistractorRule] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 顶层：母题 Spec（六大块）
# ────────────────────────────────────────────────────────────────────
class ItemTemplateSpec(BaseModel):
    """母题 DSL v1 顶层结构：六大块（架构 v2 §4.1）。

    对应 item_template_version.spec 字段（item-model.md §2.3）。
    本模型用于结构与静态校验；实例化在 instantiation/engine（T-W2-004）。
    """

    model_config = ConfigDict(extra="forbid")

    objective: Objective
    slots: dict[str, Slot] = Field(
        ..., description="槽名 → 槽定义（至少 1 个槽）"
    )
    variation_axes: VariationAxes
    presentation: Presentation
    answer_program: AnswerProgram
    distractor_rules: DistractorRules


__all__ = [
    "ALLOWED_SLOT_TYPES",
    "AnswerProgram",
    "DistractorRule",
    "DistractorRules",
    "ItemTemplateSpec",
    "KPPoint",
    "Objective",
    "ObjectiveStep",
    "Presentation",
    "PresentationBlock",
    "Slot",
    "VariationAxes",
    "VariationAxis",
]
