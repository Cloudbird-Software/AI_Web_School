"""T-W4-030 CTT 信度/区分度报告单元测试.

覆盖任务卡验收标准（逐条可执行）：
  §1 generate_ctt_report(response_events, paper_id) 返回含 α/区分度/难度/标准误 报告。
  §2 小样本警示：n<30 时报告头部标记「样本不足，结果仅供参考」。
  §3 区分度计算与既有 CTT 实现一致（复用 src/core/data/ctt.py 的 compute_ctt）。
  §4 make accept TASK=T-W4-030 全绿（本文件即单元测试主体）。
  §5 不 import 任何学科包/学段包（A5/X6 静态扫描）。

测试分层：
- 纯函数层（generate_ctt_report）：数值正确性（手算 α / SEM）+ 边界情形 +
  小样本警示 + 区分度一致性
- DB 集成层（build_measurement_report）：按 paper_id 取数 + ActiveModelPointer
  引用回填 + 无活跃估计器边界
- A5/X6 静态扫描：src/core/data/ + src/core/report/ 不 import 学科包/学段包
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.active_model_pointer import ActiveModelPointer
from src.core.data.ctt import ResponseRecord, compute_ctt
from src.core.data.ctt_report import (
    CttReport,
    DifficultyBand,
    ItemStat,
    generate_ctt_report,
)
from src.core.events.writer import record_event
from src.core.report.measurement_report import (
    EstimatorRef,
    MeasurementReport,
    build_measurement_report,
)

# 固定时间戳用于确定性测试（与 ActiveModelPointer 历史回溯对齐）
T0 = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════
# 数据集构造
# ════════════════════════════════════════════════════════════════════


def _records_3items_4students() -> list[ResponseRecord]:
    """3 题 × 4 学生数据集（手算可核 α=0.75）.

    学生得分矩阵（行=学生，列=题 A/B/C）：
        s1: [1, 1, 1] → total=3
        s2: [1, 1, 0] → total=2
        s3: [1, 0, 0] → total=1
        s4: [0, 0, 0] → total=0

    手算 Cronbach's α（样本方差 n-1=3 分母）：
        σ²_A = 0.75/3 = 0.25      （[1,1,1,0], mean=0.75, ss=0.75）
        σ²_B = 1.0/3  ≈ 0.3333    （[1,1,0,0], mean=0.5,  ss=1.0）
        σ²_C = 0.75/3 = 0.25      （[1,0,0,0], mean=0.25, ss=0.75）
        Σσ²ᵢ ≈ 0.8333
        σ²_total = 5.0/3 ≈ 1.6667 （[3,2,1,0], mean=1.5, ss=5.0）
        α = (3/2)·(1 - 0.8333/1.6667) = 1.5 · 0.5 = 0.75
    SEM = sqrt(σ²_total) · sqrt(1-α) = sqrt(1.6667) · 0.5 ≈ 0.6455
    """
    answers = {
        "A": {"s1": 1.0, "s2": 1.0, "s3": 1.0, "s4": 0.0},  # p=0.75
        "B": {"s1": 1.0, "s2": 1.0, "s3": 0.0, "s4": 0.0},  # p=0.5
        "C": {"s1": 1.0, "s2": 0.0, "s3": 0.0, "s4": 0.0},  # p=0.25
    }
    records: list[ResponseRecord] = []
    for item, per_student in answers.items():
        for student, correct in per_student.items():
            records.append(
                ResponseRecord(
                    item_version_id=item,
                    student_alias_id=student,
                    correct=correct,
                )
            )
    return records


def _records_2items_30students() -> list[ResponseRecord]:
    """2 题 × 30 学生数据集（n=30 刚过小样本门槛）.

    用确定性序列（基于 student 索引取模）保证可复现：
    学生 i：A.correct = i%2, B.correct = (i+1)%2 —— 两题完全负相关，
    区分度可计算。
    """
    records: list[ResponseRecord] = []
    for i in range(30):
        sid = f"s{i}"
        records.append(
            ResponseRecord(
                item_version_id="A", student_alias_id=sid, correct=float(i % 2)
            )
        )
        records.append(
            ResponseRecord(
                item_version_id="B", student_alias_id=sid, correct=float((i + 1) % 2)
            )
        )
    return records


# ════════════════════════════════════════════════════════════════════
# §1 generate_ctt_report 返回完整报告（α / 区分度 / 难度 / SEM）
# ════════════════════════════════════════════════════════════════════


class TestGenerateCttReportStructure:
    """验收 #1：报告结构完整性与数值正确性."""

    def test_returns_ctt_report_with_all_fields(self) -> None:
        """返回 CttReport，含 α / SEM / item_stats / difficulty_distribution."""
        report = generate_ctt_report(
            _records_3items_4students(),
            paper_id="paper-test-001",
            now=T0,
        )
        assert isinstance(report, CttReport)
        assert report.paper_id == "paper-test-001"
        assert report.sample_size == 4  # 4 学生
        assert report.item_count == 3  # 3 题
        assert report.cronbach_alpha is not None
        assert report.sem is not None
        assert len(report.item_stats) == 3
        assert isinstance(report.difficulty_distribution, list)
        assert len(report.difficulty_distribution) == 5  # 五档
        assert report.generated_at == T0

    def test_cronbach_alpha_matches_hand_computed(self) -> None:
        """α 数值与手算一致（0.75）."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        assert report.cronbach_alpha == pytest.approx(0.75, rel=1e-9)

    def test_sem_matches_hand_computed(self) -> None:
        """SEM = SD_total · √(1-α) ≈ 0.6455."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        # σ²_total = 5/3, SD = sqrt(5/3), α=0.75
        expected_sem = math.sqrt(5.0 / 3.0) * math.sqrt(1.0 - 0.75)
        assert report.sem == pytest.approx(expected_sem, rel=1e-9)

    def test_item_stats_difficulty_is_correct_rate(self) -> None:
        """每题 difficulty = 正确率（A=0.75, B=0.5, C=0.25）."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        stats = {s.item_version_id: s for s in report.item_stats}
        assert stats["A"].difficulty == pytest.approx(0.75)
        assert stats["B"].difficulty == pytest.approx(0.5)
        assert stats["C"].difficulty == pytest.approx(0.25)
        # sample_size 每题都是 4（4 学生各一条）
        for s in report.item_stats:
            assert s.sample_size == 4

    def test_item_stats_sorted_by_item_version_id(self) -> None:
        """item_stats 按 item_version_id 升序（确定性）."""
        records = [
            ResponseRecord(item_version_id="Z", student_alias_id="s1", correct=1.0),
            ResponseRecord(item_version_id="A", student_alias_id="s1", correct=0.0),
        ]
        report = generate_ctt_report(records, paper_id="p", now=T0)
        assert [s.item_version_id for s in report.item_stats] == ["A", "Z"]

    def test_difficulty_distribution_five_bands(self) -> None:
        """难度分布五档：A(0.75)=somewhat_easy, B(0.5)=medium, C(0.25)=hard."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        bands = {b.band: b.count for b in report.difficulty_distribution}
        # C=0.25 落 [0.0, 0.3) = hard 档；B=0.5 落 [0.5, 0.7) = medium；
        # A=0.75 落 [0.7, 0.9) = somewhat_easy
        assert bands["hard"] == 1  # C=0.25
        assert bands["somewhat_hard"] == 0
        assert bands["medium"] == 1  # B=0.5
        assert bands["somewhat_easy"] == 1  # A=0.75
        assert bands["easy"] == 0
        # 每桶含 lower/upper 边界
        for b in report.difficulty_distribution:
            assert isinstance(b, DifficultyBand)
            assert b.lower < b.upper

    def test_difficulty_distribution_all_easy(self) -> None:
        """全部题难度=1.0（全对）→ easy 桶 = k."""
        records = [
            ResponseRecord(item_version_id=f"item-{i}", student_alias_id=f"s{i}", correct=1.0)
            for i in range(5)
        ]
        report = generate_ctt_report(records, paper_id="p", now=T0)
        bands = {b.band: b.count for b in report.difficulty_distribution}
        assert bands["easy"] == 5


