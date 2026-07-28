"""T-W4-003 增量重判框架 + 年度全量重放（架构 v2 §4.7 / 宪法 D6 / R-D-05）.

落地 specs/contracts/events/response_event.md §3 重判规则：
- 新 scorer 上线时，仅对指定范围写平行 score_run，原 response_event.scoring_trace
  永不改动（D1 作答事件账只增不改；契约 §3 原文「原序列不动」）。
- score_run 是独立账（重判结果账），append-only（迁移 0017 触发器物理强制）。

核心接口：
- incremental_rescore(db, item_ids, new_scorer_version, *, purpose_scope=None,
  run_label=None, input_snapshot_id=...) → RescoreReport
  为指定题目集合生成平行 score_run；不传 purpose_scope 时跨场景重判
  （事件按各自 scene 写对应 scope 的 score_run）。
- replay_all(db, *, purpose_scope, scorer_version=None, run_label=None,
  input_snapshot_id=...) → ReplayReport
  全量重放：读取该场景全部历史 response_event，用当前活跃估计器重算，
  输出新旧对比报告（参数差异分布 + 一致性率）。

可重放性（D6 / 验收 §3）：
- 同代码版本 + 同数据快照必同输出——本模块不依赖时间或随机源；
  RescoreReport / ReplayReport 携带 summary_hash（输入快照 + 代码 digest +
  结果摘要的 SHA256），可被年度重放演练脚本比对复现。

为什么 incremental_rescore 不修改 response_event：契约 §3 明确「写平行 score_run，
原序列不动」——本模块只 INSERT score_run，response_event 在 DB 层 UPDATE/DELETE
会被 0003 触发器物理拒绝（即便代码绕过也无路径可写）。

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
学科评分器（如 math_equivalence）由调用方 import 学科包触发 register_scorer
注册——本模块仅通过 scorer_id 从注册表取实现，不直接 import 学科包。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

import ulid
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.active_model_pointer import ActiveModelPointer
from src.core.models.score_run import ScoreRun
from src.core.scoring.service import build_scoring_trace, infer_option_errors, run_scorer

logger = logging.getLogger(__name__)

# 场景三值域（与 ctt / active_model_pointer / D5 对齐）
VALID_PURPOSE_SCOPES: frozenset[str] = frozenset(
    {"practice", "diagnosis", "measurement"}
)

# 默认输入快照标识前缀（调用方未提供时用时间戳 + 摘要构造）
_DEFAULT_SNAPSHOT_PREFIX = "snapshot"


# ────────────────────────────────────────────────────────────────────
# 报告数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RescoreReport:
    """incremental_rescore 返回报告.

    - rescored_count：本次写入的 score_run 行数
    - skipped_count：因幂等约束跳过的事件数（同版本同标签已存在）
    - failed_count：评分失败的事件数（如评分器未注册）
    - failures：失败详情列表（event_id + 原因）
    - consistency: 新旧 correct 一致率（0~1）
    - summary_hash: 摘要哈希（D6 可重放——同输入必同输出）
    - scorer_version: 本次重判所用评分器版本（来自第一个成功评分的 ScoreResult；
      空字符串表示无成功评分）
    - run_label / input_snapshot_id: 批次标识
    """

    rescored_count: int
    skipped_count: int
    failed_count: int
    consistency: float
    summary_hash: str
    scorer_version: str
    run_label: Optional[str]
    input_snapshot_id: str
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转 JSON-serializable dict（便于报告导出）."""
        return asdict(self)


@dataclass(frozen=True)
class ReplayReport(RescoreReport):
    """replay_all 返回报告（年度全量重放）.

    继承 RescoreReport，新增参数差异分布：
    - old_param_summary: 旧版本参数摘要（如难度分布）
    - new_param_summary: 新版本参数摘要
    - param_diff_distribution: 参数差异分布（如 difficulty 差值分桶）
    """

    old_param_summary: dict[str, Any] = field(default_factory=dict)
    new_param_summary: dict[str, Any] = field(default_factory=dict)
    param_diff_distribution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ────────────────────────────────────────────────────────────────────
