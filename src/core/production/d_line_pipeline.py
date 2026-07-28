"""T-W4-021 D 线端到端流水线：蓝图→题→量规评分→影子入库.

落地架构 v2 §4.1 D 线「命题蓝图库」与 §4.5 量规评分的端到端串联：
  选命题蓝图 → 按 A 线模板实例化开放式题目 → 校验门（结构/许可/量规完整性）
  → 签发入库 → 影子模式评分器就绪。

D 线流水线把前序卡片产出粘合为一条可执行链：
  - T-W4-017 ``Blueprint`` / ``RubricTemplate`` —— 命题蓝图与量规模板
  - T-W4-018 composition/picture_writing 模板 —— A 线母题（开放式作答）
  - T-W4-019 ``AIRubricScorer`` —— 量规即数据的评分器（注册表）
  - T-W4-020 ``shadow_mode`` —— 影子运行（评分器就绪状态的验证资产）
  - T-W1-007 ``publish_item_version`` —— 入库唯一路径（门强制）
  - T-W2-010 ``run_gate`` —— 校验门编排（签发证书）

验收对照：
  §1 ``run_d_pipeline(blueprint_id, params)`` 返回入库后的 item_id 与门证书 id。
  §2 入库题目 scoring_ref 指向 ai_rubric，量规模板嵌入题目元数据（scorer_params）。
  §3 校验门验证量规模板完整性（维度齐全/分值合计正确/等级描述非空）。
  §4 ``make accept TASK=T-W4-021`` 全绿。
  §5 不 import 任何学科包/学段包（宪法 A5/X6）。

为什么 D 线流水线不直接 import 学科包模板：核心域零学科特判（X6）。命题蓝图经
``register_d_line_blueprint`` 由调用方（学科包装载器/教研后台）注入模板版本 dict、
``RubricTemplate`` 与 ``pack_digest``；本模块只做编排，不感知学科语义。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.orchestrator import GateOutcome, run_gate
from src.core.gate.policy.loader import GatePolicy
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)
from src.core.instantiation.engine import ENGINE_DIGEST, instantiate
from src.core.production.blueprint_schema import Blueprint, GradeBandSpec, WritingType
from src.core.production.rubric_template import RubricTemplate
from src.core.scoring.rubric_parser import ParsedRubric, parse_rubric
from src.core.content.writer import publish_item_version
from src.core.models.item_template import ItemTemplate
from src.core.models.item_template_version import ItemTemplateVersion


# ────────────────────────────────────────────────────────────────────
# RubricCompletenessValidator：量规完整性校验器（验收③）
# ────────────────────────────────────────────────────────────────────


class RubricCompletenessValidator(Validator):
    """量规完整性验证器（验收③：维度齐全/分值合计正确/等级描述非空）.

    从 ``ctx.artifact_payload['scoring_ref']['scorer_params']['rubric']`` 取量规，
    复用 ``parse_rubric``（T-W4-019）做结构校验，并额外校验分值合计一致性。

    校验项（任一失败即 fail）：
      1. scoring_ref.scorer_params.rubric 存在且为 dict/RubricTemplate；
      2. ``parse_rubric`` 成功（dimensions 非空 / 每维度 id·name·anchors·score_bands 齐全）；
      3. 等级描述（anchors）逐条非空字符串；
      4. ``total_max_score == sum(dimensions.max_score)``（分值合计正确）.

    为什么复用 parse_rubric 而非自写校验：量规结构契约单一来源（scorer.yaml
    ai_rubric.params_schema.rubric），parse_rubric 已覆盖结构校验；本验证器只补
    「分值合计」与「描述非空」两项 parse_rubric 未强制的语义校验。
    """

    validator_id: ClassVar[str] = "rubric_completeness"
    version: ClassVar[str] = "1.0.0+d-line"
    blocking: ClassVar[bool] = True  # 量规不完整 → 阻断发布
    cost_tier: ClassVar[str] = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        payload = ctx.artifact_payload
        if not isinstance(payload, dict):
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "artifact_payload 非 dict，无法读取 scoring_ref"},
                confidence=Decimal("1.000"),
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

        scoring_ref = payload.get("scoring_ref") or {}
        if not isinstance(scoring_ref, dict):
            return self._fail(start, "scoring_ref 非 dict")

        scorer_params = scoring_ref.get("scorer_params") or {}
        rubric = scorer_params.get("rubric")
        if rubric is None:
            return self._fail(
                start, "scoring_ref.scorer_params 缺 rubric（量规未嵌入题目元数据）"
            )

        # §3.1 结构校验：parse_rubric 覆盖 dimensions/anchors/score_bands 齐全性
        try:
            parsed: ParsedRubric = parse_rubric(rubric)
        except ValueError as e:
            return self._fail(start, f"量规结构非法：{e}", rubric=rubric)

        # §3.2 等级描述（anchors）逐条非空字符串
        empty_anchors: list[str] = []
        for dim in parsed.dimensions:
            for i, anchor in enumerate(dim.anchors):
                if not isinstance(anchor, str) or not anchor.strip():
                    empty_anchors.append(f"{dim.id}.anchor#{i}")
        if empty_anchors:
            return self._fail(
                start, f"等级描述为空：{empty_anchors}", rubric=rubric
            )

        # §3.3 分值合计：total_max_score == sum(dimensions.max_score)
        actual_total = sum(d.max_score for d in parsed.dimensions)
        if abs(actual_total - parsed.total_max_score) > 1e-9:
            return self._fail(
                start,
                f"分值合计不正确：声明 {parsed.total_max_score}，"
                f"实际维度满分合计 {actual_total}",
                rubric=rubric,
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "dimensions": [d.id for d in parsed.dimensions],
                "total_max_score": parsed.total_max_score,
                "checked": ["structure", "anchors_non_empty", "score_total"],
            },
            confidence=Decimal("1.000"),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    def _fail(
        self, start: float, reason: str, *, rubric: Any = None
    ) -> ValidatorResult:
        """构造 fail 结果（阻断发布）."""
        evidence: dict[str, Any] = {"reason": reason, "validator": self.validator_id}
        if rubric is not None:
            # 记录量规类型便于诊断（不记录完整 payload，避免日志膨胀）
            evidence["rubric_type"] = type(rubric).__name__
        return self._timed_result(
            verdict="fail",
            evidence=evidence,
            confidence=Decimal("1.000"),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )


# 模块加载时注册到 platform 桶（与 SchemaValidator 等通用验证器同模式）
register_validator("platform", RubricCompletenessValidator)


# ────────────────────────────────────────────────────────────────────
# 命题蓝图注册表（核心域不 import 学科包；由调用方注入）
# ────────────────────────────────────────────────────────────────────


class _BlueprintEntry(BaseModel):
    """注册表条目：蓝图 + 量规 + 模板版本 + 学科包摘要."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    blueprint: Blueprint
    rubric: RubricTemplate
    template_version: dict[str, Any]
    pack_digest: str