# ════════════════════════════════════════════════════════════════════
# §2 小样本警示（n<30）
# ════════════════════════════════════════════════════════════════════


class TestSmallSampleWarning:
    """验收 #2：n<30 时报告头部标记「样本不足，结果仅供参考」."""

    def test_small_sample_sets_warning_flag(self) -> None:
        """n=4 < 30 → small_sample_warning=True."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        assert report.sample_size == 4
        assert report.small_sample_warning is True

    def test_small_sample_note_contains_required_phrase(self) -> None:
        """notes 含「样本不足，结果仅供参考」文案（验收 #2 原文）."""
        report = generate_ctt_report(
            _records_3items_4students(), paper_id="p", now=T0
        )
        joined = " | ".join(report.notes)
        assert "样本不足，结果仅供参考" in joined
        # 含 n 与 min_sample 数值便于审计
        assert "n=4" in joined
        assert "min_sample=30" in joined

    def test_at_threshold_no_warning(self) -> None:
        """n=30（边界）→ small_sample_warning=False."""
        report = generate_ctt_report(
            _records_2items_30students(), paper_id="p", now=T0
        )
        assert report.sample_size == 30
        assert report.small_sample_warning is False
        # 不应含小样本警示文案
        joined = " | ".join(report.notes)
        assert "样本不足" not in joined

    def test_custom_min_sample_threshold(self) -> None:
        """min_sample 可配置；n=30 < 50 → 警示."""
        report = generate_ctt_report(
            _records_2items_30students(), paper_id="p", min_sample=50, now=T0
        )
        assert report.small_sample_warning is True
        joined = " | ".join(report.notes)
        assert "min_sample=50" in joined

    def test_zero_sample_warns(self) -> None:
        """n=0 → 警示 + α=None + notes 含「无作答数据」."""
        report = generate_ctt_report([], paper_id="p", now=T0)
        assert report.sample_size == 0
        assert report.small_sample_warning is True
        assert report.cronbach_alpha is None
        assert report.sem is None
        joined = " | ".join(report.notes)
        assert "无作答数据" in joined


