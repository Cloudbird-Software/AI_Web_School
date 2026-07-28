"""C 线端到端流水线（T-W4-016 · E2E-2 承载卡）.

架构 v2 §4.1 C 线完整流：
    语篇草稿（T-W4-013 PassageDraft）
    → 难度分析（T-W4-013 analyze_difficulty）
    → 教研改写定稿（接口预留：finalized_body）
    → 许可登记（material_license approved）
    → 题组蓝图编排（T-W4-015 assemble_testlet）
    → 校验门（T-W4-014 passage 链：schema→license→fact→age→difficulty→duplicate）
    → 签发入库（published Passage + draft item_versions + testlet item_group）

事务原子性（验收 #2）：
- 全部 DB 写入使用 auto_commit=False（flush 不 commit），由 pipeline 末尾统一 commit。
- 门编排（run_gate）内部 commit 门审计记录（gate_run/gate_verdict/certificate）——
  这是 D1 校验签发账的合法持久化，不受 pipeline 回滚影响。
- 若门未通过（fail/review）：pipeline 不写 passage/items/group，返回失败原因 + 证据链。
  门审计记录已落库（合法），但 passage/items/group 不残留（验收 #2「不残留脏数据」）。
- 若门通过：pipeline 写 passage(published) + item_versions(draft) + item_group(testlet)，
  统一 commit。

完整谱系（验收 #3）：
    passage.lineage = {
        source: "ai_draft",                    # 语篇来源
        ai_generation: PassageDraft.generation_meta,  # AI 生成记录
        teacher_finalized: bool,               # 教研定稿标记
        gate_certificate_id: cert_id,          # 门证书
        issued_by: str,                        # 签发人
    }
    item_version.lineage = {
        tier: "C",
        pipeline: {id: "c-line", version: "1.0"},
        passage_id: ...,                       # 语篇关联
        group_index: int,                      # 题组内序号（契约偏差替代字段）
        signed_by: str,
        signed_at: ISO8601,
    }

宪法 A5/X6：不 import 学科包/学段包。
宪法 D2：published passage 必须持 gate_certificate_id（publish_passage 门强制 + DB CHECK）。
"""
from __future__ import annotations

import hashlib
import ulid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.content.difficulty_analyzer import analyze_difficulty
from src.core.content.item_group_assembler import (
    assemble_item_group,
    blueprint_lineage,
)
from src.core.content.passage_generator import PassageDraft
from src.core.content.passage_schema import DifficultyTarget, PromptDirection
from src.core.content.testlet_blueprint import (
    ItemSpec,
    TestletBlueprint,
    assemble_testlet,
)
from src.core.content.writer import (
    publish_item_group,
    publish_item_version,
    publish_passage,
)
from src.core.gate.orchestrator.orchestrator import GateOutcome, run_gate
from src.core.gate.policy.loader import GatePolicy, load_default_policy
from src.core.gate.validator import GateContext
from src.core.models.material_license import MaterialLicense


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CPipelineLineage:
    """C 线流水线完整谱系（验收 #3）.

    Attributes:
        passage_id: 入库语篇 id。
        source: 语篇来源（"ai_draft" / "teacher_draft"）。
        ai_generation: AI 生成元数据（模型/prompt_hash/token/台账 call_id）。
        teacher_finalized: 是否经教研改写定稿。
        gate_certificate_id: 门证书 id。
        issued_by: 签发人。
        item_version_ids: 有序子题版本 id 列表（与 group_index 对齐）。
        item_group_id: 题组 id。
    """

    passage_id: str
    source: str
    ai_generation: dict[str, Any]
    teacher_finalized: bool
    gate_certificate_id: str
    issued_by: str
    item_version_ids: list[str]
    item_group_id: str


@dataclass(frozen=True)
class CPipelineSuccess:
    """C 线流水线成功结果（验收 #1）."""

    passage_id: str
    item_group_id: str
    cert_id: str
    lineage: CPipelineLineage


@dataclass(frozen=True)
class CPipelineFailure:
    """C 线流水线失败结果（验收 #2）.

    Attributes:
        reason: 失败原因（"gate_fail" / "gate_review" / "blueprint_error" / ...）。
        evidence_chain: 证据链（gate runs 或异常详情）。
        step: 失败发生的步骤（"difficulty" / "blueprint" / "gate" / "writeback"）。
    """

    reason: str
    step: str
    evidence_chain: list[dict[str, Any]] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────