# 模块级注册表：blueprint_id → _BlueprintEntry
# 为什么模块级而非 DB：T-W4-021 只验「流水线可执行」，蓝图 DB 持久化（迁移 0018
# 的 blueprint/rubric_template 表）由教研后台后续波次接入；本注册表供测试与
# 早期集成注入。核心域不 import 学科包，模板由调用方注入（X6 等价）。
_BLUEPRINT_REGISTRY: dict[str, _BlueprintEntry] = {}


def register_d_line_blueprint(
    blueprint_id: str,
    *,
    blueprint: Blueprint,
    rubric: RubricTemplate,
    template_version: dict[str, Any],
    pack_digest: str,
) -> None:
    """注册一条 D 线命题蓝图（供 ``run_d_pipeline`` 按 id 查找）.

    Args:
        blueprint_id: 蓝图 id（须与 ``Blueprint.blueprint_id`` 一致）.
        blueprint: 命题蓝图（写作类型/学段参数/主题池/量规引用）.
        rubric: 量规模板（嵌入题目 scoring_ref.scorer_params）.
        template_version: A 线母题模板版本 dict（composition/picture_writing.yaml）.
        pack_digest: 学科包摘要（sha256:...；公式一 item_version_id 输入）.
    """
    if blueprint_id != blueprint.blueprint_id:
        raise ValueError(
            f"blueprint_id 参数 {blueprint_id!r} 与 Blueprint.blueprint_id"
            f" {blueprint.blueprint_id!r} 不一致"
        )
    _BLUEPRINT_REGISTRY[blueprint_id] = _BlueprintEntry(
        blueprint=blueprint,
        rubric=rubric,
        template_version=template_version,
        pack_digest=pack_digest,
    )


