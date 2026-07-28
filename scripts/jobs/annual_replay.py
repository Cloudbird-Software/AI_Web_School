#!/usr/bin/env python3
"""T-W4-003 年度全量重放演练脚本（架构 v2 §4.7 / 宪法 D6 / R-D-05）.

制度化「年度全量重放」：读取指定场景全部历史 response_event，用当前活跃
估计器重算，输出新旧对比报告（参数差异分布、新旧一致性率、摘要哈希）。

为什么是「演练」而非生产自动触发：增量重判防成本爆炸（评审报告 D4）；
全量重放每年一次，证明「可重算」——D6 估计器可替换的实证。
非目标（任务卡 non_goals）：实时重判 / 生产环境自动触发 / 回滚历史分数 /
异步流处理。本脚本是离线、人工触发的演练。

可重放性（验收 §3）：同代码版本 + 同数据快照输出一致——
本脚本输出 summary_hash（SHA256），重跑应得相同哈希（除非数据变化）。

用法（需 db 容器运行、.env 含 POSTGRES_*）：
    python scripts/jobs/annual_replay.py --scope practice [--dry-run]
    python scripts/jobs/annual_replay.py --scope practice --scorer-version ctt-v2 \\
        --run-label annual-replay-2026
退出码 0 = 演练成功（含或无写入均正常）；非 0 = 失败（DB 连不上 / 估计器未登记）。

--dry-run：只取数 + 报告将要处理的事件数 + 当前活跃估计器版本，不写 score_run。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── 让脚本能 import 项目 src（与 tests/conftest.py / alembic/env.py 同处理）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 UTF-8 输出兜底（报告含中文）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv_if_needed() -> None:
    """最小 .env 加载器（与 alembic/env.py 一致，避免重复实现污染）.

    为什么覆盖而非 setdefault：多 worktree 时系统环境变量可能被其他 worktree
    污染，worktree 本地 .env 优先（与 tests/conftest.py 一致）。
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_dotenv_if_needed()


def _build_async_dsn() -> str:
    """拼装 asyncpg DSN（与 tests/conftest.py 一致）."""
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供。"
        )
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="年度全量重放演练：用当前活跃估计器重算历史事件，输出新旧对比报告"
    )
    p.add_argument(
        "--scope",
        required=True,
        choices=["practice", "diagnosis", "measurement"],
        help="重放场景（D5 必填单值——按场景独立重放，禁止跨场景混估）",
    )
    p.add_argument(
        "--scorer-version",
        default=None,
        help="重判用的评分器版本标签；None=取当前活跃估计器的 model_version",
    )
    p.add_argument(
        "--run-label",
        default=None,
        help="批次标签（如 annual-replay-2026）；用于幂等保护与报告分组",
    )
    p.add_argument(
        "--input-snapshot-id",
        default=None,
        help="输入数据快照标识（D6 可重放——同代码+同快照必同输出）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只取数 + 报告事件数 + 当前活跃估计器版本，不写 score_run",
    )
    p.add_argument(
        "--output",
        default=None,
        help="报告输出 JSON 文件路径；None=只打印到 stdout",
    )
    return p.parse_args()


async def _dry_run_report(scope: str, scorer_version: str | None) -> dict:
    """--dry-run：取数 + 报告事件数与活跃估计器版本，不写库."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.core.data.active_model_pointer import ActiveModelPointer

    engine = create_async_engine(_build_async_dsn(), echo=False)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM response_event WHERE scene = :scope"),
                    {"scope": scope},
                )
            ).scalar()
            ptr = ActiveModelPointer(session)
            active = await ptr.get_active(scope)
            return {
                "mode": "dry-run",
                "scope": scope,
                "event_count": int(count or 0),
                "active_estimator_version": (
                    active.model_version if active is not None else None
                ),
                "scorer_version_arg": scorer_version,
                "note": "dry-run 不写 score_run；正式运行去掉 --dry-run",
            }
    finally:
        await engine.dispose()


async def _full_replay(args: argparse.Namespace) -> dict:
    """正式重放：调用 replay_all 并返回完整报告."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.core.data.replay import replay_all

    engine = create_async_engine(_build_async_dsn(), echo=False)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            report = await replay_all(
                session,
                purpose_scope=args.scope,
                scorer_version=args.scorer_version,
                run_label=args.run_label,
                input_snapshot_id=args.input_snapshot_id,
            )
            return report.to_dict()
    finally:
        await engine.dispose()


async def _amain() -> int:
    args = _parse_args()

    # 注册 platform 评分器（run_scorer 通过 scorer_id 从注册表取实现）
    # 为什么显式 import：年度重放脚本独立运行，没有 FastAPI app 启动时的自动加载；
    # platform 评分器是核心域的兜底桶，import 即注册。
    import src.core.scoring.platform_scorers  # noqa: F401

    if args.dry_run:
        report = await _dry_run_report(args.scope, args.scorer_version)
    else:
        report = await _full_replay(args)

    # 输出报告
    report_json = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(report_json)
    if args.output:
        Path(args.output).write_text(report_json, encoding="utf-8")
        print(f"报告已写入 {args.output}", file=sys.stderr)

    # 退出码：dry-run 永远 0；正式重放失败数 > 0 时仍 0（failures 已在报告中，
    # 不阻断演练——这是「可重算」的实证，部分失败由人工后续跟进）
    return 0


def main() -> None:
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    except Exception as e:
        print(f"❌ 年度重放演练失败：{type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
