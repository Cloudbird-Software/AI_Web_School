"""T-W4-006 response_event 每日增量 Parquet 归档.

落地 specs/contracts/events/response_event.md §2.3「每日增量归档：导出 Parquet
至对象存储」——十年数据主权依赖开放列式格式（Parquet 是 Apache 开放标准，
不绑定厂商，与 PostgreSQL 解耦后仍可独立读十年前的数据）。

核心接口：
- export_daily(db, base_dir, target_date=None, *, scenes=SCENES) → list[ExportResult]
  导出 target_date（默认昨日 UTC）的全场景增量，每场景一个 Parquet 文件。
- export_scene(db, base_dir, target_date, scene) → ExportResult
  导出单场景；幂等：同输入必同输出（内容哈希 manifest 比对，匹配则跳过重写）。

设计要点：
- 路径分区：{base_dir}/date={YYYY-MM-DD}/scene={scene}/events-{YYYYMMDD}-{scene}.parquet
  日期 + 场景双标记（验收 §1）；按月分区表的 created_at 与文件路径日期对齐。
- schema 与 response_event 全字段对齐（契约 §1）+ dimension_scores enrichment
  （LEFT JOIN LATERAL score_run 取 rerun_of IS NULL 的最新一条；W4 新增字段）。
- 幂等三段防重：
  (1) 取数 SQL 已 ORDER BY created_at, event_id，行序确定；
  (2) 内容哈希 SHA256(排序后逐行字段 canonical JSON)，写 manifest 文件；
  (3) 重跑时先读 manifest，哈希匹配则跳过重写（不重写文件 = 不动归档存储）。
  哈希不匹配（数据增量或重算）则原子替换（temp file + os.replace）。
- 去重键 event_id：分区表 PK=(event_id, created_at)，event_id 全局唯一（应用层
  ULID/uuid 保证），同 (date, scene) 内按 event_id 去重保留首条（防御取数重复）。

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
non_goals（任务卡）：对象存储生命周期管理 / 实时流式导出 / 数据仓库对接 / 增量压缩。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 场景三值域（与 ctt / replay / D5 对齐）
SCENES: tuple[str, ...] = ("practice", "diagnosis", "measurement")

# Parquet schema：与 response_event 契约 §1 全字段对齐 + dimension_scores（W4）
# 为什么 JSONB 转 string：Parquet 原生无 JSONB 类型；存 canonical JSON 字符串
# 保真且可被 Athena/DuckDB/Spark 直接 JSON 解析。canonical（sort_keys=True）
# 保证同输入同字节，幂等基础。
PARQUET_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("student_alias_id", pa.string(), nullable=False),
        pa.field("item_version_id", pa.string(), nullable=False),
        pa.field("scene", pa.string(), nullable=False),
        pa.field("raw_payload", pa.string(), nullable=False),
        pa.field("duration_ms", pa.int32(), nullable=True),
        pa.field("scoring_trace", pa.string(), nullable=False),
        pa.field("error_inferences", pa.string(), nullable=False),
        pa.field("testlet_id", pa.string(), nullable=True),
        pa.field("session_id", pa.string(), nullable=True),
        pa.field("audio_play_events", pa.string(), nullable=True),
        pa.field("source_ref", pa.string(), nullable=True),
        pa.field("created_at", pa.timestamp("us", tz="UTC"), nullable=False),
        # W4 enrichment：来自 score_run 表（重判结果账），取 rerun_of IS NULL 的
        # 最新一条；无 score_run（如重判未跑过）则为 NULL。
        pa.field("dimension_scores", pa.string(), nullable=True),
    ]
)

# Manifest 后缀：与 parquet 文件同目录，存内容哈希 + 行数（幂等比对源）
_MANIFEST_SUFFIX = ".manifest.json"

# Parquet 写入参数（确定性的关键：version/compression 固定，不写非确定性元数据）
_PARQUET_VERSION = "2.6"
_PARQUET_COMPRESSION = "snappy"


# ────────────────────────────────────────────────────────────────────
# 报告数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExportResult:
    """单场景单日导出结果.

    - scene / target_date：定位键
    - path：输出文件路径；row_count==0 且无既有文件时为 None（不创建空文件）
    - row_count：去重后写入行数
    - content_hash：内容 SHA256（排序后逐行 canonical JSON）；空集为空串
    - skipped_unchanged：True 表示既有 manifest 哈希匹配，本次未重写文件
    """

    scene: str
    target_date: date
    path: Optional[Path]
    row_count: int
    content_hash: str
    skipped_unchanged: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path) if self.path is not None else None
        d["target_date"] = self.target_date.isoformat()
        return d


# ────────────────────────────────────────────────────────────────────
# 路径与日期范围
# ────────────────────────────────────────────────────────────────────


def build_output_path(base_dir: Path, target_date: date, scene: str) -> Path:
    """构造输出路径：{base}/date={YYYY-MM-DD}/scene={scene}/events-{YYYYMMDD}-{scene}.parquet.

    为什么路径含日期 + 场景双标记：验收 §1 要求；同时与分区表 created_at 月度
    分区对齐，下游可按路径前缀裁剪扫描范围（避免全表扫描）。
    """
    date_str = target_date.isoformat()  # YYYY-MM-DD
    compact = target_date.strftime("%Y%m%d")  # YYYYMMDD
    return (
        base_dir
        / f"date={date_str}"
        / f"scene={scene}"
        / f"events-{compact}-{scene}.parquet"
    )


def _manifest_path(parquet_path: Path) -> Path:
    """manifest 与 parquet 同目录同名加后缀."""
    return parquet_path.with_suffix(".parquet" + _MANIFEST_SUFFIX)


def _date_range_utc(target_date: date) -> tuple[datetime, datetime]:
    """目标日的 UTC [00:00, 次日 00:00) 区间.

    为什么用 UTC 半开区间：response_event.created_at 是 timestamptz（UTC 存储）；
    「昨日」按 UTC 划界保证全球时区一致，不因部署时区漂移。
    """
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


# ────────────────────────────────────────────────────────────────────
# 取数：response_event 全字段 + score_run dimension_scores enrichment
# ────────────────────────────────────────────────────────────────────

# 为什么 LEFT JOIN LATERAL：score_run 是平行账（W4-003），一个事件可能有多条
# score_run（重判历史）；取 rerun_of IS NULL 的最新一条作为「该事件的维度分数」
# 代表行。LATERAL 子查询保证每事件至多一行，不放大 response_event 行数。
# 为什么 ORDER BY created_at, event_id：行序确定是幂等基础（同输入同字节）。
# LATERAL 内为什么补 score_run_id tiebreak：created_at 默认 now() 是事务级时间戳，
# 单事务批量重判（铁律 9）写入的同事件多条 rerun_of IS NULL 行 created_at 相同，
# 无唯一 tiebreak 时 PG 取行不确定——违背本模块「同输入同字节」承诺（#59 同类）。
_FETCH_ROWS_SQL = """
SELECT re.event_id,
       re.student_alias_id,
       re.item_version_id,
       re.scene,
       re.raw_payload,
       re.duration_ms,
       re.scoring_trace,
       re.error_inferences,
       re.testlet_id,
       re.session_id,
       re.audio_play_events,
       re.source_ref,
       re.created_at,
       sr.dimension_scores AS dimension_scores
