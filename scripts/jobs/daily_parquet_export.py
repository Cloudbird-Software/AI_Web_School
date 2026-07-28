#!/usr/bin/env python3
"""T-W4-006 每日增量 Parquet 归档作业（CLI）.

定时执行（crontab / k8s CronJob）：导出前一日 UTC 的 response_event 增量
到对象存储挂载点。幂等可重跑——同日期多次执行产出相同文件（manifest 哈希比对）。

用法：
    python scripts/jobs/daily_parquet_export.py                        # 导出昨日 UTC
    python scripts/jobs/daily_parquet_export.py --date 2026-07-27      # 指定日期
    python scripts/jobs/daily_parquet_export.py --base-dir /mnt/archive/events
    python scripts/jobs/daily_parquet_export.py --scenes practice diagnosis

环境变量（与 alembic env.py / tests/conftest.py 一致）：
    POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST / POSTGRES_PORT / POSTGRES_DB
    ARCHIVE_BASE_DIR（可被 --base-dir 覆盖；默认 ./var/archive/events）

退出码：0 = 全部场景导出成功（含 skipped_unchanged）；非 0 = 任一场景失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── 让脚本能 import 项目 src（与 demo-w3-business.py 同处理）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 UTF-8 输出兜底（manifest 含中文场景标签）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from src.core.data.parquet_export import SCENES, export_daily  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# 环境：.env 加载 + 异步引擎
# ────────────────────────────────────────────────────────────────────


def _load_dotenv() -> None:
    """从项目根 .env 加载配置（与 conftest.py 一致，覆盖系统环境变量）."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _build_async_dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供"
        )
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="response_event 每日增量 Parquet 归档作业（幂等）"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="目标日期 YYYY-MM-DD（默认昨日 UTC）",
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="归档根目录（默认 $ARCHIVE_BASE_DIR 或 ./var/archive/events）",
    )
    parser.add_argument(
        "--scenes",
        nargs="+",
        default=list(SCENES),
        choices=list(SCENES),
        help="导出场景子集（默认全部三场景）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="报告 JSON 输出路径（不指定则仅打印 stdout）",
    )
    return parser.parse_args()


def _resolve_target_date(arg: str | None) -> date:
    if arg is None:
        return (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return datetime.strptime(arg, "%Y-%m-%d").date()


def _resolve_base_dir(arg: str | None) -> Path:
    if arg is not None:
        return Path(arg)
    env_val = os.environ.get("ARCHIVE_BASE_DIR")
    if env_val:
        return Path(env_val)
    # 默认项目内 var/archive/events（开发/测试用；生产用 --base-dir 或 ARCHIVE_BASE_DIR）
    return PROJECT_ROOT / "var" / "archive" / "events"


async def _amain() -> int:
    args = _parse_args()
    _load_dotenv()
    target_date = _resolve_target_date(args.date)
    base_dir = _resolve_base_dir(args.base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(_build_async_dsn(), echo=False, pool_pre_ping=True)
    try:
        async with AsyncSession(bind=engine, expire_on_commit=False) as session:
            results = await export_daily(
                session,
                base_dir,
                target_date,
                scenes=tuple(args.scenes),
            )
    finally:
        await engine.dispose()

    report = {
        "target_date": target_date.isoformat(),
        "base_dir": str(base_dir),
        "scenes": [r.to_dict() for r in results],
        "summary": {
            "total_rows": sum(r.row_count for r in results),
            "skipped_unchanged_count": sum(1 for r in results if r.skipped_unchanged),
            "scene_count": len(results),
        },
    }
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    print(report_json)
    if args.output:
        Path(args.output).write_text(report_json, encoding="utf-8")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