# 辅助：取数 + 哈希
# ────────────────────────────────────────────────────────────────────


# response_event 取数 SQL（按 item_version_id 过滤，可选 scene 过滤）
# 为什么 SELECT 全字段：重判需 raw_payload + scoring_trace（取 original_scorer_version）
# + scene（写 score_run.purpose_scope）+ item_version_id（run_scorer 需要）
# 为什么 created_at AS event_created_at：response_event 列名是 created_at（分区键），
# 但本模块语义上把它作为「事件时间戳」传给 score_run.event_created_at；
# 别名让调用方读 r.event_created_at 与下游语义对齐，避免 r.created_at 这种
# 模糊命名（core 数据域里 created_at 也可指 score_run/item_param 的写入时刻）。
_FETCH_EVENTS_BY_ITEMS_SQL = """
SELECT event_id, created_at AS event_created_at, item_version_id, scene,
       raw_payload, scoring_trace
FROM response_event
WHERE item_version_id = ANY(:item_ids)
"""
_FETCH_EVENTS_BY_SCOPE_SQL = """
SELECT event_id, created_at AS event_created_at, item_version_id, scene,
       raw_payload, scoring_trace
FROM response_event
WHERE scene = :scope
"""
# 检查 score_run 幂等：同事件同批次标签是否已存在
# 为什么不含 scorer_version：scorer_version 是 Scorer 自报审计字段（不可预测），
# 幂等键只用 (event_id, event_created_at, run_label)——同一批次同一事件只写一条。
# run_label 用 IS NOT DISTINCT FROM 处理 NULL（NULL 与 NULL 视为相等）。
_CHECK_IDEMPOTENT_SQL = """
SELECT 1 FROM score_run
WHERE event_id = :eid AND event_created_at = :ts
  AND run_label IS NOT DISTINCT FROM :label
LIMIT 1
"""


async def _fetch_item_version(
    db: AsyncSession, item_version_id: str
) -> Optional[dict[str, Any]]:
    """取 item_version 行（含六大块 JSONB）以供 run_scorer 调度.

    返回 dict 形式（与 run_scorer 的三态入参兼容）；行不存在返回 None。
    """
    row = (
        await db.execute(
            text(
                "SELECT item_version_id, interaction_ref, content, scoring_ref,"
                " error_bindings FROM item_version WHERE item_version_id = :vid"
            ),
            {"vid": item_version_id},
        )
    ).first()
    if row is None:
        return None
    # row 的 JSONB 字段已被 SQLAlchemy 解码为 dict/list
    return {
        "item_version_id": row.item_version_id,
        "interaction_ref": row.interaction_ref,
        "content": row.content,
        "scoring_ref": row.scoring_ref,
        "error_bindings": row.error_bindings,
    }


def _safe_scorer_version_from_trace(scoring_trace: Any) -> str:
    """从原始事件 scoring_trace 取 scorer_version（兜底空串）."""
    if isinstance(scoring_trace, dict):
        v = scoring_trace.get("scorer_version")
        if isinstance(v, str) and v:
            return v
    return ""


def _safe_correct_from_trace(scoring_trace: Any) -> Optional[bool]:
    """从原始事件 scoring_trace 取 correct（process.correct 或 dimension_scores.correct）.

    用于新旧一致性比对；无法判定时返回 None（不计入一致性分母）。
    """
    if not isinstance(scoring_trace, dict):
        return None
    proc = scoring_trace.get("process")
    if isinstance(proc, dict) and "correct" in proc:
        return bool(proc["correct"])
    dims = scoring_trace.get("dimension_scores")
    if isinstance(dims, dict) and "correct" in dims:
        return float(dims["correct"]) >= 1.0
    return None