FROM response_event re
LEFT JOIN LATERAL (
    SELECT sr.dimension_scores
    FROM score_run sr
    WHERE sr.event_id = re.event_id
      AND sr.event_created_at = re.created_at
      AND sr.rerun_of IS NULL
    ORDER BY sr.created_at DESC, sr.score_run_id DESC
    LIMIT 1
) sr ON true
WHERE re.created_at >= :start_ts
  AND re.created_at < :end_ts
  AND re.scene = :scene
ORDER BY re.created_at ASC, re.event_id ASC
"""


async def _fetch_rows(
    db: AsyncSession, target_date: date, scene: str
) -> list[dict[str, Any]]:
    """取目标日 + 场景的全字段行（含 dimension_scores enrichment）.

    返回 list[dict]：JSONB 字段已被 SQLAlchemy/asyncpg 解码为 dict/list；
    UUID 字段为 uuid 对象；datetime 为 timezone-aware datetime。
    """
    start_ts, end_ts = _date_range_utc(target_date)
    result = await db.execute(
        text(_FETCH_ROWS_SQL),
        {"start_ts": start_ts, "end_ts": end_ts, "scene": scene},
    )
    rows: list[dict[str, Any]] = []
    for row in result.mappings():
        rows.append(dict(row))
    return rows


# ────────────────────────────────────────────────────────────────────
# 行规范化 + 去重 + 哈希
# ────────────────────────────────────────────────────────────────────


def _canonical_json(value: Any) -> str:
    """JSONB → canonical JSON 字符串（sort_keys + ensure_ascii=False）.

    为什么 ensure_ascii=False：中文题面/错误类型标签原样保留，避免 \\uXXXX
    膨胀且不利下游阅读；UTF-8 字节序在 SHA256 输入中确定。
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """将 DB 行规范化为 Parquet schema 对齐的 dict.

    - UUID → str
    - JSONB（dict/list）→ canonical JSON 字符串
    - None 保持 None（nullable 字段）
    - datetime 保留（pyarrow 直接处理）
    """
    return {
        "event_id": str(row["event_id"]),
        "student_alias_id": str(row["student_alias_id"]),
        "item_version_id": str(row["item_version_id"]),
        "scene": str(row["scene"]),
        "raw_payload": _canonical_json(row["raw_payload"]),
        "duration_ms": row["duration_ms"],
        "scoring_trace": _canonical_json(row["scoring_trace"]),
        "error_inferences": _canonical_json(row["error_inferences"]),
        "testlet_id": str(row["testlet_id"]) if row["testlet_id"] is not None else None,
        "session_id": str(row["session_id"]) if row["session_id"] is not None else None,
        "audio_play_events": (
            _canonical_json(row["audio_play_events"])
            if row["audio_play_events"] is not None
            else None
        ),
        "source_ref": (
            _canonical_json(row["source_ref"]) if row["source_ref"] is not None else None
        ),
        "created_at": row["created_at"],
        "dimension_scores": (
            _canonical_json(row["dimension_scores"])
            if row["dimension_scores"] is not None
            else None
        ),
    }