def get_d_line_blueprint(blueprint_id: str) -> _BlueprintEntry:
    """取已注册的 D 线蓝图条目."""
    entry = _BLUEPRINT_REGISTRY.get(blueprint_id)
    if entry is None:
        raise KeyError(f"蓝图 {blueprint_id!r} 未在 D 线注册表注册")
    return entry


def reset_d_line_registry() -> None:
    """清空注册表（测试隔离用）."""
    _BLUEPRINT_REGISTRY.clear()


# ────────────────────────────────────────────────────────────────────
# 实例化参数构建
# ────────────────────────────────────────────────────────────────────


def _build_instantiate_params(
    blueprint: Blueprint,
    params: dict[str, Any],
    spec: GradeBandSpec,
) -> dict[str, Any]:
    """按写作类型与学段 spec 构建实例化参数.

    composition: {topic, word_count_min, word_count_max, time_limit_minutes}
    picture_writing: {picture_ref, prompt, word_count_min, word_count_max, time_limit_minutes}
    """
    base: dict[str, Any] = {
        "word_count_min": spec.word_count_min,
        "word_count_max": spec.word_count_max,
        "time_limit_minutes": spec.time_limit_minutes,
    }
    if blueprint.writing_type == "composition":
        topic = params.get("topic")
        if not topic:
            raise ValueError("composition 蓝图 params 缺 topic")
        base["topic"] = str(topic)
    elif blueprint.writing_type == "picture_writing":
        picture_ref = params.get("picture_ref")
        prompt = params.get("prompt")
        if not picture_ref:
            raise ValueError("picture_writing 蓝图 params 缺 picture_ref")
        if not prompt:
            raise ValueError("picture_writing 蓝图 params 缺 prompt")
        base["picture_ref"] = str(picture_ref)
        base["prompt"] = str(prompt)
    else:
        raise ValueError(f"未知 writing_type: {blueprint.writing_type!r}")
    return base


def _select_grade_band_spec(
    blueprint: Blueprint, grade_band: str
) -> GradeBandSpec:
    """从蓝图取指定学段的 spec（须存在）."""
    for spec in blueprint.grade_band_specs:
        if spec.grade_band == grade_band:
            return spec
    raise ValueError(
        f"蓝图 {blueprint.blueprint_id!r} 缺学段 {grade_band!r} 的 spec"
    )


# ────────────────────────────────────────────────────────────────────
# 母题版本幂等落库（满足 item.template_version_id FK，契约 §2.3）
# ────────────────────────────────────────────────────────────────────


async def _ensure_template_version(
    db: AsyncSession,
    template_version: dict[str, Any],
    pack_id: str,
) -> None:
    """幂等落库母题身份 + 母题版本（status=draft）.

    ``item.template_version_id`` 是 FK 指向 ``item_template_version``（契约 §2.3）；
    D 线流水线端到端入库 item 前必须确保母题版本行已存在，否则 FK 违反。

    幂等：同 ``template_id`` / ``template_version_id`` 已存在则复用（D1 只增不改，
    禁止 UPDATE/DELETE 既有母题版本）。母题不直接过门（§2.3 status 仅
    draft/published/retired），此处用 draft 最小满足 FK；母题正式发布是 A 线职责。

    为什么放核心域而非学科包：模板版本 dict 由 ``register_d_line_blueprint`` 调用方
    注入（学科包装载器），本模块只做编排入库，不感知学科语义（X6 等价）。
    """
    template_id = template_version["template_id"]
    template_version_id = template_version["template_version_id"]

    if await db.get(ItemTemplate, template_id) is None:
        db.add(
            ItemTemplate(
                template_id=template_id,
                pack_id=pack_id,
                current_version_id=None,
            )
        )
        await db.flush()  # 让 item_template 先落库，满足 item_template_version.template_id FK

    if await db.get(ItemTemplateVersion, template_version_id) is None:
        db.add(
            ItemTemplateVersion(
                template_version_id=template_version_id,
                template_id=template_id,
                dsl_version=template_version["dsl_version"],
                spec=template_version["spec"],
                status="draft",
            )
        )
        await db.flush()