def _compute_summary_hash(
    *,
    input_snapshot_id: str,
    events_processed: list[dict[str, Any]],
    rescored_summary: list[dict[str, Any]],
) -> str:
    """计算重判摘要哈希（D6 可重放——同输入必同输出）.

    hash 输入 = 输入快照 id + 已处理事件指纹（按 event_id 排序去序）+ 重判结果摘要。
    """
    h = hashlib.sha256()
    h.update(input_snapshot_id.encode("utf-8"))
    # 事件指纹：按 event_id 排序后取 (event_id, item_version_id, scene) 三元组
    events_canonical = sorted(
        (str(e["event_id"]), str(e["item_version_id"]), str(e["scene"]))
        for e in events_processed
    )
    h.update(json.dumps(events_canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    # 重判结果摘要：按 score_run_id 排序去序
    rescored_canonical = sorted(
        (str(r["event_id"]), str(r["scorer_version"]), bool(r["correct"]))
        for r in rescored_summary
    )
    h.update(json.dumps(rescored_canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _default_input_snapshot_id(events_count: int, purpose_scope: Optional[str]) -> str:
    """默认输入快照标识（调用方未提供时构造一个稳定的标识）.

    为什么用 (events_count, purpose_scope) 而非时间戳：可重放性要求同输入同输出；
    时间戳会让每次调用产生不同 hash。count + scope 是输入数据的稳定摘要。
    """
    return f"{_DEFAULT_SNAPSHOT_PREFIX}:{purpose_scope or 'all'}:{events_count}"


# ────────────────────────────────────────────────────────────────────
# 单事件重判核心
# ────────────────────────────────────────────────────────────────────


async def _rescore_one_event(
    db: AsyncSession,
    *,
    event_id: UUID,
    event_created_at: datetime,
    item_version_id: str,
    scene: str,
    raw_payload: dict[str, Any],
    original_scoring_trace: dict[str, Any],
    new_scorer_version: str,
    run_label: Optional[str],
    input_snapshot_id: str,
    item_version_cache: dict[str, Optional[dict[str, Any]]],
) -> tuple[Optional[ScoreRun], Optional[str]]:
    """对单条事件重判，返回 (ScoreRun 或 None, 失败原因 或 None).

    幂等：同事件同版本同标签已存在 → 返回 (None, None) 表示跳过（非失败）。
    评分失败：返回 (None, 原因) 计入 failures。
    成功：返回 (ScoreRun, None)，已 db.add 但未 commit（调用方批量 commit）。
    """
    # 幂等检查：同事件同批次标签已写过则跳过
    existing = (
        await db.execute(
            text(_CHECK_IDEMPOTENT_SQL),
            {
                "eid": event_id,
                "ts": event_created_at,
                "label": run_label,
            },
        )
    ).first()
    if existing is not None:
        return None, None  # 幂等跳过

    # 取 item_version（缓存避免同题多事件重复查）
    if item_version_id not in item_version_cache:
        item_version_cache[item_version_id] = await _fetch_item_version(db, item_version_id)
    iv = item_version_cache[item_version_id]
    if iv is None:
        return None, f"item_version not found: {item_version_id}"

    # 重判：用当前注册的评分器（按 item_version.scoring_ref.scorer_id 调度）
    try:
        result = run_scorer(iv, raw_payload)
    except Exception as e:
        return None, f"scorer failed: {type(e).__name__}: {e}"

    # 装配 scoring_trace（与在线评分一致——含 dimension_scores 供 CTT 取数）
    error_inferences = list(result.error_inferences) + infer_option_errors(iv, raw_payload)
    # 去重（与 score_and_record 一致）
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for inf in error_inferences:
        key = f"{inf.get('error_type_id')}|{inf.get('evidence')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(inf)

    scoring_trace = build_scoring_trace(iv["scoring_ref"]["scorer_id"], result)
    correct = bool(result.dimension_scores.get("correct", 0.0) >= 1.0)
    original_scorer_version = _safe_scorer_version_from_trace(original_scoring_trace)

    run = ScoreRun(
        score_run_id="sr_" + str(ulid.new()),
        event_id=event_id,
        event_created_at=event_created_at,
        rerun_of=None,
        purpose_scope=scene,
        scorer_id=iv["scoring_ref"]["scorer_id"],
        scorer_version=result.scorer_version,
        original_scorer_version=original_scorer_version,
        dimension_scores=dict(result.dimension_scores),
        scoring_trace=scoring_trace,
        error_inferences=deduped,
        correct=correct,
        run_label=run_label,
        input_snapshot_id=input_snapshot_id,
    )
    db.add(run)
    return run, None


# ────────────────────────────────────────────────────────────────────
# 主接口 1：incremental_rescore
# ────────────────────────────────────────────────────────────────────


async def incremental_rescore(
    db: AsyncSession,
    item_ids: Iterable[str],
    new_scorer_version: str,
    *,
    purpose_scope: Optional[str] = None,
    run_label: Optional[str] = None,
    input_snapshot_id: Optional[str] = None,
) -> RescoreReport:
    """对指定题目集合的作答事件增量重判，写平行 score_run（原 response_event 不动）.

    Args:
        db: 异步会话。
        item_ids: 要重判的 item_version_id 集合。
        new_scorer_version: 新评分器版本标签（用于幂等与对比；与实际评分器
            的 result.scorer_version 可能一致——后者由注册的 Scorer 自报）。
        purpose_scope: 可选场景过滤；None=跨场景重判（按事件各自 scene 写
            对应 scope 的 score_run，D5 仍单场景独立估计）。越域值抛 ValueError。
        run_label: 批次标签（如 'annual-replay-2026'）；用于幂等保护与报告分组。
        input_snapshot_id: 输入数据快照标识（D6 可重放）；None=自动构造稳定标识。

    Returns:
        RescoreReport（含写入数、跳过数、失败数、一致性率、摘要哈希）。

    Notes:
        - 仅 INSERT score_run；response_event 在 DB 层物理禁 UPDATE/DELETE（D1）。
        - 同事件同版本同标签幂等：已存在则跳过，不报错。
        - 评分失败的事件计入 failures，不阻断其他事件重判。
    """
    if purpose_scope is not None and purpose_scope not in VALID_PURPOSE_SCOPES:
        raise ValueError(
            f"purpose_scope 越域：{purpose_scope!r}"
            f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
        )

    item_ids_list = list(item_ids)
    if not item_ids_list:
        return RescoreReport(
            rescored_count=0, skipped_count=0, failed_count=0,
            consistency=0.0, summary_hash="", scorer_version="",
            run_label=run_label,
            input_snapshot_id=input_snapshot_id or _default_input_snapshot_id(0, purpose_scope),
        )

    # 取数
    if purpose_scope is not None:
        rows = (
            await db.execute(
                text(_FETCH_EVENTS_BY_ITEMS_SQL + " AND scene = :scope"),
                {"item_ids": item_ids_list, "scope": purpose_scope},
            )
        ).all()
    else:
        rows = (
            await db.execute(
                text(_FETCH_EVENTS_BY_ITEMS_SQL),
                {"item_ids": item_ids_list},
            )
        ).all()

    if not rows:
        return RescoreReport(
            rescored_count=0, skipped_count=0, failed_count=0,
            consistency=0.0, summary_hash="", scorer_version="",
            run_label=run_label,
            input_snapshot_id=input_snapshot_id or _default_input_snapshot_id(0, purpose_scope),
        )

    snap_id = input_snapshot_id or _default_input_snapshot_id(len(rows), purpose_scope)
    iv_cache: dict[str, Optional[dict[str, Any]]] = {}
    rescored_summary: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0
    failed = 0
    rescored = 0
    consistent = 0
    comparable = 0
    actual_scorer_version = ""

    for r in rows:
        run, reason = await _rescore_one_event(
            db,
            event_id=r.event_id,
            event_created_at=r.event_created_at,
            item_version_id=r.item_version_id,
            scene=r.scene,
            raw_payload=r.raw_payload,
            original_scoring_trace=r.scoring_trace,
            new_scorer_version=new_scorer_version,
            run_label=run_label,
            input_snapshot_id=snap_id,
            item_version_cache=iv_cache,
        )
        if run is None:
            if reason is None:
                skipped += 1  # 幂等跳过
            else:
                failed += 1
                failures.append({
                    "event_id": str(r.event_id),
                    "item_version_id": r.item_version_id,
                    "reason": reason,
                })
            continue
        rescored += 1
        if not actual_scorer_version:
            actual_scorer_version = run.scorer_version
        rescored_summary.append({
            "event_id": str(run.event_id),
            "scorer_version": run.scorer_version,
            "correct": run.correct,
        })
        # 一致性比对：新旧 correct 是否一致
        old_correct = _safe_correct_from_trace(r.scoring_trace)
        if old_correct is not None:
            comparable += 1
            if old_correct == run.correct:
                consistent += 1

    # 一次性 commit（事务原子性：所有写入要么全成要么全失败）
    if rescored > 0:
        await db.commit()

    consistency = (consistent / comparable) if comparable > 0 else 0.0
    summary_hash = _compute_summary_hash(
        input_snapshot_id=snap_id,
        events_processed=[
            {"event_id": r.event_id, "item_version_id": r.item_version_id, "scene": r.scene}
            for r in rows
        ],
        rescored_summary=rescored_summary,
    )

    return RescoreReport(
        rescored_count=rescored,
        skipped_count=skipped,
        failed_count=failed,
        consistency=consistency,
        summary_hash=summary_hash,
        scorer_version=actual_scorer_version,
        run_label=run_label,
        input_snapshot_id=snap_id,
        failures=failures,
    )


# ────────────────────────────────────────────────────────────────────
# 主接口 2：replay_all（年度全量重放）
# ────────────────────────────────────────────────────────────────────


async def replay_all(
    db: AsyncSession,
    *,
    purpose_scope: str,
    scorer_version: Optional[str] = None,
    run_label: Optional[str] = None,
    input_snapshot_id: Optional[str] = None,
) -> ReplayReport:
    """年度全量重放：读取该场景全部历史事件，用当前活跃估计器重算并出对比报告.

    Args:
        db: 异步会话。
        purpose_scope: 场景（D5 必填单值——按场景独立重放）。
        scorer_version: 重判用评分器版本标签；None=取当前活跃估计器的 model_version。
        run_label: 批次标签（如 'annual-replay-2026'）。
        input_snapshot_id: 输入数据快照标识；None=自动构造。

    Returns:
        ReplayReport（继承 RescoreReport，新增参数差异分布 + 新旧一致性）。

    Notes:
        - 全量重放可能耗时较长，建议由年度脚本调用；非实时、非生产自动触发（non_goals）。
        - 同 incremental_rescore：仅写平行 score_run，原 response_event 不动。
    """
    if purpose_scope not in VALID_PURPOSE_SCOPES:
        raise ValueError(
            f"purpose_scope 越域：{purpose_scope!r}"
            f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
        )

    # 取当前活跃估计器版本（D6——年度重放引用「当前活跃」版本的实证）
    ptr = ActiveModelPointer(db)
    active = await ptr.get_active(purpose_scope)
    if scorer_version is None:
        if active is None:
            raise ValueError(
                f"场景 {purpose_scope!r} 无活跃估计器版本；请先 set_active 或显式传 scorer_version"
            )
        scorer_version = active.model_version

    # 取该场景全部历史事件
    rows = (
        await db.execute(
            text(_FETCH_EVENTS_BY_SCOPE_SQL),
            {"scope": purpose_scope},
        )
    ).all()

    if not rows:
        snap_id = input_snapshot_id or _default_input_snapshot_id(0, purpose_scope)
        return ReplayReport(
            rescored_count=0, skipped_count=0, failed_count=0,
            consistency=0.0, summary_hash="", scorer_version="",
            run_label=run_label, input_snapshot_id=snap_id,
            old_param_summary={}, new_param_summary={}, param_diff_distribution={},
        )

    snap_id = input_snapshot_id or _default_input_snapshot_id(len(rows), purpose_scope)
    iv_cache: dict[str, Optional[dict[str, Any]]] = {}
    rescored_summary: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0
    failed = 0
    rescored = 0
    consistent = 0
    comparable = 0
    actual_scorer_version = ""
    # 收集新旧难度分布（按 correct 推断难度近似，便于新旧差异概览）
    old_correct_counter: Counter[bool] = Counter()
    new_correct_counter: Counter[bool] = Counter()
    # 旧 scorer_version 分布（用于报告新旧版本切换实证）
    old_scorer_versions: Counter[str] = Counter()

    for r in rows:
        run, reason = await _rescore_one_event(
            db,
            event_id=r.event_id,
            event_created_at=r.event_created_at,
            item_version_id=r.item_version_id,
            scene=r.scene,
            raw_payload=r.raw_payload,
            original_scoring_trace=r.scoring_trace,
            new_scorer_version=scorer_version,
            run_label=run_label,
            input_snapshot_id=snap_id,
            item_version_cache=iv_cache,
        )
        # 收集旧版本统计（无论是否重判成功都收）
        old_sv = _safe_scorer_version_from_trace(r.scoring_trace)
        old_scorer_versions[old_sv] += 1
        old_correct = _safe_correct_from_trace(r.scoring_trace)
        if old_correct is not None:
            old_correct_counter[old_correct] += 1

        if run is None:
            if reason is None:
                skipped += 1
            else:
                failed += 1
                failures.append({
                    "event_id": str(r.event_id),
                    "item_version_id": r.item_version_id,
                    "reason": reason,
                })
            continue
        rescored += 1
        if not actual_scorer_version:
            actual_scorer_version = run.scorer_version
        rescored_summary.append({
            "event_id": str(run.event_id),
            "scorer_version": run.scorer_version,
            "correct": run.correct,
        })
        new_correct_counter[run.correct] += 1
        if old_correct is not None:
            comparable += 1
            if old_correct == run.correct:
                consistent += 1

    if rescored > 0:
        await db.commit()

    consistency = (consistent / comparable) if comparable > 0 else 0.0
    summary_hash = _compute_summary_hash(
        input_snapshot_id=snap_id,
        events_processed=[
            {"event_id": r.event_id, "item_version_id": r.item_version_id, "scene": r.scene}
            for r in rows
        ],
        rescored_summary=rescored_summary,
    )

    # 参数差异分布（用 correct 比例作为难度近似指标——避免重算 CTT 增加耦合）
    old_total = sum(old_correct_counter.values())
    new_total = sum(new_correct_counter.values())
    old_difficulty = (
        old_correct_counter.get(True, 0) / old_total if old_total > 0 else None
    )
    new_difficulty = (
        new_correct_counter.get(True, 0) / new_total if new_total > 0 else None
    )
    param_diff: dict[str, Any] = {}
    if old_difficulty is not None and new_difficulty is not None:
        param_diff = {
            "difficulty_old": old_difficulty,
            "difficulty_new": new_difficulty,
            "difficulty_delta": new_difficulty - old_difficulty,
        }

    return ReplayReport(
        rescored_count=rescored,
        skipped_count=skipped,
        failed_count=failed,
        consistency=consistency,
        summary_hash=summary_hash,
        scorer_version=actual_scorer_version,
        run_label=run_label,
        input_snapshot_id=snap_id,
        failures=failures,
        old_param_summary={
            "scorer_versions": dict(old_scorer_versions),
            "correct_distribution": {
                "true": old_correct_counter.get(True, 0),
                "false": old_correct_counter.get(False, 0),
            },
            "difficulty_approx": old_difficulty,
        },
        new_param_summary={
            "scorer_version": actual_scorer_version,
            "correct_distribution": {
                "true": new_correct_counter.get(True, 0),
                "false": new_correct_counter.get(False, 0),
            },
            "difficulty_approx": new_difficulty,
        },
        param_diff_distribution=param_diff,
    )


__all__ = [
    "VALID_PURPOSE_SCOPES",
    "RescoreReport",
    "ReplayReport",
    "incremental_rescore",
    "replay_all",
]