def _dedup_by_event_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 event_id 去重保留首条（防御取数重复；行已按 created_at, event_id 排序）.

    为什么需要：虽然 (event_id, created_at) 是分区表 PK，但若 score_run LATERAL
    子查询或分区边界异常导致重复，去重保证归档幂等。同 event_id 多行只保留首条
    （即最早 created_at 的那条，与排序一致）。
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        eid = str(r["event_id"])
        if eid in seen:
            logger.warning("parquet_export: 重复 event_id 已去重 %s", eid)
            continue
        seen.add(eid)
        deduped.append(r)
    return deduped


def _compute_content_hash(normalized_rows: list[dict[str, Any]]) -> str:
    """计算内容哈希：SHA256(逐行 canonical JSON).

    为什么逐行而非整表 dumps：行已确定序，逐行 update 后取 hexdigest；
    整表 dumps 受 dict 内嵌顺序影响（虽然 sort_keys=True 已稳），逐行更明确。
    空集返回空串（manifest 比对时区分「无数据」与「未导出」）。
    """
    if not normalized_rows:
        return ""
    h = hashlib.sha256()
    for r in normalized_rows:
        # 行 canonical：键序固定，datetime 转 ISO8601
        row_canonical = json.dumps(
            r, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        h.update(row_canonical.encode("utf-8"))
    return h.hexdigest()


def _rows_to_table(normalized_rows: list[dict[str, Any]]) -> pa.Table:
    """构造 pyarrow Table（schema 强制对齐，空集也返回空表保 schema）."""
    return pa.Table.from_pylist(normalized_rows, schema=PARQUET_SCHEMA)


# ────────────────────────────────────────────────────────────────────
# Parquet + manifest 原子写入
# ────────────────────────────────────────────────────────────────────


def _write_parquet_atomic(table: pa.Table, parquet_path: Path) -> None:
    """原子写 Parquet：写 .tmp 后 os.replace（同文件系统原子替换）.

    为什么原子写：归档文件可能被下游（数据仓库/对象存储同步）并发读；
    非原子写会读到半成品。os.replace 在同一文件系统上是原子的（POSIX rename
    语义；Windows 上也是原子替换目标文件）。
    """
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parquet_path.with_suffix(parquet_path.suffix + ".tmp")
    pq.write_table(
        table,
        tmp_path,
        version=_PARQUET_VERSION,
        compression=_PARQUET_COMPRESSION,
        use_dictionary=True,
        write_statistics=True,
    )
    os.replace(tmp_path, parquet_path)


def _write_manifest_atomic(
    manifest_path: Path, content_hash: str, row_count: int, scene: str, target_date: date
) -> None:
    """原子写 manifest：{content_hash, row_count, scene, target_date, written_at}.

    written_at 仅记录写入时刻（不参与幂等比对，仅审计）；content_hash 是幂等键。
    """
    manifest = {
        "scene": scene,
        "target_date": target_date.isoformat(),
        "row_count": row_count,
        "content_hash": content_hash,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": "1",
    }
    tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, manifest_path)


