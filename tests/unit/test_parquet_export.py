"""T-W4-006 response_event 每日增量 Parquet 归档测试.

覆盖任务卡验收 §1-§5：
  §1 daily_parquet_export 按日期分区导出，输出路径含日期与场景标记。
  §2 Parquet schema 与 response_event 全字段对齐，含 testlet_id / audio_play_events
     / dimension_scores 等 W4 字段。
  §3 幂等重跑：同一日期多次执行产出相同文件（去重键防重复写入）。
  §4 make accept TASK=T-W4-006 全绿（本文件即单元测试主体）。
  §5 不 import 任何学科包/学段包。

契约 specs/contracts/events/response_event.md §2.3「每日增量归档」。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pyarrow.parquet as pq
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.parquet_export import (
    PARQUET_SCHEMA,
    SCENES,
    ExportResult,
    build_output_path,
    export_daily,
    export_scene,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：清表 + 直插 response_event / score_run
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_events_and_runs(async_session: AsyncSession):
    """每测试前清空 response_event + score_run（append-only 表用 TRUNCATE CASCADE）.

    与 test_replay.py 同手法：TRUNCATE 一并清空外键依赖的 score_run。
    """
    await async_session.execute(text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE score_run RESTART IDENTITY CASCADE"))
    await async_session.commit()
    yield


def _today_utc() -> datetime:
    """今天 UTC 0 时刻（确保落在当前月分区内——0003 迁移创建了当月+未来3月分区）."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _insert_event(
    db: AsyncSession,
    *,
    event_id: UUID,
    created_at: datetime,
    scene: str = "practice",
    item_version_id: str = "sha256:pq-iv-001",
    student_alias_id: UUID | None = None,
    raw_payload: dict | None = None,
    duration_ms: int | None = None,
    testlet_id: str | None = None,
    session_id: UUID | None = None,
    audio_play_events: list | None = None,
    source_ref: dict | None = None,
    scoring_trace: dict | None = None,
    error_inferences: list | None = None,
) -> None:
    """直插 response_event（绕过 score_and_record，聚焦归档逻辑测试）.

    response_event append-only 触发器只禁 UPDATE/DELETE，INSERT 正常；
    分区表自动路由到 created_at 对应月分区（当月分区由 0003 创建）。
    """
    await db.execute(
        text(
            "INSERT INTO response_event ("
            "  event_id, student_alias_id, item_version_id, scene, raw_payload,"
            "  duration_ms, scoring_trace, error_inferences, testlet_id,"
            "  session_id, audio_play_events, source_ref, created_at"
            ") VALUES ("
            "  :eid, :sid, :ivid, CAST(:scene AS response_event_scene_enum),"
            "  CAST(:raw AS jsonb), :dur, CAST(:trace AS jsonb),"
            "  CAST(:infs AS jsonb), :tid, :ssid,"
            "  CAST(:audio AS jsonb), CAST(:sref AS jsonb), :cat"
            ")"
        ),
        {
            "eid": event_id,
            "sid": student_alias_id or uuid4(),
            "ivid": item_version_id,
            "scene": scene,
            "raw": json.dumps(raw_payload or {"selected": "B"}, ensure_ascii=False),
            "dur": duration_ms,
            "trace": json.dumps(
                scoring_trace
                or {
                    "scorer_id": "exact_match",
                    "scorer_version": "1.0.0+platform",
                    "process": {"correct": True},
                    "confidence": {"scoring": 1.0, "recognition": 0.0},
                },
                ensure_ascii=False,
            ),
            "infs": json.dumps(error_inferences or [], ensure_ascii=False),
            "tid": testlet_id,
            "ssid": session_id,
            "audio": json.dumps(audio_play_events, ensure_ascii=False) if audio_play_events else None,
            "sref": json.dumps(source_ref, ensure_ascii=False) if source_ref else None,
            "cat": created_at,
        },
    )
    await db.commit()