def _content_hash(body: str) -> str:
    """语篇正文内容寻址哈希（sha256:...，D3 精神）."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _new_passage_id() -> str:
    """生成语篇 id（ULID，与 writer 风格一致）."""
    return "pass_" + str(ulid.new())


def _new_license_id() -> str:
    """生成许可 id."""
    return "lic_" + str(ulid.new())


def _kp_refs_from_direction(direction: PromptDirection) -> list[dict[str, str]]:
    """从命题方向提取 kp_refs（落 passage.kp_refs JSONB）."""
    return [
        {"dimension": kp.dimension, "code": kp.code} for kp in direction.kp_refs
    ]


def _build_item_version_data(
    spec: ItemSpec,
    *,
    passage_id: str,
    group_index: int,
    subject: str,
    grade_band: str,
    signed_by: str,
) -> dict[str, Any]:
    """根据子题规格构建 item_version 数据（draft 状态）.

    题组内子题为 draft 状态（non_goals：自动题目生成）；实际题目内容由
    后续命题流程填充。lineage 携带 passage_id + group_index（契约偏差替代字段）。
    """
    return {
        "pack_id": subject,
        "tier": "C",
        "status": "draft",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": c} for c in spec.kp_codes],
            "kp_set_mode": "all_required",
            "cognitive_level": "understand",
            "gradeband": grade_band,
            "graph_release": "graph-v1",
        },
        "interaction_ref": {
            "interaction_id": spec.interaction_type,
            "interaction_params": {},
        },
        "content": {
            "blocks": [
                {
                    "kind": "stem",
                    "template": spec.stem_hint or f"子题 {spec.spec_id}",
                    "rendered": spec.stem_hint or f"子题 {spec.spec_id}",
                }
            ]
        },
        "scoring_ref": {
            "scorer_id": spec.scoring_method,
            "scorer_params": {},
        },
        "error_bindings": [],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "c-line", "version": "1.0"},
            "passage_id": passage_id,
            "group_index": group_index,
            "signed_by": signed_by,
            "signed_at": datetime.now(timezone.utc).isoformat(),
        },
    }


# ────────────────────────────────────────────────────────────────────
# 流水线主入口
# ────────────────────────────────────────────────────────────────────


async def run_c_pipeline(
    passage_draft: PassageDraft,
    item_specs: list[ItemSpec],
    *,
    db: AsyncSession,
    finalized_body: Optional[str] = None,
    license_source: str = "ai_generated",
    license_rights_holder: str = "system",
    issued_by: str = "c-line-pipeline",
    policy: Optional[GatePolicy] = None,
    vocab_baseline: Optional[set[str]] = None,
) -> CPipelineSuccess | CPipelineFailure:
    """C 线端到端流水线（任务卡 T-W4-016 验收 #1/#2/#3）.

    流程：
    1. 难度分析（analyze_difficulty）
    2. 教研改写定稿（finalized_body 覆盖草稿正文，接口预留）
    3. 题组蓝图编排（assemble_testlet）
    4. 注册许可（material_license approved）
    5. 构建门上下文 + 运行门编排（run_gate，passage 链）
    6. 门通过 → 写 passage(published) + item_versions(draft) + item_group(testlet)
    7. 返回成功（含完整谱系）

    Args:
        passage_draft: AI 起草的语篇草稿（T-W4-013 generate_passage 产物）。
        item_specs: 子题规格列表（2-6 道）。
        db: 异步会话。
        finalized_body: 教研改写定稿正文；None 时用草稿正文（接口预留）。
        license_source: 许可来源标记。
        license_rights_holder: 权利人。
        issued_by: 签发人 id。
        policy: 门策略；None 时加载默认策略。
        vocab_baseline: 课标词表（字级集合），供难度分析与难度门消费。

    Returns:
        CPipelineSuccess | CPipelineFailure。

    Notes:
        - 门编排内部 commit 门审计记录（D1 合法持久化），不受 pipeline 回滚影响。
        - 门未通过时 passage/items/group 不写入（验收 #2 不残留脏数据）。
        - 门通过后全部业务写入统一 commit（事务原子性）。
    """
    direction = passage_draft.direction

    # ── 1. 难度分析 ──
    body = finalized_body if finalized_body is not None else passage_draft.body
    teacher_finalized = finalized_body is not None

    difficulty_target = direction.difficulty_target
    difficulty_report = analyze_difficulty(
        text=body,
        grade_band=direction.grade_band,
        vocab_baseline=vocab_baseline,
        difficulty_target=difficulty_target,
    )

    # ── 2. 题组蓝图编排 ──
    passage_id = _new_passage_id()
    try:
        blueprint: TestletBlueprint = assemble_testlet(
            passage_id=passage_id,
            item_specs=item_specs,
            ordered=True,
            shared_context={
                "passage_id": passage_id,
                "genre": direction.genre,
                "grade_band": direction.grade_band,
            },
        )
    except Exception as exc:
        return CPipelineFailure(
            reason="blueprint_error",
            step="blueprint",
            evidence_chain=[{"error": str(exc)}],
        )

    # ── 3. 注册许可（flush，不 commit） ──
    license_id = _new_license_id()
    license_row = MaterialLicense(
        license_id=license_id,
        source=license_source,
        rights_holder=license_rights_holder,
        scope="c-line-passage",
        decision="approved",
    )
    db.add(license_row)
    await db.flush()  # 让 license_validator 可查到

    # ── 4. 构建门上下文 ──
    policy = policy or load_default_policy()

    gate_payload = {
        "passage_id": passage_id,
        "body": body,
        "genre": direction.genre,
        "grade_band": direction.grade_band,
        "subject": direction.subject,
        "license_id": license_id,
        "difficulty_metrics": difficulty_report.metrics.model_dump(),
        "difficulty_target": {
            "min": difficulty_target.min,
            "max": difficulty_target.max,
        },
    }
    if vocab_baseline is not None:
        gate_payload["vocab_baseline"] = list(vocab_baseline)

    ctx = GateContext(
        artifact_type="passage",
        pack_id="platform",
        artifact_payload=gate_payload,
        db=db,
    )

    # ── 5. 运行门编排 ──
    outcome: GateOutcome = await run_gate(
        artifact_ref=passage_id,
        artifact_type="passage",
        pack_id="platform",
        ctx=ctx,
        policy=policy,
        db=db,
        issued_by=issued_by,
    )

    # 门未通过：返回失败（passage/items/group 不写入）
    if outcome.final_verdict == "fail":
        return CPipelineFailure(
            reason="gate_fail",
            step="gate",
            evidence_chain=[
                {
                    "validator_id": r.validator_id,
                    "verdict": r.verdict,
                    "evidence": r.evidence,
                    "short_circuited": r.short_circuited,
                }
                for r in outcome.runs
            ],
        )
    if outcome.final_verdict == "review":
        return CPipelineFailure(
            reason="gate_review",
            step="gate",
            evidence_chain=[
                {
                    "validator_id": r.validator_id,
                    "verdict": r.verdict,
                    "evidence": r.evidence,
                }
                for r in outcome.runs
            ],
        )

    cert_id = outcome.cert_id
    assert cert_id is not None  # final_verdict='pass' 必有 cert_id

    # ── 6. 门通过：写 passage(published) + item_versions(draft) + item_group(testlet) ──
    try:
        # 6a. 写 published passage（持 cert_id，门强制由 publish_passage + DB CHECK 兜底）
        passage_data = {
            "passage_id": passage_id,
            "content_hash": _content_hash(body),
            "body": body,
            "genre": direction.genre,
            "kp_refs": _kp_refs_from_direction(direction),
            "difficulty_metrics": difficulty_report.metrics.model_dump(),
            "license_id": license_id,
            "grade_band": direction.grade_band,
            "subject": direction.subject,
            "status": "published",
        }
        await publish_passage(
            passage_data=passage_data,
            gate_certificate_id=cert_id,
            db=db,
            auto_commit=False,
        )

        # 6b. 写 item_versions（draft，每道子题一条）
        spec_id_to_version_id: dict[str, str] = {}
        group_index_map = {
            spec.spec_id: idx
            for idx, spec in enumerate(blueprint.item_specs)
        }
        for spec in blueprint.item_specs:
            version_data = _build_item_version_data(
                spec,
                passage_id=passage_id,
                group_index=group_index_map[spec.spec_id],
                subject=direction.subject,
                grade_band=direction.grade_band,
                signed_by=issued_by,
            )
            result = await publish_item_version(
                item_id=None,
                version_data=version_data,
                gate_certificate_id=None,  # draft 不需要门证书
                db=db,
                auto_commit=False,
            )
            spec_id_to_version_id[spec.spec_id] = result["item_version_id"]

        # 6c. 写 item_group（testlet=true）
        item_group = assemble_item_group(
            blueprint=blueprint,
            spec_id_to_version_id=spec_id_to_version_id,
            material_version_id=None,  # passage-based testlet：语篇关联通过 lineage
        )
        db.add(item_group)
        await db.flush()

        # 6d. 统一 commit（事务原子性）
        await db.commit()

    except Exception as exc:
        # 写回失败：回滚 pipeline 的业务写入（门审计记录已由 run_gate commit，不受影响）
        await db.rollback()
        return CPipelineFailure(
            reason="writeback_error",
            step="writeback",
            evidence_chain=[{"error": str(exc)}],
        )

    # ── 7. 构建完整谱系并返回成功 ──
    ai_gen_meta = {
        "model": passage_draft.generation_meta.model,
        "prompt_hash": passage_draft.generation_meta.prompt_hash,
        "prompt_version": passage_draft.generation_meta.prompt_version,
        "token_in": passage_draft.generation_meta.token_in,
        "token_out": passage_draft.generation_meta.token_out,
        "call_id": passage_draft.generation_meta.call_id,
        "task_level": passage_draft.generation_meta.task_level,
        "fallback": passage_draft.generation_meta.fallback,
    }

    lineage = CPipelineLineage(
        passage_id=passage_id,
        source="ai_draft",
        ai_generation=ai_gen_meta,
        teacher_finalized=teacher_finalized,
        gate_certificate_id=cert_id,
        issued_by=issued_by,
        item_version_ids=[spec_id_to_version_id[s.spec_id] for s in blueprint.item_specs],
        item_group_id=item_group.item_group_id,
    )

    return CPipelineSuccess(
        passage_id=passage_id,
        item_group_id=item_group.item_group_id,
        cert_id=cert_id,
        lineage=lineage,
    )


__all__ = [
    "CPipelineLineage",
    "CPipelineSuccess",
    "CPipelineFailure",
    "run_c_pipeline",
]
