"""B 线语料装配线 v1（T-W2-017）.

实现「框架模板 + 语料库填充 → ItemVersion dict」装配。

地位：架构 v2 §4.1 B 线 · 语料装配线（半模板级）。与 A 线对等：
- A 线：母题 DSL + 实例化引擎，产物走公式一（compute_instance_id）
- B 线：框架模板 + 语料库填充，产物走公式二（compute_canonical_item_version_id）
  （任务卡 §验收 #2 允许「公式二/一」；本实现采用公式二以避免跨 worktree
  强依赖 A 线引擎，且 B 线产物结构更接近 C/D 级「内容快照」语义。）

产物特点（验收 §3）：
- lineage.tier = "B"
- lineage.corpus_refs 非空，每条含 corpus_version_id + digest
- lineage.template_version_id + lineage.params 保留（B 线核心谱系）
- 同 (template, corpus_refs, params, locale) 多次装配必得同一 item_version_id（D3）

为什么 b_assembler 不写 DB：架构 v2 §4.1 四条线均统一汇入"内容写入服务"
（content writer）入库；本模块只做纯计算产出 dict，入库由 publish_item_version
承载（与 A 线引擎一致——A 线 instantiate() 也不写 DB）。

宪法 A5/A7：本模块不 import 任何学科包/学段包（仅依赖核心域 content_addressing
与 item_version 模型；学科函数库通过 corpus_refs 间接消费，不直接引用）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.models.content_addressing import compute_canonical_item_version_id


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class BAssemblerError(ValueError):
    """B 线装配失败基类."""


class MissingCorpusError(BAssemblerError):
    """corpus_refs 为空或格式不合法（验收 §3：必须非空）.

    架构 v2 §4.1 B 线：语料库是一等数据资产——产物必须携带 corpus_refs。
    """


class SlotValidationError(BAssemblerError):
    """params 与 slots 声明不匹配（必填缺失/类型不符/未知槽）."""


# ────────────────────────────────────────────────────────────────────
# Pydantic schema
# ────────────────────────────────────────────────────────────────────

# B 线简化类型枚举（与 A 线 DSL Slot.type 子集对齐；不引入 array/object
# 是因为 B 线"半模板"性质：参数均为标量，复合结构应升级为 A 线母题 DSL）
TypeT = Literal[
    "integer", "number", "string", "boolean",
]


class SlotSpec(BaseModel):
    """B 线框架模板的槽位声明.

    比 A 线 DSL Slot 简化：仅 name/type/required/description，
    不含取值域/槽间约束（B 线的"半模板"性质：结构参数化但参数域验证可宽松；
    严格域校验由校验门承载）。
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    type: TypeT
    required: bool = True
    description: Optional[str] = None


class BlockSpec(BaseModel):
    """B 线框架模板的题面块.

    用 {slot_name} 占位符引用 slots；装配时替换为 params 实际值。
    保留 template 字段在产物中用于谱系追溯（让审计能看到原始模板字符串）。
    """
    # 为什么 extra="allow"：题面块可能携带交互特化字段（如 numeric_blank 的
    # precision/single_choice 的 options），本处不细化以保持 B 线对交互类型的中立。
    model_config = ConfigDict(extra="allow")

    type: str = Field(..., min_length=1)
    # 模板字符串，含 {slot_name} 占位符；纯静态块可为 None
    template: Optional[str] = None
    # 已渲染的静态内容（type 为 image/audio 等无插值时使用）
    value: Optional[Any] = None


class FrameworkTemplate(BaseModel):
    """B 线框架模板（结构参数化）.

    地位：B 线生产入口；含 slots 声明 + presentation 模板 + 评分器配置。
    不同于 A 线母题 DSL（六大块完整版含 variation_axes/answer_program/
    distractor_rules），本模板仅含 B 线必需子集：slots / presentation /
    objective / interaction_ref / scoring_ref / error_bindings。

    为什么不含 answer_program：B 线"半模板"的正解由调用方在 params.answer
    直接给出（或由更上游的装配器调用 A 线 expr_eval 算出后注入 params）；
    本模块不做表达式求值，避免与 A 线 expr_eval 模块耦合。
    """
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(..., min_length=1)
    template_version: str = Field(..., min_length=1)
    pack_id: str = Field(..., min_length=1)
    slots: list[SlotSpec] = Field(..., min_length=1)
    presentation: list[BlockSpec] = Field(..., min_length=1)
    objective: dict[str, Any]
    interaction_ref: dict[str, Any]
    scoring_ref: dict[str, Any]
    error_bindings: list[dict[str, Any]] = Field(default_factory=list)
    description: Optional[str] = None

    @field_validator("template_version")
    @classmethod
    def _version_pattern(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"template_version 应为 semver，实际 {v!r}")
        return v