async def _insert_score_run(
    db: AsyncSession,
    *,
    event_id: UUID,
    event_created_at: datetime,
    dimension_scores: dict,
    rerun_of: str | None = None,
    purpose_scope: str = "practice",
    run_label: str | None = None,
    created_at: datetime | None = None,
) -> str:
    """直插 score_run（验证 dimension_scores enrichment）.

    rerun_of IS NULL = 原始评分；rerun_of 非 NULL = 重判结果。
    归档应取 rerun_of IS NULL 的最新一条作为 dimension_scores 代表行。
    """
    score_run_id = f"sr-{uuid4().hex[:12]}"
    await db.execute(
        text(
            "INSERT INTO score_run ("
            "  score_run_id, event_id, event_created_at, rerun_of, purpose_scope,"
            "  scorer_id, scorer_version, original_scorer_version, dimension_scores,"
            "  scoring_trace, error_inferences, correct, run_label, input_snapshot_id,"
            "  created_at"
            ") VALUES ("
            "  :srid, :eid, :ecat, :rrf, :ps, 'exact_match', 'exact_match-v1',"
            "  '1.0.0+platform', CAST(:ds AS jsonb), CAST(:trace AS jsonb),"
            "  CAST(:infs AS jsonb), TRUE, :label, 'snap-test', :cat"
            ")"
        ),
        {
            "srid": score_run_id,
            "eid": event_id,
            "ecat": event_created_at,
            "rrf": rerun_of,
            "ps": purpose_scope,
            "ds": json.dumps(dimension_scores, ensure_ascii=False),
            "trace": json.dumps({"scorer_id": "exact_match"}, ensure_ascii=False),
            "infs": json.dumps([], ensure_ascii=False),
            "label": run_label,
            "cat": created_at or datetime.now(timezone.utc),
        },
    )
    await db.commit()
    return score_run_id


# ────────────────────────────────────────────────────────────────────
# §1 路径含日期与场景标记 + 按日期分区导出
# ────────────────────────────────────────────────────────────────────


class TestPathAndPartitioning:
    """§1 输出路径含日期 + 场景双标记；按场景分文件。"""

    def test_build_output_path_format(self):
        """路径格式 date=YYYY-MM-DD/scene=<scene>/events-YYYYMMDD-<scene>.parquet."""
        path = build_output_path(Path("/archive"), date(2026, 7, 27), "practice")
        assert path == Path(
            "/archive/date=2026-07-27/scene=practice/events-20260727-practice.parquet"
        )

    async def test_export_daily_produces_per_scene_files(self, async_session, tmp_path):
        """§1 每场景一个文件，路径含日期与场景标记."""
        target = _today_utc().date()
        # 三个场景各 1 事件
        for scene in SCENES:
            await _insert_event(
                async_session,
                event_id=uuid4(),
                created_at=_today_utc(),
                scene=scene,
            )

        results = await export_daily(async_session, tmp_path, target)

        assert len(results) == 3
        for r in results:
            assert r.target_date == target
            assert r.row_count == 1
            assert r.path is not None
            assert r.path.exists()
            # 路径含日期与场景标记
            assert f"date={target.isoformat()}" in str(r.path)
            assert f"scene={r.scene}" in str(r.path)
            assert r.scene in r.path.name

    async def test_scene_subset_filter(self, async_session, tmp_path):
        """--scenes 子集：仅导出指定场景."""
        target = _today_utc().date()
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="diagnosis"
        )

        results = await export_daily(async_session, tmp_path, target, scenes=("practice",))

        assert len(results) == 1
        assert results[0].scene == "practice"
        assert results[0].row_count == 1


# ────────────────────────────────────────────────────────────────────
# §2 schema 与 response_event 全字段对齐 + dimension_scores
# ────────────────────────────────────────────────────────────────────