def _read_manifest(manifest_path: Path) -> Optional[dict[str, Any]]:
    """读既有 manifest；不存在或 JSON 解析失败返回 None（视为需重写）."""
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("parquet_export: manifest 读取失败，将重写 %s: %s", manifest_path, exc)
        return None


# ────────────────────────────────────────────────────────────────────
# 主接口
# ────────────────────────────────────────────────────────────────────


async def export_scene(
    db: AsyncSession,
    base_dir: Path,
    target_date: date,
    scene: str,
) -> ExportResult:
    """导出单场景单日到 Parquet（幂等）.

    流程：
    1. 取数（response_event 全字段 + score_run dimension_scores enrichment）
    2. 规范化 + event_id 去重
    3. 计算内容哈希
    4. 读既有 manifest；哈希匹配 → 跳过重写（skipped_unchanged=True）
    5. 不匹配 → 原子写 Parquet + manifest

    空集处理：row_count==0 时不创建文件；若既有文件存在则保留（数据被删的异常
    场景——response_event append-only 不应发生，记 warning 不动既有归档）。
    """
    if scene not in SCENES:
        raise ValueError(
            f"非法 scene={scene!r}；合法值 {SCENES}（D5 分场景独立统计禁止混估）"
        )

    raw_rows = await _fetch_rows(db, target_date, scene)
    normalized = [_normalize_row(r) for r in raw_rows]
    deduped = _dedup_by_event_id(normalized)
    content_hash = _compute_content_hash(deduped)

    parquet_path = build_output_path(base_dir, target_date, scene)
    manifest_path = _manifest_path(parquet_path)

    if deduped:
        existing = _read_manifest(manifest_path)
        if existing is not None and existing.get("content_hash") == content_hash:
            logger.info(
                "parquet_export: scene=%s date=%s 哈希匹配，跳过重写（%d 行）",
                scene, target_date.isoformat(), len(deduped),
            )
            return ExportResult(
                scene=scene,
                target_date=target_date,
                path=parquet_path,
                row_count=len(deduped),
                content_hash=content_hash,
                skipped_unchanged=True,
            )
        # 哈希不匹配或无 manifest：原子重写
        table = _rows_to_table(deduped)
        _write_parquet_atomic(table, parquet_path)
        _write_manifest_atomic(
            manifest_path, content_hash, len(deduped), scene, target_date
        )
        logger.info(
            "parquet_export: scene=%s date=%s 写入 %d 行 → %s",
            scene, target_date.isoformat(), len(deduped), parquet_path,
        )
        return ExportResult(
            scene=scene,
            target_date=target_date,
            path=parquet_path,
            row_count=len(deduped),
            content_hash=content_hash,
            skipped_unchanged=False,
        )

    # 空集：不创建空文件
    if parquet_path.exists():
        logger.warning(
            "parquet_export: scene=%s date=%s 当日无数据但既有归档文件存在 "
            "（response_event append-only，不应发生）：%s",
            scene, target_date.isoformat(), parquet_path,
        )
    return ExportResult(
        scene=scene,
        target_date=target_date,
        path=None,
        row_count=0,
        content_hash="",
        skipped_unchanged=True,
    )


async def export_daily(
    db: AsyncSession,
    base_dir: Path,
    target_date: Optional[date] = None,
    *,
    scenes: Sequence[str] = SCENES,
) -> list[ExportResult]:
    """导出 target_date（默认昨日 UTC）的全场景增量.

    每场景一个 Parquet 文件，路径含日期 + 场景标记（验收 §1）。
    幂等：同 (date, scene) 多次执行产出相同文件（验收 §3）。

    返回顺序与 scenes 入参一致；空场景返回 path=None 的结果。
    """
    if target_date is None:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    results: list[ExportResult] = []
    for scene in scenes:
        result = await export_scene(db, base_dir, target_date, scene)
        results.append(result)
    return results


__all__ = [
    "SCENES",
    "PARQUET_SCHEMA",
    "ExportResult",
    "build_output_path",
    "export_scene",
    "export_daily",
]