# ════════════════════════════════════════════════════════════════════
# §3 区分度与既有 CTT 实现一致（复用 compute_ctt）
# ════════════════════════════════════════════════════════════════════


class TestDiscriminationConsistency:
    """验收 #3：报告 item_stats.discrimination 与 compute_ctt 完全一致."""

    def test_discrimination_matches_compute_ctt(self) -> None:
        """报告区分度 == compute_ctt 的 discrimination（同一数据集）."""
        records = _records_3items_4students()
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        report = generate_ctt_report(records, paper_id="p", now=T0)
        for stat in report.item_stats:
            ctt_stat = ctt_stats[stat.item_version_id]
            assert stat.discrimination == ctt_stat.discrimination
            assert stat.difficulty == ctt_stat.difficulty
            assert stat.sample_size == ctt_stat.sample_size

    def test_discrimination_none_when_zero_variance(self) -> None:
        """全对（零方差）→ discrimination=None（与 compute_ctt 一致）."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=1.0)
            for i in range(4)
        ]
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        report = generate_ctt_report(records, paper_id="p", now=T0)
        assert ctt_stats["A"].discrimination is None
        assert report.item_stats[0].discrimination is None

    def test_discrimination_none_when_single_record(self) -> None:
        """n=1 → discrimination=None（与 compute_ctt 一致）."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id="s1", correct=1.0)
        ]
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        report = generate_ctt_report(records, paper_id="p", now=T0)
        assert ctt_stats["A"].discrimination is None
        assert report.item_stats[0].discrimination is None

    def test_discrimination_value_matches_hand_computed_pearson(self) -> None:
        """3 题数据集区分度与手算修正点二列 Pearson 一致."""
        records = _records_3items_4students()
        # 学生总分（A+B+C 各一条）：s1=3, s2=2, s3=1, s4=0
        # 题 A: xs=[1,1,1,0], 修正总分 ys=[2,1,0,0] (减本题得分)
        # mean_x=0.75, mean_y=0.75
        # sxy = 0.25*1.25 + 0.25*0.25 + 0.25*(-0.75) + (-0.75)*(-0.75)
        #     = 0.3125 + 0.0625 - 0.1875 + 0.5625 = 0.75
        # sxx = 0.75, syy = 1.25²+0.25²+0.75²+0.75² = 2.75
        # r = 0.75 / sqrt(0.75*2.75) ≈ 0.52223
        report = generate_ctt_report(records, paper_id="p", now=T0)
        stats = {s.item_version_id: s for s in report.item_stats}
        expected_r = 0.75 / math.sqrt(0.75 * 2.75)
        assert stats["A"].discrimination == pytest.approx(expected_r, rel=1e-9)