class TestSchemaAlignment:
    """§2 Parquet schema 含 response_event 全字段 + dimension_scores."""

    async def test_schema_has_all_response_event_fields(self):
        """schema 字段集 ⊇ response_event 契约 §1 全字段 + dimension_scores."""
        field_names = set(PARQUET_SCHEMA.names)
        expected = {
            "event_id", "student_alias_id", "item_version_id", "scene",
            "raw_payload", "duration_ms", "scoring_trace", "error_inferences",
            "testlet_id", "session_id", "audio_play_events", "source_ref",
            "created_at", "dimension_scores",
        }
        assert expected.issubset(field_names)

    async def test_exported_parquet_has_full_field_values(self, async_session, tmp_path):
        """§2 写入的 Parquet 文件全字段值可回读，含 testlet_id / audio_play_events
        / dimension_scores（W4 字段）."""
        target = _today_utc().date()
        eid = uuid4()
        sid = uuid4()
        ssid = uuid4()
        cat = _today_utc()
        ds = {"correct": 1.0, "process": 0.8}

        await _insert_event(
            async_session,
            event_id=eid,
            created_at=cat,
            scene="practice",
            student_alias_id=sid,
            item_version_id="sha256:pq-full-iv",
            raw_payload={"selected": "B", "note": "测试"},
            duration_ms=5320,
            testlet_id="testlet-001",
            session_id=ssid,
            audio_play_events=[{"play_count": 2, "limited": False}],
            source_ref={"paper_id": "paper-1", "placement_token": "q3"},
        )
        await _insert_score_run(
            async_session,
            event_id=eid,
            event_created_at=cat,
            dimension_scores=ds,
        )

        results = await export_daily(async_session, tmp_path, target, scenes=("practice",))
        r = results[0]
        assert r.row_count == 1

        # 回读 Parquet 验证全字段
        table = pq.read_table(r.path)
        assert table.num_rows == 1
        row = table.to_pylist()[0]

        assert row["event_id"] == str(eid)
        assert row["student_alias_id"] == str(sid)
        assert row["item_version_id"] == "sha256:pq-full-iv"
        assert row["scene"] == "practice"
        assert json.loads(row["raw_payload"]) == {"selected": "B", "note": "测试"}
        assert row["duration_ms"] == 5320
        trace = json.loads(row["scoring_trace"])
        assert trace["scorer_id"] == "exact_match"
        infs = json.loads(row["error_inferences"])
        assert infs == []
        assert row["testlet_id"] == "testlet-001"
        assert row["session_id"] == str(ssid)
        assert json.loads(row["audio_play_events"]) == [{"play_count": 2, "limited": False}]
        assert json.loads(row["source_ref"]) == {"paper_id": "paper-1", "placement_token": "q3"}
        # W4 enrichment
        assert json.loads(row["dimension_scores"]) == ds
        # created_at 时区保留
        assert row["created_at"].year == cat.year
        assert row["created_at"].month == cat.month
        assert row["created_at"].day == cat.day

    async def test_dimension_scores_null_when_no_score_run(self, async_session, tmp_path):
        """§2 无 score_run 的事件 dimension_scores 为 NULL（enrichment 缺失不报错）."""
        target = _today_utc().date()
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )

        results = await export_daily(async_session, tmp_path, target, scenes=("practice",))
        table = pq.read_table(results[0].path)
        row = table.to_pylist()[0]
        assert row["dimension_scores"] is None

    async def test_dimension_scores_prefers_non_rerun(self, async_session, tmp_path):
        """enrichment 取 rerun_of IS NULL 的最新一条（原始评分优先于重判）."""
        target = _today_utc().date()
        eid = uuid4()
        cat = _today_utc()
        await _insert_event(
            async_session, event_id=eid, created_at=cat, scene="practice"
        )
        # 原始评分
        original_sr = await _insert_score_run(
            async_session,
            event_id=eid,
            event_created_at=cat,
            dimension_scores={"correct": 1.0, "tag": "original"},
        )
        # 重判（rerun_of 指向原始）——不应被 enrichment 选中
        await _insert_score_run(
            async_session,
            event_id=eid,
            event_created_at=cat,
            dimension_scores={"correct": 0.0, "tag": "rerun"},
            rerun_of=original_sr,
            created_at=cat + timedelta(hours=1),
        )

        results = await export_daily(async_session, tmp_path, target, scenes=("practice",))
        table = pq.read_table(results[0].path)
        row = table.to_pylist()[0]
        ds = json.loads(row["dimension_scores"])
        assert ds["tag"] == "original"

    async def test_duration_ms_nullable_preserved(self, async_session, tmp_path):
        """duration_ms NULL=未知（契约禁止填 0 冒充）；Parquet 保留 None."""
        target = _today_utc().date()
        await _insert_event(
            async_session,
            event_id=uuid4(),
            created_at=_today_utc(),
            scene="practice",
            duration_ms=None,
        )

        results = await export_daily(async_session, tmp_path, target, scenes=("practice",))
        table = pq.read_table(results[0].path)
        row = table.to_pylist()[0]
        assert row["duration_ms"] is None


