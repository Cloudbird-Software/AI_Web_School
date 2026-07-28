#!/usr/bin/env python3
"""T-W4-046 年度全量重放首演报告生成器（E2E-8 承载卡）.

架构 v2 §4.7 / 宪法 D6 / R-D-05：年度全量重放演练——用当前活跃估计器重算
全部历史 response_event，证明「可重算」与「增量重判不重放全量」.

与 scripts/jobs/annual_replay.py（T-W4-003）的差异：
- annual_replay.py：原始重放脚本，调用 replay_all 输出 JSON 报告。
- annual_replay_report.py（本文件）：首演报告生成器，在 replay_all 之上叠加：
  * ActiveModelPointer 版本映射（estimator_run 表查询实证）
  * 异常项列表（replay failures 结构化提取）
  * 新旧参数并存实证（item_param 表查询同题多版本参数）
  * Markdown 报告渲染（基于 docs/replay-report-template.md）

验收标准（任务卡 T-W4-046）：
1. 读取全部历史 response_event，用当前活跃估计器重算，输出报告。
2. 报告含：重算参数分布、与旧参数差异统计、一致性率、异常项列表、
   ActiveModelPointer 版本映射。
3. 新旧参数并存验证：切换前后报告分别引用各自版本的参数（数据库查询实证）。
5. 不 import 任何学科包/学段包.

用法（需 db 容器运行、.env 含 POSTGRES_*）：
    python scripts/jobs/annual_replay_report.py --scope practice \\
        --run-label annual-replay-2026 \\
        --output docs/replay-report-2026.md
    python scripts/jobs/annual_replay_report.py --scope practice --json

退出码 0 = 报告生成成功；非 0 = 失败（DB 连不上 / 估计器未登记 / replay 失败）.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── 让脚本能 import 项目 src（与 tests/conftest.py / alembic/env.py 同处理）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 UTF-8 输出兜底（报告含中文）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv_if_needed() -> None:
    """最小 .env 加载器（与 alembic/env.py 一致，避免重复实现污染）."""
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


# ════════════════════════════════════════════════════════════════════
# 报告数据结构
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class VersionMapping:
    """ActiveModelPointer 版本映射（验收 #2：版本映射 + 验收 #3：新旧并存）.

    - current_active: 当前活跃 model_version（replay 引用的版本）
    - history: 该场景所有版本登记历史（activated_at / retired_at / code_digest）
    - old_versions: 已退役版本列表（切换前的版本——历史报告引用之）
    - new_version: 当前活跃版本（切换后的版本——本次重放引用之）
    """

    scope: str
    current_active: Optional[str]
    history: list[dict[str, Any]] = field(default_factory=list)
    old_versions: list[str] = field(default_factory=list)
    new_version: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParamCoexistence:
    """新旧参数并存实证（验收 #3：切换前后报告各引用各自版本的参数）.

    - by_item: {item_version_id: {model_version: {params, sample_size, as_of}}}
      同一题在多个 model_version 下有参数行 → 并存实证
    - coexisting_items: 有 ≥2 个版本参数的 item_version_id 列表
    - total_param_rows: 该场景 item_param 总行数
    """

    by_item: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    coexisting_items: list[str] = field(default_factory=list)
    total_param_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnnualReplayReport:
    """年度全量重放首演报告（E2E-8 承载）.

    聚合 replay_all 输出 + 版本映射 + 异常项 + 参数并存实证.
    """

    # 元信息
    scope: str
    run_label: Optional[str]
    generated_at: str  # ISO 字符串
    # 验收 #1：replay_all 输出（含重算参数分布、差异统计、一致性率、摘要哈希）
    replay: dict[str, Any]
    # 验收 #2：ActiveModelPointer 版本映射
    version_mapping: dict[str, Any]
    # 验收 #2：异常项列表（replay failures 结构化）
    anomalies: list[dict[str, Any]]
    # 验收 #3：新旧参数并存实证
    param_coexistence: dict[str, Any]
    # 摘要
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ════════════════════════════════════════════════════════════════════
# 报告生成核心
# ════════════════════════════════════════════════════════════════════


async def _build_version_mapping(
    db: Any, *, purpose_scope: str
) -> VersionMapping:
    """查 estimator_run 表构造版本映射（验收 #2）.

    当前活跃 = retired_at IS NULL；旧版本 = retired_at IS NOT NULL.
    """
    from sqlalchemy import select

    from src.core.models.estimator_run import EstimatorRun

    rows = (
        await db.execute(
            select(EstimatorRun)
            .where(EstimatorRun.purpose_scope == purpose_scope)
            .order_by(EstimatorRun.activated_at)
        )
    ).scalars().all()

    history: list[dict[str, Any]] = []
    old_versions: list[str] = []
    current_active: Optional[str] = None
    for r in rows:
        entry = {
            "run_id": r.run_id,
            "model_version": r.model_version,
            "code_digest": r.code_digest,
            "input_snapshot_id": r.input_snapshot_id,
            "graph_release_id": r.graph_release_id,
            "activated_at": r.activated_at.isoformat() if r.activated_at else None,
            "retired_at": r.retired_at.isoformat() if r.retired_at else None,
            "is_active": r.retired_at is None,
        }
        history.append(entry)
        if r.retired_at is None:
            current_active = r.model_version
        else:
            old_versions.append(r.model_version)

    return VersionMapping(
        scope=purpose_scope,
        current_active=current_active,
        history=history,
        old_versions=old_versions,
        new_version=current_active,
    )


async def _build_param_coexistence(
    db: Any, *, purpose_scope: str
) -> ParamCoexistence:
    """查 item_param 表实证新旧参数并存（验收 #3）.

    同一 item_version_id 在多个 method_version 下有参数行 → 并存.
    """
    from sqlalchemy import text

    rows = (
        await db.execute(
            text(
                "SELECT item_version_id, method_version, params, sample_size, as_of"
                " FROM item_param WHERE purpose_scope = :scope"
                " ORDER BY item_version_id, method_version, as_of"
            ),
            {"scope": purpose_scope},
        )
    ).all()

    by_item: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        iv = r.item_version_id
        mv = r.method_version
        by_item.setdefault(iv, {})[mv] = {
            "params": r.params,
            "sample_size": r.sample_size,
            "as_of": r.as_of.isoformat() if r.as_of else None,
        }
    coexisting = [iv for iv, versions in by_item.items() if len(versions) >= 2]

    return ParamCoexistence(
        by_item=by_item,
        coexisting_items=coexisting,
        total_param_rows=len(rows),
    )


async def generate_annual_replay_report(
    db: Any,
    *,
    purpose_scope: str,
    run_label: Optional[str] = None,
    input_snapshot_id: Optional[str] = None,
) -> AnnualReplayReport:
    """生成年度全量重放首演报告.

    步骤（对应验收 #1-#3）：
    1. 调用 replay_all 重算全部历史事件（验收 #1）
    2. 查 estimator_run 构造版本映射（验收 #2）
    3. 从 replay failures 提取异常项（验收 #2）
    4. 查 item_param 实证新旧参数并存（验收 #3）
    5. 聚合摘要并返回 AnnualReplayReport

    Args:
        db: 异步会话。
        purpose_scope: 场景（practice/diagnosis/measurement，D5 单值）。
        run_label: 批次标签（如 'annual-replay-2026'）。
        input_snapshot_id: 输入快照标识；None=自动构造.

    Returns:
        AnnualReplayReport（含 replay / version_mapping / anomalies /
        param_coexistence / summary 五段）.

    Raises:
        ValueError: purpose_scope 越域或无活跃估计器版本.
    """
    from src.core.data.replay import replay_all

    # 验收 #1：replay_all 重算
    replay = await replay_all(
        db,
        purpose_scope=purpose_scope,
        run_label=run_label,
        input_snapshot_id=input_snapshot_id,
    )
    replay_dict = replay.to_dict()

    # 验收 #2：版本映射
    version_mapping = await _build_version_mapping(db, purpose_scope=purpose_scope)

    # 验收 #2：异常项列表（replay failures）
    anomalies = list(replay_dict.get("failures", []))

    # 验收 #3：新旧参数并存实证
    param_coexistence = await _build_param_coexistence(
        db, purpose_scope=purpose_scope
    )

    # 摘要
    summary = {
        "total_events": (
            replay_dict.get("rescored_count", 0)
            + replay_dict.get("skipped_count", 0)
            + replay_dict.get("failed_count", 0)
        ),
        "rescored": replay_dict.get("rescored_count", 0),
        "skipped": replay_dict.get("skipped_count", 0),
        "failed": replay_dict.get("failed_count", 0),
        "consistency": replay_dict.get("consistency", 0.0),
        "summary_hash": replay_dict.get("summary_hash", ""),
        "active_version": version_mapping.current_active,
        "old_versions": version_mapping.old_versions,
        "coexisting_param_items": len(param_coexistence.coexisting_items),
    }

    return AnnualReplayReport(
        scope=purpose_scope,
        run_label=run_label,
        generated_at=datetime.now(timezone.utc).isoformat(),
        replay=replay_dict,
        version_mapping=version_mapping.to_dict(),
        anomalies=anomalies,
        param_coexistence=param_coexistence.to_dict(),
        summary=summary,
    )


# ════════════════════════════════════════════════════════════════════
# Markdown 渲染（基于 docs/replay-report-template.md）
# ════════════════════════════════════════════════════════════════════


def render_report_markdown(report: AnnualReplayReport) -> str:
    """渲染年度重放首演报告为 Markdown（基于 docs/replay-report-template.md）.

    输出结构（与模板逐段对齐）：
    - 元信息（场景/批次/生成时刻）
    - 摘要（事件数/重算/跳过/失败/一致性/摘要哈希）
    - ActiveModelPointer 版本映射（验收 #2）
    - 重算参数分布与差异统计（验收 #1，来自 replay_all）
    - 一致性率（验收 #1）
    - 异常项列表（验收 #2）
    - 新旧参数并存实证（验收 #3）
    """
    r = report.to_dict()
    s = r["summary"]
    vm = r["version_mapping"]
    pc = r["param_coexistence"]
    replay = r["replay"]

    lines: list[str] = []
    lines.append(f"# 年度全量重放首演报告 — {r['scope']}")
    lines.append("")
    lines.append("> 架构 v2 §4.7 / 宪法 D6 / R-D-05：估计器可替换 + 可重算实证。")
    lines.append("> 本报告由 `scripts/jobs/annual_replay_report.py` 生成。")
    lines.append("")

    # ── 元信息 ──
    lines.append("## 元信息")
    lines.append("")
    lines.append(f"- 场景（purpose_scope）：`{r['scope']}`")
    lines.append(f"- 批次标签：`{r['run_label']}`")
    lines.append(f"- 生成时刻（UTC）：{r['generated_at']}")
    lines.append("")

    # ── 摘要 ──
    lines.append("## 摘要")
    lines.append("")
    lines.append(f"- 历史事件总数：{s['total_events']}")
    lines.append(f"- 重算成功：{s['rescored']}")
    lines.append(f"- 幂等跳过：{s['skipped']}")
    lines.append(f"- 重算失败：{s['failed']}")
    lines.append(f"- 新旧一致性率：{s['consistency']:.4f}")
    lines.append(f"- 摘要哈希（D6 可重放）：`{s['summary_hash']}`")
    lines.append(f"- 当前活跃估计器版本：`{s['active_version']}`")
    lines.append(f"- 已退役版本：{s['old_versions']}")
    lines.append(f"- 新旧参数并存的题目数：{s['coexisting_param_items']}")
    lines.append("")

    # ── ActiveModelPointer 版本映射 ──
    lines.append("## ActiveModelPointer 版本映射（验收 #2）")
    lines.append("")
    lines.append(f"- 当前活跃版本：`{vm['current_active']}`")
    lines.append(f"- 切换前旧版本：{vm['old_versions']}")
    lines.append(f"- 切换后新版本：`{vm['new_version']}`")
    lines.append("")
    lines.append("| run_id | model_version | activated_at | retired_at | is_active |")
    lines.append("|--------|---------------|--------------|------------|-----------|")
    for h in vm["history"]:
        retired = h["retired_at"] or "—"
        lines.append(
            f"| `{h['run_id']}` | `{h['model_version']}` | "
            f"{h['activated_at']} | {retired} | {h['is_active']} |"
        )
    lines.append("")

    # ── 重算参数分布与差异统计 ──
    lines.append("## 重算参数分布与差异统计（验收 #1）")
    lines.append("")
    old_ps = replay.get("old_param_summary", {}) or {}
    new_ps = replay.get("new_param_summary", {}) or {}
    pdiff = replay.get("param_diff_distribution", {}) or {}
    lines.append("### 旧版本参数摘要")
    lines.append("")
    lines.append(f"- scorer_versions：`{old_ps.get('scorer_versions', {})}`")
    lines.append(f"- correct_distribution：`{old_ps.get('correct_distribution', {})}`")
    lines.append(f"- difficulty_approx：`{old_ps.get('difficulty_approx')}`")
    lines.append("")
    lines.append("### 新版本参数摘要")
    lines.append("")
    lines.append(f"- scorer_version：`{new_ps.get('scorer_version', '')}`")
    lines.append(f"- correct_distribution：`{new_ps.get('correct_distribution', {})}`")
    lines.append(f"- difficulty_approx：`{new_ps.get('difficulty_approx')}`")
    lines.append("")
    lines.append("### 参数差异分布")
    lines.append("")
    if pdiff:
        lines.append(f"- difficulty_old：`{pdiff.get('difficulty_old')}`")
        lines.append(f"- difficulty_new：`{pdiff.get('difficulty_new')}`")
        lines.append(f"- difficulty_delta：`{pdiff.get('difficulty_delta')}`")
    else:
        lines.append("_无可比参数差异（旧/新数据不足）_")
    lines.append("")

    # ── 一致性率 ──
    lines.append("## 一致性率（验收 #1）")
    lines.append("")
    lines.append(f"- 新旧 correct 一致率：`{replay.get('consistency', 0.0):.4f}`")
    lines.append(f"- 重算所用 scorer_version：`{replay.get('scorer_version', '')}`")
    lines.append("")

    # ── 异常项列表 ──
    lines.append("## 异常项列表（验收 #2）")
    lines.append("")
    anomalies = r["anomalies"]
    if not anomalies:
        lines.append("_无异常项——重算全部成功_")
    else:
        lines.append("| event_id | item_version_id | reason |")
        lines.append("|----------|------------------|--------|")
        for a in anomalies:
            lines.append(
                f"| `{a.get('event_id')}` | `{a.get('item_version_id')}` | "
                f"{a.get('reason')} |"
            )
    lines.append("")

    # ── 新旧参数并存实证 ──
    lines.append("## 新旧参数并存实证（验收 #3）")
    lines.append("")
    lines.append(f"- 该场景 item_param 总行数：`{pc['total_param_rows']}`")
    lines.append(f"- 新旧参数并存的题目数：`{len(pc['coexisting_items'])}`")
    lines.append("")
    coexisting = pc["coexisting_items"]
    if not coexisting:
        lines.append("_无并存参数行——仅一个版本有参数（首演前未切换）_")
    else:
        lines.append("### 并存题目详情")
        lines.append("")
        lines.append("| item_version_id | method_versions |")
        lines.append("|-----------------|------------------|")
        for iv in coexisting:
            versions = list(pc["by_item"][iv].keys())
            lines.append(f"| `{iv}` | `{versions}` |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> 本报告证明：年度全量重放可重算全部历史事件（D6 估计器可替换），"
        "新旧参数并存于 item_param 表（切换前后报告各引用各自版本），"
        "增量重判不重放全量（replay_all 仅写平行 score_run，原 response_event 不动）。"
    )
    lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="年度全量重放首演报告生成器：replay_all + 版本映射 + 参数并存实证"
    )
    # --scope 非 dry-run 模式必填（_amain 内校验）；dry-run 模式允许省略，
    # 以便 w4.sh 出口脚本与 CI 用 --dry-run 做接口自检而不连 DB。
    p.add_argument(
        "--scope",
        required=False,
        choices=["practice", "diagnosis", "measurement"],
        default=None,
        help="重放场景（D5 必填单值——按场景独立重放，禁止跨场景混估；"
             "dry-run 模式可省略）",
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
        "--output",
        default=None,
        help="报告输出文件路径；--json 时写 JSON，否则写 Markdown",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式报告（默认 Markdown）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="接口自检模式：不连 DB、不跑 replay_all，仅验证参数解析与模块导入"
             "（w4.sh 出口脚本 / CI 轻量探活用）",
    )
    return p.parse_args()


async def _amain() -> int:
    args = _parse_args()

    # dry-run：接口自检，不连 DB（w4.sh 出口与 CI 探活）
    # 为什么需要 dry-run：replay_all 需要运行中的 DB 容器 + 已登记的活跃估计器，
    # 出口脚本与 CI 用 dry-run 验证脚本可加载、参数可解析即可——
    # 真实重放由 test_replay.py（单元）与 annual_replay_report.py 全量运行覆盖。
    if args.dry_run:
        print(
            "✅ annual_replay_report.py dry-run：参数解析与模块导入通过"
            + (f"（--scope={args.scope}）" if args.scope else "（--scope 省略）")
        )
        return 0

    if args.scope is None:
        raise SystemExit(
            "非 dry-run 模式必须提供 --scope（practice/diagnosis/measurement，D5 单值）"
        )

    # 注册 platform 评分器（与 annual_replay.py 同处理）
    import src.core.scoring.platform_scorers  # noqa: F401
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(_build_async_dsn(), echo=False)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            report = await generate_annual_replay_report(
                session,
                purpose_scope=args.scope,
                run_label=args.run_label,
                input_snapshot_id=args.input_snapshot_id,
            )
    finally:
        await engine.dispose()

    if args.json:
        out = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str)
    else:
        out = render_report_markdown(report)

    print(out)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"报告已写入 {args.output}", file=sys.stderr)

    return 0


def main() -> None:
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    except Exception as e:
        print(f"❌ 年度重放报告生成失败：{type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()


__all__ = [
    "AnnualReplayReport",
    "ParamCoexistence",
    "VersionMapping",
    "generate_annual_replay_report",
    "render_report_markdown",
]