# ────────────────────────────────────────────────────────────────────
# version_data 构建（A 线实例化产物 → writer 入库格式）
# ────────────────────────────────────────────────────────────────────


def _build_version_data(
    item_version: dict[str, Any],
    *,
    blueprint: Blueprint,
    pack_digest: str,
    template_version: dict[str, Any],
    locale: str = "zh-CN",
) -> dict[str, Any]:
    """把实例化产物转为 ``publish_item_version`` 所需的 version_data.

    A 级产物（tier='A'）走公式一 item_version_id；writer 的 _compute_item_version_id
    需要完整公式一参数（template_version_digest / normalized_params / pack_digest /
    engine_digest / corpus_digests）。本函数从实例化产物的 lineage 中取 normalized_params，
    并补齐其余参数，确保 writer 重算出与 instantiate 相同的 item_version_id（D3 可复现）。
    """
    template_version_id = template_version["template_version_id"]
    lineage = item_version.get("lineage") or {}
    normalized_params = (
        lineage.get("params", {}).get("normalized", {})
        if isinstance(lineage.get("params"), dict)
        else {}
    )
    return {
        "pack_id": blueprint.pack_id,
        "tier": "A",
        "status": "published",
        "objective": item_version["objective"],
        "interaction_ref": item_version["interaction_ref"],
        "content": item_version["content"],
        "scoring_ref": item_version["scoring_ref"],
        "error_bindings": item_version.get("error_bindings", []),
        "lineage": lineage,
        # 公式一参数（与 instantiate 输入对齐，writer 重算得同 id）
        "template_version_id": template_version_id,
        "template_version_digest": template_version_id,
        "normalized_params": normalized_params,
        "pack_digest": pack_digest,
        "engine_digest": ENGINE_DIGEST,
        "corpus_digests": [],
        "locale": locale,
        # published 非 draft 需 rendered_snapshot（writer 缺省占位）
        "rendered_snapshot": {"placeholder": True, "note": "D 线流水线占位快照"},
    }


# ────────────────────────────────────────────────────────────────────
# 流水线结果
# ────────────────────────────────────────────────────────────────────