# ────────────────────────────────────────────────────────────────────
# §3 幂等重跑：同输入同输出（去重键防重复写入）
# ────────────────────────────────────────────────────────────────────


class TestIdempotency:
    """§3 幂等：同日期多次执行产出相同文件."""

    async def test_rerun_skips_when_hash_matches(self, async_session, tmp_path):
        """§3 第二次执行：manifest 哈希匹配 → skipped_unchanged=True，文件未重写."""
        target = _today_utc().date()
        eid = uuid4()
        await _insert_event(
            async_session, event_id=eid, created_at=_today_utc(), scene="practice"
        )

        first = await export_scene(async_session, tmp_path, target, "practice")
        assert first.skipped_unchanged is False
        assert first.row_count == 1
        first_mtime = first.path.stat().st_mtime_ns
        first_hash = first.content_hash

        # 第二次：哈希应匹配，跳过重写
        second = await export_scene(async_session, tmp_path, target, "practice")
        assert second.skipped_unchanged is True
        assert second.row_count == 1
        assert second.content_hash == first_hash
        assert second.path == first.path
        # 文件未被重写（mtime 不变）
        assert second.path.stat().st_mtime_ns == first_mtime

    async def test_rerun_after_data_change_rewrites(self, async_session, tmp_path):
        """§3 数据变更后：哈希不匹配 → 重写文件，skipped_unchanged=False."""
        target = _today_utc().date()
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        first = await export_scene(async_session, tmp_path, target, "practice")
        assert first.row_count == 1
        assert first.skipped_unchanged is False

        # 新增事件 → 哈希变化
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        second = await export_scene(async_session, tmp_path, target, "practice")
        assert second.skipped_unchanged is False
        assert second.row_count == 2
        assert second.content_hash != first.content_hash
        # Parquet 文件行数更新
        table = pq.read_table(second.path)
        assert table.num_rows == 2

    async def test_same_input_produces_same_content_hash(self, async_session, tmp_path):
        """§3 同输入必同 content_hash（确定性基础）."""
        target = _today_utc().date()
        eid = uuid4()
        await _insert_event(
            async_session, event_id=eid, created_at=_today_utc(), scene="practice"
        )

        r1 = await export_scene(async_session, tmp_path, target, "practice")
        # 不同输出目录重算 → 同哈希
        other_dir = tmp_path / "other"
        r2 = await export_scene(async_session, other_dir, target, "practice")
        assert r1.content_hash == r2.content_hash
        assert r1.row_count == r2.row_count

    async def test_dedup_by_event_id(self, async_session, tmp_path):
        """§3 去重键防重复：同 event_id 重复行只保留首条."""
        target = _today_utc().date()
        eid = uuid4()
        cat = _today_utc()
        # 直接插两条同 event_id + created_at（PK 冲突会拒绝），
        # 改为验证去重逻辑：插一条事件 + 模拟取数重复（通过单测 _dedup_by_event_id）
        await _insert_event(
            async_session, event_id=eid, created_at=cat, scene="practice"
        )
        from src.core.data.parquet_export import _dedup_by_event_id, _normalize_row

        # 构造重复行
        row = {
            "event_id": eid, "student_alias_id": uuid4(), "item_version_id": "iv",
            "scene": "practice", "raw_payload": {"selected": "B"}, "duration_ms": 100,
            "scoring_trace": {"scorer_id": "x"}, "error_inferences": [],
            "testlet_id": None, "session_id": None, "audio_play_events": None,
            "source_ref": None, "created_at": cat, "dimension_scores": None,
        }
        normalized = _normalize_row(row)
        deduped = _dedup_by_event_id([normalized, normalized, normalized])
        assert len(deduped) == 1

        # 正常导出也只有 1 行
        r = await export_scene(async_session, tmp_path, target, "practice")
        assert r.row_count == 1

    async def test_manifest_file_written(self, async_session, tmp_path):
        """manifest 文件与 parquet 同目录，含 content_hash + row_count."""
        target = _today_utc().date()
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        r = await export_scene(async_session, tmp_path, target, "practice")
        manifest = r.path.with_suffix(".parquet.manifest.json")
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["content_hash"] == r.content_hash
        assert data["row_count"] == 1
        assert data["scene"] == "practice"
        assert data["target_date"] == target.isoformat()


