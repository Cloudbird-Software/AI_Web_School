"""W3-S3 在线作答会话服务.

落地架构 v2 §3.1/§4.5（S3）与 §4.8 合规层（时长与用眼保护）：

  开始练习（静态卷序列 / 实例池序列快照）→ 取下一题 → 提交作答
  → 即时评分（W3-S4 score_and_record）→ 即时反馈（含按错误类型展示的解析）
  → 错题回测标记。

设计决策：
- **序列快照确定性**：会话开始时把题目序列固化进 item_sequence（静态卷取
  paper_item 题序；实例池取调用方给定顺序），之后不变——重新组卷/池变化
  不影响进行中的会话。S1 组卷引擎（并行任务）落地后，其产出以
  item_version_ids 序列或 paper_id 形式接入本服务，无需改会话框架
  （架构 §4.4：会话抽象为 next_item(session_state) 策略接口）。
- **已发布区纪律**：会话只出 status='published' 的 item_version（宪法 X2：
  未过校验门的产物禁止入已发布区/服务侧）。
- **时长保护**：gradeband L≤15 分钟、M/H≤60 分钟（§4.8）；计时锚点
  last_resume_at（开始或上次休息确认时刻），超时进入 rest_prompted 并
  拒绝取题/提交，休息确认（resume_session）后重置锚点继续——阈值是
  「建议阈值」语义：提示并阻断连续作答，但不强制终结会话。
- **错题回测**：答错即标记（wrong_marks）；会话开启 retest_wrong 时，
  主序列走完后按标记顺序逐题回测一轮，回测作答同样评分落账（同 session_id）。
- **clock 注入**：所有入口接受 now 参数（默认当前 UTC），时长保护测试
  不依赖 sleep。

宪法 A5/X6：本模块不 import 任何学科包/学段包；评分调度经
src/core/scoring 注册表（学科评分器由学科包侧注册）。
"""
from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item import Item
from src.core.models.item_version import ItemVersion
from src.core.models.paper import Paper
from src.core.models.paper_item import PaperItem
from src.core.scoring.service import score_and_record
from src.core.session.models import (
    Feedback,
    NextItem,
    PracticeSession,
    SessionState,
)


class ConsentRequired(Exception):
    """家长授权缺失或已失效."""


def _build_stub_exposure_items(
    item_version_ids: list[str],
    template_version_ids: Optional[dict[str, str]] = None,
) -> list[Any]:
    """TODO: 替换为 src.core.assembly.candidates.CandidateItem 正式构造.
    当前 stub 仅提供 item_version_id / template_version_id 属性，
    配合下面 _record_paper_exposures_stub / _record_student_exposures_stub
    使用，保证代码可运行；待 exposure 模块接口稳定后切换至正式实现。
    """
    template_version_ids = template_version_ids or {}

    class _StubCandidateItem:
        def __init__(self, ivid: str, tvid: Optional[str]) -> None:
            self.item_version_id = ivid
            self.template_version_id = tvid

    return [
        _StubCandidateItem(ivid, template_version_ids.get(ivid))
        for ivid in item_version_ids
    ]