class CorpusRef(BaseModel):
    """语料库版本引用（lineage.corpus_refs 元素）.

    与 src/core/models/item_version.CorpusRef 字段一致：
    corpus_version_id + digest。B 线产物必须非空（验收 §3）。
    """
    model_config = ConfigDict(extra="forbid")

    corpus_version_id: str = Field(..., min_length=1)
    digest: str = Field(..., min_length=1)


# ────────────────────────────────────────────────────────────────────
# 装配核心
# ────────────────────────────────────────────────────────────────────

# 占位符正则：{slot_name} 形式（与 Python str.format 区分：不解析 !r/:fmt 后缀）
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# 类型映射：slot.type → 期望的 Python 类型元组
# 为什么 bool 单独排除：Python 中 bool 是 int 子类，isinstance(True, int) == True，
# 但 B 线语义上不允许 bool 充当 integer/number（防止 true→1 误用）。
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "integer": (int,),
    "number": (int, float),
    "string": (str,),
    "boolean": (bool,),
}


def _validate_params(
    slots: list[SlotSpec], params: dict[str, Any]
) -> None:
    """校验 params 与 slots 声明对齐.

    规则：
    - required slot 必须在 params 中（缺失 → SlotValidationError）
    - params 中的 key 必须在 slots 声明里（未知槽 → SlotValidationError，
      防止拼写错误悄悄通过）
    - 类型检查（int/float/str/bool 基础匹配；bool 不能充当 integer/number）

    不做的事：
    - 取值域校验（min/max/枚举）→ 由校验门承载
    - 槽间约束 → 由校验门或 A 线 DSL 承载
    """
    slot_by_name: dict[str, SlotSpec] = {s.name: s for s in slots}

    # 必填检查
    for slot in slots:
        if slot.required and slot.name not in params:
            raise SlotValidationError(
                f"必填槽 {slot.name!r} 未在 params 中提供"
            )

    # 未知参数检查
    for key in params:
        if key not in slot_by_name:
            raise SlotValidationError(
                f"params 含未知槽 {key!r}（未在 template.slots 声明）"
            )

    # 类型检查
    for slot in slots:
        if slot.name not in params:
            continue
        val = params[slot.name]
        expected = _TYPE_MAP.get(slot.type)
        if expected is None:
            continue  # 未知类型不校验（向前兼容）
        # bool 不能充当 integer/number
        if slot.type in ("integer", "number") and isinstance(val, bool):
            raise SlotValidationError(
                f"槽 {slot.name!r} 期望 {slot.type}，实际 bool"
                "（bool 不能充当数值，防止 true→1 误用）"
            )
        if not isinstance(val, expected):
            raise SlotValidationError(
                f"槽 {slot.name!r} 期望 {slot.type}，"
                f"实际 {type(val).__name__}"
            )


def _interpolate_blocks(
    blocks: list[BlockSpec], params: dict[str, Any]
) -> list[dict[str, Any]]:
    """插值 presentation.blocks：{slot_name} → params 值.

    规则：
    - block.template 中的 {slot_name} 被替换为 params 中对应值的字符串形式
    - 替换后的字符串写入 block.value（输出时调用方用 value 字段）
    - block.template 保留在产物中用于谱系追溯
    - block.template 引用未知槽 → SlotValidationError

    为什么不用 str.format：format 会解析 {:fmt} / {!r} 等后缀，B 线模板
    不需要格式化能力，简化为纯占位符替换更安全（防止恶意格式串攻击）。
    """
    rendered: list[dict[str, Any]] = []
    for blk in blocks:
        out: dict[str, Any] = {"type": blk.type}

        if blk.template is not None:
            def _sub(m: "re.Match[str]") -> str:
                name = m.group(1)
                if name not in params:
                    raise SlotValidationError(
                        f"模板引用未知槽 {name!r}（不在 params 中）"
                    )
                return str(params[name])

            out["value"] = _PLACEHOLDER_RE.sub(_sub, blk.template)
            out["template"] = blk.template  # 保留模板用于谱系
        elif blk.value is not None:
            out["value"] = blk.value

        # extra 字段原样保留（交互特化字段）
        for k, v in blk.__dict__.items():
            if k in ("type", "template", "value"):
                continue
            if v is not None:
                out[k] = v

        rendered.append(out)
    return rendered