# ────────────────────────────────────────────────────────────────────
# 边界：空场景 / 非法场景 / 多场景隔离
# ────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """边界场景处理."""

    async def test_empty_scene_creates_no_file(self, async_session, tmp_path):
        """空场景：row_count==0 时不创建文件，path=None."""
        target = _today_utc().date()
        # 不插入任何事件
        r = await export_scene(async_session, tmp_path, target, "practice")
        assert r.row_count == 0
        assert r.path is None
        assert r.content_hash == ""
        assert r.skipped_unchanged is True  # 空操作视为跳过
        # 不创建 parquet 文件
        assert not build_output_path(tmp_path, target, "practice").exists()

    async def test_invalid_scene_raises(self, async_session, tmp_path):
        """非法 scene 抛 ValueError（D5 场景域约束）."""
        target = _today_utc().date()
        with pytest.raises(ValueError, match="非法 scene"):
            await export_scene(async_session, tmp_path, target, "invalid_scene")

    async def test_scenes_isolated_in_separate_files(self, async_session, tmp_path):
        """多场景隔离：每个场景独立文件，行数不串."""
        target = _today_utc().date()
        # practice 2 条，diagnosis 1 条，measurement 0 条
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="practice"
        )
        await _insert_event(
            async_session, event_id=uuid4(), created_at=_today_utc(), scene="diagnosis"
        )

        results = await export_daily(async_session, tmp_path, target)
        by_scene = {r.scene: r for r in results}
        assert by_scene["practice"].row_count == 2
        assert by_scene["diagnosis"].row_count == 1
        assert by_scene["measurement"].row_count == 0
        assert by_scene["measurement"].path is None

    async def test_date_range_filters_correctly(self, async_session, tmp_path):
        """日期范围过滤：只导出目标日的事件，其他日不入选."""
        target = _today_utc().date()
        # 昨天的事件（若昨天在当月分区内）——不一定在分区内，改用「明天」确保分区内
        today = _today_utc()
        tomorrow = today + timedelta(days=1)
        # 目标日 = 今天：今天的事件入选，明天的不入选
        await _insert_event(
            async_session, event_id=uuid4(), created_at=today, scene="practice"
        )
        await _insert_event(
            async_session, event_id=uuid4(), created_at=tomorrow, scene="practice"
        )

        r = await export_scene(async_session, tmp_path, target, "practice")
        assert r.row_count == 1  # 只有今天的事件