async def _record_paper_exposures_stub(
    session: AsyncSession,
    *,
    channel: str,
    subject_pack_id: str,
    gradeband: str,
    week_label: str,
    item_version_ids: list[str],
    textbook_version: Optional[str] = None,
    paper_id: Optional[str] = None,
) -> int:
    """TODO: 切换至 src.core.assembly.exposure.record_paper_exposures.
    当前为本地 stub，直接 INSERT 行至 paper_exposure（与正式接口同 schema），
    避免依赖 assembly 层导致循环 import。
    """
    try:
        from src.core.models.exposure import PaperExposure
    except Exception:
        return 0
    import ulid as _ulid
    rows = [
        PaperExposure(
            exposure_id=str(_ulid.new()),
            channel=channel,
            subject_pack_id=subject_pack_id,
            textbook_version=textbook_version,
            gradeband=gradeband,
            week_label=week_label,
            item_version_id=ivid,
            template_version_id=None,
            paper_id=paper_id,
        )
        for ivid in item_version_ids
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


async def _record_student_exposures_stub(
    session: AsyncSession,
    *,
    student_alias_id: str,
    purpose: str,
    item_version_ids: list[str],
    paper_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int:
    """TODO: 切换至 src.core.assembly.exposure.record_student_exposures.
    当前为本地 stub，直接 INSERT 行至 student_exposure。
    """
    try:
        from src.core.models.exposure import StudentExposure
    except Exception:
        return 0
    import ulid as _ulid
    rows = [
        StudentExposure(
            exposure_id=str(_ulid.new()),
            student_alias_id=student_alias_id,
            item_version_id=ivid,
            template_version_id=None,
            paper_id=paper_id,
            session_id=session_id,
            purpose=purpose,
        )
        for ivid in item_version_ids
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 时长保护阈值（架构 v2 §4.8：低段≤15 分钟、3–6 年级≤60 分钟建议阈值）
GRADEBAND_TIME_LIMIT_SEC: dict[str, int] = {
    "L": 15 * 60,
    "M": 60 * 60,
    "H": 60 * 60,
}

# 会话场景（measurement 首年不做——W3 非目标）
VALID_SCENES = frozenset({"practice", "diagnosis"})

# 解析块的 kind/type 取值（content.blocks 中视为「解析」的块）
_EXPLANATION_KINDS = frozenset(
    {"explanation", "solution", "analysis", "explanation_text"}
)

# 会做选项合成（乱序出示）的交互类型
_CHOICE_INTERACTIONS = frozenset({"single_choice", "multi_choice"})


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class SessionNotFoundError(ValueError):
    """会话不存在."""


class SessionStateError(ValueError):
    """会话状态不允许当前操作（已完成/已放弃/序列外作答）."""


class SessionCompletedError(SessionStateError):
    """会话已完成，不能再取题/作答."""


class OutOfSequenceError(SessionStateError):
    """作答题目不是当前应答题目（会话按序列逐题推进）."""


class RestRequiredError(SessionStateError):
    """时长保护触发：已连续作答超过学段阈值，须休息确认后继续.

    message 即休息提示文案（§4.8 用眼保护），API 层原样透出。
    """

    def __init__(self, message: str, *, elapsed_sec: int, time_limit_sec: int):
        super().__init__(message)
        self.message = message
        self.elapsed_sec = elapsed_sec
        self.time_limit_sec = time_limit_sec


class UnpublishedItemError(ValueError):
    """会话题目必须是已发布（published）的 item_version（门纪律）."""


# ────────────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────────────

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict 或对象取属性（兼容 ORM/Pydantic/dict 三态）."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _load_session(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    for_update: bool = False,
) -> PracticeSession:
    """按 id 取会话（str 自动转 UUID）；不存在抛 SessionNotFoundError.

    for_update=True 时使用 SELECT ... FOR UPDATE 获取行级锁，
    防止并发 submit_answer 同时读到相同的 current_index（P1-5 lost update）.
    """
    sid = UUID(str(session_id)) if not isinstance(session_id, UUID) else session_id
    if not for_update:
        session = await db.get(PracticeSession, sid)
    else:
        # AsyncSession 没有直接的 get(with_for_update)，用 execute + select
        from sqlalchemy import select
        stmt = (
            select(PracticeSession)
            .where(PracticeSession.session_id == sid)
            .with_for_update()
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFoundError(f"会话 {sid} 不存在")
    return session


def _elapsed_active_sec(session: PracticeSession, now: datetime) -> int:
    """距上次休息确认以来的连续作答秒数（时长保护计时）."""
    return max(0, int((now - session.last_resume_at).total_seconds()))


def _check_time_protection(session: PracticeSession, now: datetime) -> None:
    """时长保护检查：超时置 rest_prompted 并抛 RestRequiredError（休息提示）.

    为什么取题与提交都检查：学生可能长时间停留在某题后再提交——保护的是
    「连续作答时长」本身，不是某个端点。
    """
    if session.status in ("completed", "abandoned"):
        return
    elapsed = _elapsed_active_sec(session, now)
    if elapsed > session.time_limit_sec:
        session.status = "rest_prompted"
        minutes = session.time_limit_sec // 60
        raise RestRequiredError(
            f"已连续作答超过 {minutes} 分钟，该休息了——"
            "站起来活动一下、看看远处，休息好后回来继续。",
            elapsed_sec=elapsed,
            time_limit_sec=session.time_limit_sec,
        )


# ────────────────────────────────────────────────────────────────────
# 开始练习
# ────────────────────────────────────────────────────────────────────

async def start_session(
    db: AsyncSession,
    *,
    student_alias_id: UUID,
    gradeband: Optional[str] = None,
    scene: str = "practice",
    paper_id: Optional[str] = None,
    item_version_ids: Optional[list[str]] = None,
    retest_wrong: bool = False,
    now: Optional[datetime] = None,
) -> PracticeSession:
    """开始练习：快照题目序列，创建会话.

    题目来源（二选一）：
    - paper_id：静态卷——按 paper_item.item_number 取卷内题序（W2 追溯表，
      即「按约束快照确定性组卷」的 W2 已有产物形态）；
    - item_version_ids：实例池序列——调用方给定顺序直接快照。

    Args:
        db: 异步会话。
        student_alias_id: 匿名学生 id（D7）。
        gradeband: 学段（L/M/H）；paper_id 会话缺省取 paper.gradeband。
        scene: practice / diagnosis（measurement 首年不做）。
        paper_id / item_version_ids: 题目来源，二选一。
        retest_wrong: 主序列走完后是否对错题回测一轮。
        now: 开始时刻（默认当前 UTC；测试注入）。

    Returns:
        新建的 PracticeSession（status='active'）。

    Raises:
        ValueError: 参数互斥/缺失、scene 非法、paper 不存在、题目序列为空。
        UnpublishedItemError: 题目不存在或未发布（门纪律）。
    """
    # ── P0-4: 家长授权前置校验（宪法 D7） ──────────────────────────────
    try:
        from src.core.compliance.parental_consent import check_consent
        from fastapi import HTTPException as _HTTPException
        consent_ok = await check_consent(
            db,
            student_alias_id=student_alias_id,
            purpose=scene,
            now=now,
        )
        if not consent_ok.is_valid:
            raise _HTTPException(
                status_code=403,
                detail="parental consent not granted",
            )
    except ConsentRequired:
        raise
    except ImportError:
        raise ConsentRequired(
            "家长授权校验模块未加载，需先完成 src.core.compliance 初始化"
        )

    if (paper_id is None) == (item_version_ids is None):
        raise ValueError("paper_id 与 item_version_ids 必须且只能提供一个")
    if scene not in VALID_SCENES:
        raise ValueError(f"scene 必须 ∈ {sorted(VALID_SCENES)}，实际 {scene!r}")
    ts = now or _utcnow()

    placement_tokens: list[Optional[str]]
    if paper_id is not None:
        paper = await db.get(Paper, paper_id)
        if paper is None:
            raise ValueError(f"paper_id={paper_id!r} 不存在")
        if gradeband is None:
            gradeband = paper.gradeband
        rows = (
            await db.execute(
                select(PaperItem)
                .where(PaperItem.paper_id == paper_id)
                .order_by(PaperItem.item_number)
            )
        ).scalars().all()
        item_version_ids = [r.item_version_id for r in rows]
        placement_tokens = [r.placement_token for r in rows]
    else:
        assert item_version_ids is not None  # for type-checkers
        placement_tokens = [None] * len(item_version_ids)

    if not item_version_ids:
        raise ValueError("题目序列为空（卷内无题或实例池为空）")
    if gradeband not in GRADEBAND_TIME_LIMIT_SEC:
        raise ValueError(f"gradeband 必须 ∈ {sorted(GRADEBAND_TIME_LIMIT_SEC)}")

    # 已发布区纪律：会话只出 published 题目（宪法 X2）
    versions = (
        await db.execute(
            select(ItemVersion).where(
                ItemVersion.item_version_id.in_(item_version_ids)
            )
        )
    ).scalars().all()
    by_id = {v.item_version_id: v for v in versions}
    for vid in item_version_ids:
        v = by_id.get(vid)
        if v is None:
            raise UnpublishedItemError(f"item_version_id={vid!r} 不存在")
        if v.status != "published":
            raise UnpublishedItemError(
                f"item_version_id={vid!r} 状态 {v.status!r} 非 published"
                "（未过校验门的产物禁止入服务侧）"
            )

    sequence = [
        {
            "item_version_id": vid,
            "placement_token": placement_tokens[i],
            "item_number": i + 1,
        }
        for i, vid in enumerate(item_version_ids)
    ]

    session = PracticeSession(
        session_id=uuid.uuid4(),
        student_alias_id=student_alias_id,
        scene=scene,
        gradeband=gradeband,
        status="active",
        paper_id=paper_id,
        item_sequence=sequence,
        current_index=0,
        retest_wrong=retest_wrong,
        wrong_marks=[],
        # 阈值建会话时定型落列：策略调整不回溯影响进行中的会话
        time_limit_sec=GRADEBAND_TIME_LIMIT_SEC[gradeband],
        answered_count=0,
        correct_count=0,
        started_at=ts,
        last_resume_at=ts,
        last_activity_at=ts,
    )
    db.add(session)
    await db.flush()

    # ── P1-6: 曝光账本写入（静态卷 paper_exposure + 学生轨 student_exposure） ──
    if paper_id is not None and paper is not None:
        week_label = getattr(paper, "weekly_batch_id", None) or f"adhoc-{ts.strftime('%Y%m%d')}"
        subject_pack_id = getattr(paper, "subject_pack_id", "subject-math")
        await _record_paper_exposures_stub(
            db,
            channel="online_practice",
            subject_pack_id=subject_pack_id,
            gradeband=gradeband,
            week_label=week_label,
            item_version_ids=list(item_version_ids),
            paper_id=paper_id,
        )
    await _record_student_exposures_stub(
        db,
        student_alias_id=str(student_alias_id),
        purpose=scene,
        item_version_ids=list(item_version_ids),
        paper_id=paper_id,
        session_id=str(session.session_id),
    )

    await db.commit()
    return session


# ────────────────────────────────────────────────────────────────────
# 会话状态
# ────────────────────────────────────────────────────────────────────

def _build_state(session: PracticeSession, now: datetime) -> SessionState:
    elapsed = _elapsed_active_sec(session, now)
    marks = session.wrong_marks or []
    return SessionState(
        session_id=session.session_id,
        status=session.status,
        scene=session.scene,
        gradeband=session.gradeband,
        paper_id=session.paper_id,
        total=len(session.item_sequence),
        main_answered=session.current_index,
        answered_count=session.answered_count,
        correct_count=session.correct_count,
        wrong_count=len(marks),
        retest_pending=sum(1 for m in marks if m.get("retest_status") == "pending"),
        elapsed_active_sec=elapsed,
        time_limit_sec=session.time_limit_sec,
        remaining_sec=session.time_limit_sec - elapsed,
        started_at=session.started_at,
        completed_at=session.completed_at,
    )


async def get_session_state(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    now: Optional[datetime] = None,
) -> SessionState:
    """取会话状态（进度/已用时长/时长保护余量）."""
    session = await _load_session(db, session_id)
    return _build_state(session, now or _utcnow())


async def resume_session(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    now: Optional[datetime] = None,
) -> SessionState:
    """休息确认：rest_prompted → active，重置时长保护计时锚点.

    active 状态下调用是合法的休息确认（等效重置计时）；
    completed/abandoned 抛 SessionStateError。
    """
    session = await _load_session(db, session_id, for_update=True)
    if session.status in ("completed", "abandoned"):
        raise SessionStateError(f"会话已 {session.status}，不能休息确认")
    ts = now or _utcnow()
    session.status = "active"
    session.last_resume_at = ts
    session.last_activity_at = ts
    await db.commit()
    return _build_state(session, ts)


async def abandon_session(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    now: Optional[datetime] = None,
) -> SessionState:
    """放弃会话（学生中途退出）；已作答事件保留在 response_event 账."""
    session = await _load_session(db, session_id, for_update=True)
    if session.status == "completed":
        raise SessionStateError("会话已完成，不能放弃")
    ts = now or _utcnow()
    session.status = "abandoned"
    session.last_activity_at = ts
    await db.commit()
    return _build_state(session, ts)


# ────────────────────────────────────────────────────────────────────
# 取下一题
# ────────────────────────────────────────────────────────────────────

def _current_retest_mark(session: PracticeSession) -> Optional[dict[str, Any]]:
    """当前应回测的错题标记：已出示未作答的优先，否则取首个待回测."""
    marks = session.wrong_marks or []
    for m in marks:
        if m.get("retest_status") == "served":
            return m
    for m in marks:
        if m.get("retest_status") == "pending":
            return m
    return None


def _extract_options(
    session_id: UUID, item_version: ItemVersion
) -> Optional[list[dict[str, str]]]:
    """选择题选项合成：正解（scorer_params.answer）+ 干扰项（error_bindings）.

    - 若 content.blocks 已含带 options 的块（渲染层装配好的），返回 None
      （选项以题面块为准，客户端直接渲染）。
    - 否则合成 [{id, label}]：id=选项值（作答 selected 与 error_bindings
      .option_value 同口径），label=显示文本（选项值本身——干扰项设计 label
      是错误类型语义，不下发客户端）。
    - 确定性乱序：种子 = sha256(session_id|item_version_id)，同一会话同一题
      选项顺序稳定（刷新不乱），不同学生顺序不同（防背选项位）。
    """
    interaction_id = _get(
        _get(item_version, "interaction_ref") or {}, "interaction_id"
    )
    if interaction_id not in _CHOICE_INTERACTIONS:
        return None
    blocks = _get(_get(item_version, "content") or {}, "blocks") or []
    for b in blocks:
        if _get(b, "options"):
            return None

    values: list[str] = []
    params = _get(_get(item_version, "scoring_ref") or {}, "scorer_params") or {}
    answer = params.get("answer")
    answer_values = answer if isinstance(answer, (list, tuple)) else [answer]
    for v in answer_values:
        if v is not None and str(v) not in values:
            values.append(str(v))
    for binding in _get(item_version, "error_bindings") or []:
        ov = _get(binding, "option_value")
        if ov is not None and str(ov) not in values:
            values.append(str(ov))
    if not values:
        return None

    seed = hashlib.sha256(
        f"{session_id}|{item_version.item_version_id}".encode("utf-8")
    ).hexdigest()
    random.Random(seed).shuffle(values)
    return [{"id": v, "label": v} for v in values]


async def get_next_item(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    now: Optional[datetime] = None,
) -> Optional[NextItem]:
    """取下一题：主序列逐题推进；主序列走完后按错题标记回测（retest_wrong）.

    Returns:
        NextItem；会话完成（主序列+回测均走完）返回 None 并把会话置 completed。

    Raises:
        SessionNotFoundError / SessionStateError / RestRequiredError。
    """
    # 题目推进逻辑要改 session.current_index、status=completed 等，拿行锁防并发
    session = await _load_session(db, session_id, for_update=True)
    ts = now or _utcnow()
    if session.status == "completed":
        return None
    if session.status == "abandoned":
        raise SessionStateError("会话已放弃，不能取题")
    _check_time_protection(session, ts)

    sequence = session.item_sequence or []
    round_ = "main"
    entry: Optional[dict[str, Any]] = None
    if session.current_index < len(sequence):
        entry = sequence[session.current_index]
    elif session.retest_wrong:
        mark = _current_retest_mark(session)
        if mark is not None:
            round_ = "retest"
            entry = {
                "item_version_id": mark["item_version_id"],
                "item_number": mark.get("item_number"),
            }
            if mark.get("retest_status") == "pending":
                # 出示即标记 served（刷新重取同一题，幂等）
                marks = [dict(m) for m in session.wrong_marks]
                for m in marks:
                    if m["item_version_id"] == mark["item_version_id"] and m.get(
                        "retest_status"
                    ) == "pending":
                        m["retest_status"] = "served"
                        break
                session.wrong_marks = marks
                session.last_activity_at = ts
                await db.commit()

    if entry is None:
        # 主序列与回测均走完 → 完成
        session.status = "completed"
        session.completed_at = ts
        session.last_activity_at = ts
        await db.commit()
        return None

    item_version = await db.get(ItemVersion, entry["item_version_id"])
    if item_version is None:  # pragma: no cover - 开始时已校验存在
        raise ValueError(f"item_version {entry['item_version_id']!r} 不存在")
    session.last_activity_at = ts
    await db.commit()

    return NextItem(
        session_id=session.session_id,
        round=round_,
        position=int(entry.get("item_number") or session.current_index + 1),
        total=len(sequence),
        item_version_id=item_version.item_version_id,
        interaction_id=str(
            _get(_get(item_version, "interaction_ref") or {}, "interaction_id")
        ),
        content_blocks=list(
            _get(_get(item_version, "content") or {}, "blocks") or []
        ),
        options=_extract_options(session.session_id, item_version),
    )


# ────────────────────────────────────────────────────────────────────
# 提交作答（评分 + 反馈 + 错题标记）
# ────────────────────────────────────────────────────────────────────

def _extract_explanation(item_version: ItemVersion) -> Optional[list[str]]:
    """从 content.blocks 提取解析文本（explanation/solution/analysis 块）."""
    blocks = _get(_get(item_version, "content") or {}, "blocks") or []
    parts = []
    for b in blocks:
        kind = _get(b, "kind") or _get(b, "type")
        if kind in _EXPLANATION_KINDS:
            text = _get(b, "rendered") or _get(b, "value") or _get(b, "template")
            if text:
                parts.append(str(text))
    return parts or None


def _build_error_feedback(
    item_version: ItemVersion,
    error_inferences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按错误类型装配展示反馈：error_type_id + 干扰项设计 label + 置信度.

    label 来自 error_bindings（distractor_rules 的设计语义，如「多 1」），
    帮助学生/家长理解「错在哪种理解」（业务全景 §三）。
    """
    label_by_error: dict[str, Any] = {}
    for binding in _get(item_version, "error_bindings") or []:
        et = _get(binding, "error_type_id")
        if et and et not in label_by_error:
            label_by_error[str(et)] = _get(binding, "label")
    feedback = []
    for inf in error_inferences:
        et = str(inf.get("error_type_id"))
        feedback.append({
            "error_type_id": et,
            "confidence": inf.get("confidence"),
            "label": label_by_error.get(et),
            "evidence": inf.get("evidence"),
        })
    return feedback


async def submit_answer(
    db: AsyncSession,
    session_id: UUID | str,
    *,
    item_version_id: str,
    response: dict[str, Any],
    duration_ms: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Feedback:
    """提交作答：即时评分 → 落 response_event → 反馈 → 错题回测标记.

    序列纪律：只能作答「当前应答题」（主序列 current_index 或当前回测题），
    否则 OutOfSequenceError——在线会话逐题推进，不允许跳答/补答。

    Args:
        db: 异步会话。
        session_id: 会话 id。
        item_version_id: 作答题目版本（必须是当前应答题）。
        response: 原始作答载荷（结构由交互类型 response_schema 保证）。
        duration_ms: 作答耗时（毫秒）；None=未知（禁止填 0 冒充，契约 §1）。
        now: 事件时间戳（默认当前 UTC；测试注入）。

    Returns:
        Feedback（对错/维度分/错误推断/按错误类型的解析/进度）。

    Raises:
        SessionNotFoundError / SessionCompletedError / OutOfSequenceError /
        RestRequiredError / ScorerNotRegisteredError。
    """
    # 提交逻辑会改 current_index / wrong_mark / status，拿行锁防并发丢失更新
    session = await _load_session(db, session_id, for_update=True)
    ts = now or _utcnow()

    # ── P0-4: 每次作答重新校验家长授权仍有效 ────────────────────────────
    try:
        from src.core.compliance.parental_consent import check_consent
        from fastapi import HTTPException as _HTTPException
        consent_ok = await check_consent(
            db,
            student_alias_id=session.student_alias_id,
            purpose=session.scene,  # type: ignore[arg-type]
            now=now,
        )
        if not consent_ok.is_valid:
            raise _HTTPException(
                status_code=403,
                detail="parental consent not granted",
            )
    except ConsentRequired:
        raise
    except ImportError:
        raise ConsentRequired(
            "家长授权校验模块未加载，需先完成 src.core.compliance 初始化"
        )

    if session.status == "completed":
        raise SessionCompletedError("会话已完成，不能再作答")
    if session.status == "abandoned":
        raise SessionStateError("会话已放弃，不能再作答")
    _check_time_protection(session, ts)

    # 当前应答题：主序列 current_index；否则当前回测标记
    sequence = session.item_sequence or []
    round_ = "main"
    expected_id: Optional[str] = None
    mark: Optional[dict[str, Any]] = None
    if session.current_index < len(sequence):
        expected_id = sequence[session.current_index]["item_version_id"]
    elif session.retest_wrong:
        mark = _current_retest_mark(session)
        if mark is not None:
            round_ = "retest"
            expected_id = mark["item_version_id"]

    if expected_id is None:
        raise SessionCompletedError("会话题目已走完，不能再作答")
    if item_version_id != expected_id:
        raise OutOfSequenceError(
            f"当前应答题为 {expected_id!r}，收到 {item_version_id!r}"
            "（会话按序列逐题推进，不允许跳答/补答）"
        )

    item_version = await db.get(ItemVersion, item_version_id)
    if item_version is None:  # pragma: no cover - 开始时已校验存在
        raise ValueError(f"item_version {item_version_id!r} 不存在")
    item = await db.get(Item, item_version.item_id)
    pack_id = item.pack_id if item is not None else None

    # 来源追溯（契约 §1 source_ref）：静态卷 {paper_id, placement_token}；
    # 实例池会话无组卷运行可引，留 NULL（session_id 已承载会话关联）。
    source_ref: Optional[dict[str, Any]] = None
    if session.paper_id is not None and round_ == "main":
        source_ref = {
            "paper_id": session.paper_id,
            "placement_token": sequence[session.current_index].get("placement_token"),
        }

    outcome = await score_and_record(
        db,
        item_version=item_version,
        response=response,
        student_alias_id=session.student_alias_id,
        scene=session.scene,  # type: ignore[arg-type]  # DB CHECK 已约束二值
        pack_id=pack_id,
        duration_ms=duration_ms,
        session_id=session.session_id,
        source_ref=source_ref,
        now=ts,
    )

    # 会话状态推进
    session.answered_count += 1
    if outcome.correct:
        session.correct_count += 1
    session.last_activity_at = ts

    if round_ == "main":
        session.current_index += 1
        if not outcome.correct:
            marks = [dict(m) for m in (session.wrong_marks or [])]
            marks.append({
                "item_version_id": item_version_id,
                "item_number": sequence[session.current_index - 1].get("item_number"),
                "error_type_ids": [
                    str(i["error_type_id"]) for i in outcome.error_inferences
                ],
                "first_seen_at": ts.isoformat(),
                # 未开启回测的会话仅标记（off），开启的进入待回测队列
                "retest_status": "pending" if session.retest_wrong else "off",
            })
            session.wrong_marks = marks
    else:
        assert mark is not None
        marks = [dict(m) for m in (session.wrong_marks or [])]
        for m in marks:
            # served（取题后作答）与 pending（未取题直接提交）都接受
            if m["item_version_id"] == item_version_id and m.get(
                "retest_status"
            ) in ("served", "pending"):
                m["retest_status"] = "passed" if outcome.correct else "failed"
                break
        session.wrong_marks = marks

    # 完成判定：主序列走完且（未开回测 或 无待回测/已出示标记）
    if session.current_index >= len(sequence):
        if not session.retest_wrong or _current_retest_mark(session) is None:
            session.status = "completed"
            session.completed_at = ts

    # ── P1-6: 题目切换时记录 student_exposure（已答当前题，推进到下一题） ──
    next_ivids: list[str] = []
    if session.status != "completed":
        if session.current_index < len(sequence):
            next_ivids.append(sequence[session.current_index]["item_version_id"])
        elif session.retest_wrong:
            next_mark = _current_retest_mark(session)
            if next_mark is not None:
                next_ivids.append(next_mark["item_version_id"])
    if next_ivids:
        await _record_student_exposures_stub(
            db,
            student_alias_id=str(session.student_alias_id),
            purpose=str(session.scene),
            item_version_ids=next_ivids,
            paper_id=session.paper_id,
            session_id=str(session.session_id),
        )

    await db.commit()

    return Feedback(
        event_id=outcome.event_id,
        correct=outcome.correct,
        dimension_scores=outcome.dimension_scores,
        error_inferences=outcome.error_inferences,
        error_feedback=_build_error_feedback(item_version, outcome.error_inferences),
        explanation=_extract_explanation(item_version),
        progress={
            "total": len(sequence),
            "main_answered": session.current_index,
            "answered_count": session.answered_count,
            "correct_count": session.correct_count,
        },
        session_status=session.status,
    )


__all__ = [
    "GRADEBAND_TIME_LIMIT_SEC",
    "VALID_SCENES",
    "OutOfSequenceError",
    "RestRequiredError",
    "SessionCompletedError",
    "SessionNotFoundError",
    "SessionStateError",
    "UnpublishedItemError",
    "abandon_session",
    "get_next_item",
    "get_session_state",
    "resume_session",
    "start_session",
    "submit_answer",
]