class DPipelineResult(BaseModel):
    """D 线流水线结果（验收①：item_id 与门证书 id）.

    - item_id: 入库后的 item id（None=未入库，门未通过）.
    - item_version_id: 实例化产物 id（无论门是否通过都有值，便于诊断）.
    - cert_id: 门证书 id（仅门通过时非空）.
    - final_verdict: 门综合判定 pass/fail/review.
    - gate_outcome: 完整门结果（含各验证器留痕）.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    item_id: str | None = None
    item_version_id: str
    cert_id: str | None = None
    final_verdict: str
    gate_outcome: GateOutcome | None = None


# ────────────────────────────────────────────────────────────────────
# 主入口：run_d_pipeline
# ────────────────────────────────────────────────────────────────────


async def run_d_pipeline(
    blueprint_id: str,
    params: dict[str, Any],
    *,
    db: AsyncSession,
    policy: GatePolicy,
    grade_band: str | None = None,
    locale: str = "zh-CN",
    issued_by: str = "d-line-pipeline",
) -> DPipelineResult:
    """D 线端到端流水线：蓝图→实例化→校验门→签发入库（验收①）.

    流程：
      1. 从注册表取蓝图 + 量规 + 模板 + pack_digest（``get_d_line_blueprint``）；
      2. 按学段 spec 构建实例化参数（``_build_instantiate_params``）；
      3. A 线实例化（``instantiate``），scoring_ref 指向 ai_rubric + 量规嵌入
         scorer_params（验收②）；
      4. 运行校验门（``run_gate``），RubricCompletenessValidator 校验量规完整性
         （验收③：维度齐全/分值合计正确/等级描述非空）；
      5. 门通过 → ``publish_item_version`` 入库（status='published' + 证书）；
      6. 返回 ``DPipelineResult``（item_id + cert_id）.

    Args:
        blueprint_id: 命题蓝图 id（须已注册）.
        params: 实例化参数（composition: {topic}; picture_writing: {picture_ref, prompt}；
            可选 grade_band 覆盖参数中的学段）.
        db: 异步会话（门落库 + item_version 入库）.
        policy: 门策略（链须含 rubric_completeness）.
        grade_band: 学段 L/M/H；None 时从 params['grade_band'] 取，再缺省 'M'.
        locale: 语言/地区，默认 zh-CN.
        issued_by: 门证书签发人 id（落 gate_certificate.issued_by）.

    Returns:
        DPipelineResult：含 item_id / item_version_id / cert_id / final_verdict.

    Raises:
        KeyError: 蓝图未注册.
        ValueError: 参数缺失 / 学段 spec 不存在 / 实例化失败.
    """
    # 1. 取蓝图
    entry = get_d_line_blueprint(blueprint_id)
    blueprint = entry.blueprint
    rubric = entry.rubric
    template_version = entry.template_version
    pack_digest = entry.pack_digest

    # 2. 学段 + 实例化参数
    gb = grade_band or params.get("grade_band", "M")
    spec = _select_grade_band_spec(blueprint, str(gb))
    inst_params = _build_instantiate_params(blueprint, params, spec)

    # 3. A 线实例化（验收②：scoring_ref 指向 ai_rubric + 量规嵌入）
    result = instantiate(
        template_version,
        inst_params,
        pack_digest=pack_digest,
        interaction_id="writing",
        scorer_id="ai_rubric",
        scorer_params={"rubric": rubric.to_scorer_params()},
        locale=locale,
        signed_by="d-line-pipeline",
    )
    item_version = result.model_dump()
    item_version_id = item_version["item_version_id"]

    # 4. 校验门（验收③：RubricCompletenessValidator 校验量规完整性）
    ctx = GateContext(
        artifact_type="item",
        pack_id=blueprint.pack_id,
        artifact_payload=item_version,
    )
    gate_outcome = await run_gate(
        artifact_ref=item_version_id,
        artifact_type="item",
        pack_id=blueprint.pack_id,
        ctx=ctx,
        policy=policy,
        db=db,
        issued_by=issued_by,
    )

    # 5. 门未通过 → 不入库（返回 item_version_id 便于诊断，item_id/cert_id 为 None）
    if gate_outcome.final_verdict != "pass":
        return DPipelineResult(
            item_id=None,
            item_version_id=item_version_id,
            cert_id=None,
            final_verdict=gate_outcome.final_verdict,
            gate_outcome=gate_outcome,
        )

    # 6. 门通过 → 入库（验收①：返回 item_id 与 cert_id）
    # 6a. 确保母题版本行已存在（item.template_version_id FK，契约 §2.3）
    await _ensure_template_version(db, template_version, blueprint.pack_id)
    # 6b. 入库 item + item_version
    version_data = _build_version_data(
        item_version,
        blueprint=blueprint,
        pack_digest=pack_digest,
        template_version=template_version,
        locale=locale,
    )
    published = await publish_item_version(
        item_id=None,
        version_data=version_data,
        gate_certificate_id=gate_outcome.cert_id,
        db=db,
    )

    return DPipelineResult(
        item_id=published["item_id"],
        item_version_id=published["item_version_id"],
        cert_id=gate_outcome.cert_id,
        final_verdict="pass",
        gate_outcome=gate_outcome,
    )


__all__ = [
    "DPipelineResult",
    "RubricCompletenessValidator",
    "get_d_line_blueprint",
    "register_d_line_blueprint",
    "reset_d_line_registry",
    "run_d_pipeline",
]