# ────────────────────────────────────────────────────────────────────
# §5 不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """§5 核心域禁止 import 学科包/学段包（宪法 A5/X6）。"""

    def test_parquet_export_module_no_subject_pack_imports(self):
        """parquet_export 模块不 import src.packs / src.gradeband_packs."""
        import src.core.data.parquet_export as mod
        # 检查模块源码不含学科包/学段包 import
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "src.packs" not in source
        assert "src.gradeband" not in source
        assert "from src.packs" not in source
        assert "import src.packs" not in source

    def test_parquet_export_not_in_subject_pack_modules(self):
        """parquet_export 在核心域 src/core/data/ 下，不在学科包目录."""
        import src.core.data.parquet_export as mod
        path = Path(mod.__file__).resolve()
        # 必须在 src/core/ 下，不在 src/packs/ 下
        assert "src" + chr(92) + "core" in str(path) or "src/core" in str(path)
        assert "src" + chr(92) + "packs" not in str(path)
        assert "src/packs" not in str(path)


class TestEnrichmentTiebreak:
    """LATERAL 最新行取行的确定性（#59 同类缺陷的 score_run 面）."""

    async def test_same_created_at_tie_picks_deterministically(
        self, async_session, tmp_path
    ):
        """同 created_at 的多条 rerun_of IS NULL 行：取行必须字节确定.

        场景：单事务批量重判（铁律 9）对同一事件先后写两条 rerun_of IS NULL
        行（不同 run_label，uq_score_run_identity 允许）——created_at 默认
        now() 是事务级时间戳，两行相同。无唯一 tiebreak 时 PG 取行不确定，
        导出违背「同输入同字节」；修复后按 score_run_id DESC 稳定取行。
        断言：两次导出取到的代表行一致，且为 score_run_id 字典序更大者。
        """
        target = _today_utc().date()
        eid = uuid4()
        cat = _today_utc()
        await _insert_event(
            async_session, event_id=eid, created_at=cat, scene="practice"
        )
        # 受控 ID（字典序 AA < BB）：ORDER BY score_run_id DESC 稳定取 BB（second-write）
        for srid, label, tag in [
            ("sr-tiebreak-controlled-AA", "batch-1", "first-write"),
            ("sr-tiebreak-controlled-BB", "batch-2", "second-write"),
        ]:
            await async_session.execute(
                text(
                    "INSERT INTO score_run ("
                    "  score_run_id, event_id, event_created_at, rerun_of, purpose_scope,"
                    "  scorer_id, scorer_version, original_scorer_version,"
                    "  dimension_scores, scoring_trace, error_inferences, correct,"
                    "  run_label, input_snapshot_id, created_at"
                    ") VALUES ("
                    "  :srid, :eid, :ecat, NULL, 'practice', 'exact_match',"
                    "  'exact_match-v1', '1.0.0+platform',"
                    "  CAST(:ds AS jsonb), CAST('{}' AS jsonb),"
                    "  CAST('[]' AS jsonb), TRUE, :label, 'snap-tie', :cat"
                    ")"
                ),
                {
                    "srid": srid,
                    "eid": eid,
                    "ecat": cat,
                    "ds": json.dumps({"correct": 1.0, "tag": tag}),
                    "label": label,
                    "cat": cat,  # 同 created_at（事务级时间戳的等价模拟）
                },
            )
        await async_session.commit()

        picked_tags = []
        for _ in range(2):
            results = await export_daily(
                async_session, tmp_path, target, scenes=("practice",)
            )
            table = pq.read_table(results[0].path)
            row = table.to_pylist()[0]
            picked_tags.append(json.loads(row["dimension_scores"])["tag"])

        assert picked_tags == ["second-write", "second-write"]