# ════════════════════════════════════════════════════════════════════
# Cronbach's α 边界情形
# ════════════════════════════════════════════════════════════════════


class TestCronbachAlphaEdgeCases:
    """α 在边界情形下的行为（不可计算时 None + notes 说明）."""

    def test_single_item_alpha_none(self) -> None:
        """k=1（单题）→ α=None（α 公式需 k≥2）."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=float(i % 2))
            for i in range(10)
        ]
        report = generate_ctt_report(records, paper_id="p", now=T0)
        assert report.item_count == 1
        assert report.cronbach_alpha is None
        assert report.sem is None
        joined = " | ".join(report.notes)
        assert "α 不可计算" in joined or "Cronbach" in joined

    def test_all_students_same_total_alpha_none(self) -> None:
        """全员同分（σ²_total=0）→ α=None."""
        # 两题，每个学生都得 1 分（A 对 B 错 或 A 错 B 对）
        records: list[ResponseRecord] = []
        for i in range(10):
            sid = f"s{i}"
            records.append(
                ResponseRecord(
                    item_version_id="A", student_alias_id=sid, correct=float(i % 2)
                )
            )
            records.append(
                ResponseRecord(
                    item_version_id="B", student_alias_id=sid, correct=float(1 - i % 2)
                )
            )
        report = generate_ctt_report(records, paper_id="p", now=T0)
        # 每个学生总分都是 1（A+B=1）→ σ²_total=0 → α=None
        assert report.cronbach_alpha is None
        joined = " | ".join(report.notes)
        assert "α 不可计算" in joined or "Cronbach" in joined

    def test_perfect_consistency_alpha_one(self) -> None:
        """题间完全一致（同学生对的题都对、错的都错）→ α 接近 1.0.

        学生 s1: A=1, B=1, C=1 → total=3
        学生 s2: A=0, B=0, C=0 → total=0
        （仅 2 学生，σ²_total 与 σ²ᵢ 完美一致 → α=1）
        """
        records = [
            ResponseRecord(item_version_id="A", student_alias_id="s1", correct=1.0),
            ResponseRecord(item_version_id="B", student_alias_id="s1", correct=1.0),
            ResponseRecord(item_version_id="C", student_alias_id="s1", correct=1.0),
            ResponseRecord(item_version_id="A", student_alias_id="s2", correct=0.0),
            ResponseRecord(item_version_id="B", student_alias_id="s2", correct=0.0),
            ResponseRecord(item_version_id="C", student_alias_id="s2", correct=0.0),
        ]
        report = generate_ctt_report(records, paper_id="p", now=T0)
        # 完美一致：σ²ᵢ 之和 = σ²_total → α = 1.0
        assert report.cronbach_alpha == pytest.approx(1.0, rel=1e-9)


# ════════════════════════════════════════════════════════════════════
# DB 集成层：build_measurement_report
# ════════════════════════════════════════════════════════════════════


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
    """插入最小 item + item_version（满足 response_event FK）."""
    item_id = f"item-for-{item_version_id[-8:]}"
    await db.execute(
        text("INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"),
        {"iid": item_id},
    )
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage)"
            " VALUES (:vid, :iid, 'draft', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
        ),
        {"vid": item_version_id, "iid": item_id},
    )
    await db.commit()


async def _answer_measurement(
    db: AsyncSession,
    *,
    item_version_id: str,
    student_alias_id,
    correct: int,
    paper_id: str,
    created_at: datetime | None = None,
) -> None:
    """写入一条测量场景作答事件（source_ref.paper_id 关联测量卷）."""
    await record_event(
        db,
        event_id=uuid4(),
        student_alias_id=student_alias_id,
        item_version_id=item_version_id,
        scene="measurement",
        raw_payload={"selected": "A"},
        scoring_trace={
            "scorer_id": "exact_match",
            "scorer_version": "1.0.0+test",
            "dimension_scores": {"correct": correct},
            "process": {},
            "confidence": {"scoring": 1.0},
        },
        error_inferences=[],
        source_ref={"paper_id": paper_id, "placement_token": "test-token"},
        created_at=created_at or datetime.now(timezone.utc),
    )


class TestBuildMeasurementReport:
    """DB 集成：按 paper_id 取数 + ActiveModelPointer 引用回填."""

    async def test_report_with_active_estimator_ref(
        self, async_session: AsyncSession
    ) -> None:
        """有活跃 measurement 估计器 → estimator_ref 回填完整字段."""
        paper_id = "paper-mr-001"
        # 准备 3 题 × 4 学生 measurement 作答（4 个固定学生 UUID 跨题复用）
        for vid in ("iv-A", "iv-B", "iv-C"):
            await _insert_item_version(async_session, vid)
        students = [uuid4() for _ in range(4)]  # s1..s4 跨题复用同一 UUID
        answers = {
            "iv-A": [1, 1, 1, 0],  # s1=1, s2=1, s3=1, s4=0
            "iv-B": [1, 1, 0, 0],
            "iv-C": [1, 0, 0, 0],
        }
        for vid, scores in answers.items():
            for sid, correct in zip(students, scores):
                await _answer_measurement(
                    async_session,
                    item_version_id=vid,
                    student_alias_id=sid,
                    correct=correct,
                    paper_id=paper_id,
                )

        # 登记活跃 measurement 估计器
        # activated_at 必须早于报告时刻 T0：get_active(timestamp=T0) 按 D6
        # 历史回溯语义仅返回 activated_at<=T0 的行；不传 activated_at 会默认
        # datetime.now()（晚于 T0）导致回溯落空——与同文件
        # test_estimator_ref_uses_timestamp_backtracking 的 t1/t2 模式一致。
        ptr = ActiveModelPointer(async_session)
        activated_at = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
        await ptr.set_active(
            "measurement", "ctt-v1",
            code_digest="sha256:ctt-report-code",
            input_snapshot_id="snap-mr-001",
            graph_release_id="gr-mr-001",
            activated_at=activated_at,
        )

        report = await build_measurement_report(
            async_session, paper_id=paper_id, now=T0
        )

        assert isinstance(report, MeasurementReport)
        assert report.paper_id == paper_id
        assert report.ctt_report.sample_size == 4
        assert report.ctt_report.item_count == 3
        assert report.ctt_report.cronbach_alpha == pytest.approx(0.75, rel=1e-9)
        # estimator_ref 字段完整回填
        assert report.estimator_ref is not None
        assert isinstance(report.estimator_ref, EstimatorRef)
        assert report.estimator_ref.purpose_scope == "measurement"
        assert report.estimator_ref.model_version == "ctt-v1"
        assert report.estimator_ref.code_digest == "sha256:ctt-report-code"
        assert report.estimator_ref.input_snapshot_id == "snap-mr-001"
        assert report.estimator_ref.graph_release_id == "gr-mr-001"

    async def test_report_without_active_estimator(
        self, async_session: AsyncSession
    ) -> None:
        """无活跃 measurement 估计器 → estimator_ref=None，ctt_report 仍生成."""
        paper_id = "paper-mr-no-est"
        await _insert_item_version(async_session, "iv-X")
        await _answer_measurement(
            async_session, item_version_id="iv-X", student_alias_id=uuid4(),
            correct=1, paper_id=paper_id,
        )

        report = await build_measurement_report(
            async_session, paper_id=paper_id, now=T0
        )
        # 无活跃估计器：estimator_ref=None（D6 引用缺失显式呈现，不阻塞）
        assert report.estimator_ref is None
        # ctt_report 仍照常生成（统计量与估计器版本无关）
        assert report.ctt_report.sample_size == 1

    async def test_scene_isolation_measurement_only(
        self, async_session: AsyncSession
    ) -> None:
        """D5 禁混估：practice 事件不计入 measurement 报告."""
        paper_id = "paper-mr-isolate"
        await _insert_item_version(async_session, "iv-Y")
        # measurement：3 对 1 错 → p=0.75
        for i in range(4):
            await _answer_measurement(
                async_session, item_version_id="iv-Y", student_alias_id=uuid4(),
                correct=0 if i == 0 else 1, paper_id=paper_id,
            )
        # practice：10 全对（若混入会把 p 抬高到 11/14≈0.786）
        for _ in range(10):
            await record_event(
                async_session,
                event_id=uuid4(),
                student_alias_id=uuid4(),
                item_version_id="iv-Y",
                scene="practice",
                raw_payload={"selected": "A"},
                scoring_trace={
                    "scorer_id": "exact_match",
                    "scorer_version": "1.0.0+test",
                    "dimension_scores": {"correct": 1},
                    "process": {},
                    "confidence": {"scoring": 1.0},
                },
                error_inferences=[],
                source_ref={"paper_id": paper_id},
                created_at=datetime.now(timezone.utc),
            )

        report = await build_measurement_report(
            async_session, paper_id=paper_id, now=T0
        )
        # 只计 measurement 的 4 条
        assert report.ctt_report.sample_size == 4
        assert report.ctt_report.item_stats[0].sample_size == 4
        assert report.ctt_report.item_stats[0].difficulty == pytest.approx(0.75)

    async def test_paper_id_isolation(self, async_session: AsyncSession) -> None:
        """不同 paper_id 的事件互不混入（按 source_ref.paper_id 精确过滤）."""
        paper_a = "paper-mr-A"
        paper_b = "paper-mr-B"
        await _insert_item_version(async_session, "iv-Z")
        # paper_a: 2 对
        for _ in range(2):
            await _answer_measurement(
                async_session, item_version_id="iv-Z", student_alias_id=uuid4(),
                correct=1, paper_id=paper_a,
            )
        # paper_b: 3 全错
        for _ in range(3):
            await _answer_measurement(
                async_session, item_version_id="iv-Z", student_alias_id=uuid4(),
                correct=0, paper_id=paper_b,
            )

        report_a = await build_measurement_report(
            async_session, paper_id=paper_a, now=T0
        )
        report_b = await build_measurement_report(
            async_session, paper_id=paper_b, now=T0
        )
        assert report_a.ctt_report.sample_size == 2
        assert report_a.ctt_report.item_stats[0].difficulty == pytest.approx(1.0)
        assert report_b.ctt_report.sample_size == 3
        assert report_b.ctt_report.item_stats[0].difficulty == pytest.approx(0.0)

    async def test_estimator_ref_uses_timestamp_backtracking(
        self, async_session: AsyncSession
    ) -> None:
        """D6：报告生成时刻决定引用哪个活跃估计器（历史回溯）."""
        paper_id = "paper-mr-timeline"
        await _insert_item_version(async_session, "iv-T")
        await _answer_measurement(
            async_session, item_version_id="iv-T", student_alias_id=uuid4(),
            correct=1, paper_id=paper_id,
        )

        ptr = ActiveModelPointer(async_session)
        # t1 时登记 ctt-v1，t2 时切换到 rasch-v1
        t1 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
        await ptr.set_active(
            "measurement", "ctt-v1", code_digest="d-ctt-v1",
            input_snapshot_id="s1", graph_release_id="g1", activated_at=t1,
        )
        await ptr.set_active(
            "measurement", "rasch-v1", code_digest="d-rasch-v1",
            input_snapshot_id="s2", graph_release_id="g2", activated_at=t2,
        )

        # t1.5 报告 → 引用 ctt-v1
        t_mid = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        report_mid = await build_measurement_report(
            async_session, paper_id=paper_id, now=t_mid
        )
        assert report_mid.estimator_ref is not None
        assert report_mid.estimator_ref.model_version == "ctt-v1"

        # t2 之后报告 → 引用 rasch-v1
        t_after = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
        report_after = await build_measurement_report(
            async_session, paper_id=paper_id, now=t_after
        )
        assert report_after.estimator_ref is not None
        assert report_after.estimator_ref.model_version == "rasch-v1"

    async def test_spec_table_ref_echoed(self, async_session: AsyncSession) -> None:
        """spec_table_ref 传入则回显（卷规约审计）."""
        paper_id = "paper-mr-spec"
        await _insert_item_version(async_session, "iv-S")
        await _answer_measurement(
            async_session, item_version_id="iv-S", student_alias_id=uuid4(),
            correct=1, paper_id=paper_id,
        )
        report = await build_measurement_report(
            async_session,
            paper_id=paper_id,
            spec_table_ref="spec-2026q3/1.0.0",
            now=T0,
        )
        assert report.spec_table_ref == "spec-2026q3/1.0.0"

    async def test_no_events_returns_empty_report(
        self, async_session: AsyncSession
    ) -> None:
        """paper_id 无任何作答事件 → 空报告（n=0, α=None）."""
        report = await build_measurement_report(
            async_session, paper_id="paper-empty", now=T0
        )
        assert report.ctt_report.sample_size == 0
        assert report.ctt_report.item_count == 0
        assert report.ctt_report.cronbach_alpha is None
        assert report.ctt_report.sem is None
        assert report.ctt_report.small_sample_warning is True


# ════════════════════════════════════════════════════════════════════
# §5 不 import 学科包/学段包（A5/X6 静态扫描）
# ════════════════════════════════════════════════════════════════════


def test_no_subject_pack_imports_in_data() -> None:
    """src/core/data/ 不 import 任何学科包/学段包（宪法 A5/A7）."""
    data_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "data"
    )
    assert data_dir.is_dir(), f"目录不存在：{data_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(data_dir.rglob("*.py")):
        if pattern.findall(py_file.read_text(encoding="utf-8")):
            violations.append(str(py_file.relative_to(data_dir)))
    assert not violations, (
        f"src/core/data/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )


def test_no_subject_pack_imports_in_report() -> None:
    """src/core/report/ 不 import 任何学科包/学段包（宪法 A5/A7）."""
    report_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "report"
    )
    assert report_dir.is_dir(), f"目录不存在：{report_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(report_dir.rglob("*.py")):
        if pattern.findall(py_file.read_text(encoding="utf-8")):
            violations.append(str(py_file.relative_to(report_dir)))
    assert not violations, (
        f"src/core/report/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