def assemble(
    template: Union[FrameworkTemplate, dict[str, Any]],
    corpus_refs: list[Union[CorpusRef, dict[str, Any]]],
    params: dict[str, Any],
    *,
    locale: str = "zh-CN",
    signed_at: Optional[str] = None,
) -> dict[str, Any]:
    """B 线装配：框架模板 + 语料库填充 → ItemVersion dict.

    步骤（架构 v2 §4.1 B 线）：
      1. coerce template/corpus_refs 为 Pydantic 模型
      2. 校验 corpus_refs 非空（验收 §3 / 架构 v2 §4.1：语料库为一等数据资产）
      3. 校验 params 与 template.slots 对齐
      4. 插值 presentation.blocks：{slot_name} → params 值
      5. 构造六大块
      6. compute_canonical_item_version_id（公式二，D3 可复现）
      7. 构造 lineage：tier="B"，corpus_refs 非空
      8. 返回 ItemVersion dict（status="draft"，不入库——入库由 writer 承载）

    Args:
        template: FrameworkTemplate 实例或 dict（自动 coerce）。
        corpus_refs: 语料库版本引用列表，每项含 corpus_version_id + digest。
            必须非空（验收 §3）。
        params: 槽值字典，key 必须匹配 template.slots 声明。
        locale: 语言/地区，默认 zh-CN。
        signed_at: 签发时间 ISO 字符串；None 用当前 UTC（影响 lineage.signed_at，
            不影响 item_version_id——item_version_id 仅依赖六大块+locale）。

    Returns:
        ItemVersion dict，含 item_version_id / item_id / status / 六大块
        （objective/interaction_ref/content/scoring_ref/error_bindings/lineage）。
        B 级产物 item_id = item_version_id（自引用，与 A 级 A/B 一致）。

    Raises:
        MissingCorpusError: corpus_refs 为空。
        SlotValidationError: params 与 slots 不匹配（必填缺失/类型不符/未知槽）。
        BAssemblerError: 其他装配失败。
    """
    # ── coerce to Pydantic ──
    if isinstance(template, dict):
        template = FrameworkTemplate(**template)
    coerced_refs: list[CorpusRef] = [
        CorpusRef(**r) if isinstance(r, dict) else r for r in corpus_refs
    ]

    # ── 验收 §3：corpus_refs 必须非空 ──
    if not coerced_refs:
        raise MissingCorpusError(
            "B 线装配必须携带非空 corpus_refs"
            "（架构 v2 §4.1 B 线：语料库为一等数据资产；"
            "任务卡 §验收 #3：lineage.corpus_refs 非空）"
        )

    # ── 校验 params ──
    _validate_params(template.slots, params)

    # ── 插值 presentation ──
    rendered_blocks = _interpolate_blocks(template.presentation, params)

    # ── 构造六大块（浅拷贝防外部污染）──
    objective = dict(template.objective)
    interaction_ref = dict(template.interaction_ref)
    content = {"blocks": rendered_blocks}
    scoring_ref = dict(template.scoring_ref)
    error_bindings = list(template.error_bindings)

    # ── 构造 lineage（tier=B, corpus_refs 非空）──
    # 为什么 signed_at 不影响 item_version_id：lineage 进 content hash 的部分
    # 仅是 lineage dict 本身；signed_at 在 lineage 内，所以会改变 content hash
    # ——但调用方可固定 signed_at 来保证可复现性（测试场景）。
    # 生产场景由调用方注入确定时间戳（如批次时间），不依赖 datetime.now()。
    now = signed_at or datetime.now(timezone.utc).isoformat()
    pipeline = {
        "id": f"{template.pack_id}.b_assembler",
        "version": template.template_version,
    }
    lineage: dict[str, Any] = {
        "tier": "B",
        "pipeline": pipeline,
        "template_version_id": template.template_id,
        "params": dict(params),  # 谱系保留参数（B 线核心信息）
        "corpus_refs": [
            {"corpus_version_id": r.corpus_version_id, "digest": r.digest}
            for r in coerced_refs
        ],
        "signed_by": "b_assembler",
        "signed_at": now,
    }

    # ── 计算 item_version_id（公式二：canonical content addressing）──
    # B 线允许公式二/一（任务卡 §验收 #2）；本实现采用公式二：
    # 不依赖 A 线实例化引擎（避免跨 worktree 强依赖），仅依赖六大块 + locale。
    item_version_id = compute_canonical_item_version_id(
        objective=objective,
        interaction_ref=interaction_ref,
        content=content,
        scoring_ref=scoring_ref,
        error_bindings=error_bindings,
        locale=locale,
    )

    # ── 构造 ItemVersion dict ──
    # B 级产物：item_id = item_version_id（自引用，与 A 级 A/B 一致）
    return {
        "item_version_id": item_version_id,
        "item_id": item_version_id,
        "status": "draft",
        "objective": objective,
        "interaction_ref": interaction_ref,
        "content": content,
        "scoring_ref": scoring_ref,
        "error_bindings": error_bindings,
        "lineage": lineage,
    }


__all__ = [
    "BAssemblerError",
    "BlockSpec",
    "CorpusRef",
    "FrameworkTemplate",
    "MissingCorpusError",
    "SlotSpec",
    "SlotValidationError",
    "TypeT",
    "assemble",
]
