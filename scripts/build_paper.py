"""脚本：组卷 + 渲染 + PDF 导出 + 可选落库（Issue #29 Paper generation & rendering pipeline）.

用法示例（Issue #29 验收 #1 - 生成一份 10 题数学周练卷）：
    python scripts/build_paper.py \\
        --subject subject-math --gradeband M \\
        --kp-snapshot math-week30 --kp math.arithmetic.addition_within_20 \\
        --kp math.arithmetic.subtraction_within_20 \\
        --num-items 5 --interaction single_choice=3 --interaction numeric_blank=2 \\
        --title "Weekly Practice Math W30" --seed 20260730 \\
        --source specs/examples \\
        --output /tmp/paper_out \\
        --no-pdf  # 本地无 Edge/Playwright 时跳过 PDF

用法示例（Issue #29 验收 #2 - 生成 50 份相同题序的批量卷，seed=1）：
    python scripts/build_paper.py \\
        --subject subject-math --gradeband L --num-items 10 \\
        --interaction single_choice=6 --interaction numeric_blank=4 \\
        --kp-snapshot kp-snap-l1 --kp math.nal.even_odd --kp math.nal.place_value \\
        --title "Batch Math L1" --seed 1 \\
        --source specs/examples --output /tmp/batch_paper --copies 50 --no-pdf

验收标准（与 Issue #29 对齐）：
1. build_paper.py 能从 `--source` 读实例池（JSON/CSV，复用 adapters），产出 1 份或
   N 份卷，写出 paper + paper_item JSON 行，可选导出 PDF。
2. 每卷 paper_row 结构与 Paper ORM 对齐；paper_item_row 结构与 PaperItem ORM 对齐。
3. 确定性：同 source + 同 seed → 同 item_version_id 集合（用哈希记录）。
4. 可选：提供 --dry-run 只选题不落盘。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# 确保能 import project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.render.weekly_batch import (  # noqa: E402
    WeeklyConstraints,
    WeeklyScope,
    run as run_weekly_batch,
)
from src.registry.importer import run_import  # noqa: E402
from src.registry.adapters import list_adapters  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# CLI 构建
# ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_paper.py",
        description="组卷 + 渲染 + PDF 导出（Issue #29 Paper generation pipeline）",
    )
    p.add_argument(
        "--subject", required=True,
        choices=["subject-math", "subject-chinese", "subject-english"],
        help="学科包 id",
    )
    p.add_argument(
        "--gradeband", required=True, choices=["L", "M", "H"],
        help="学段",
    )
    p.add_argument(
        "--kp-snapshot", required=True, type=str,
        help="知识点快照引用 id（写入 paper.kp_snapshot_ref）",
    )
    p.add_argument(
        "--kp", action="append", default=[], type=str, dest="kp_list",
        help="知识点 code（可重复，用于筛选/审计，目前 paper pipeline 不做强过滤）",
    )
    p.add_argument("--num-items", required=True, type=int, help="总题量")
    p.add_argument(
        "--interaction", action="append", required=True, dest="interactions",
        help="交互分布，格式 interaction_id=N（可重复），例如 --interaction single_choice=5",
    )
    p.add_argument("--seed", type=int, default=20260730, help="确定性种子")
    p.add_argument("--title", type=str, default="Weekly Practice", help="卷名")
    p.add_argument(
        "--source", required=True, type=Path,
        help="item_version 源：目录/文件（JSON/CSV/RACE/CMRC/ASSISTments），"
             "会走 run_import 的 adapter 拉成已校验的实例池",
    )
    p.add_argument("--source-adapter", type=str, default="json",
                   help="源适配器名，默认 json（可用: %s）" % ", ".join(list_adapters()))
    p.add_argument(
        "--output", required=True, type=Path,
        help="输出目录（PDF + 卷元 data JSON）",
    )
    p.add_argument("--copies", type=int, default=1, help="批量生成 N 份（共享题序）")
    p.add_argument("--no-pdf", action="store_true", help="只选题+写元数据，不导出 PDF")
    p.add_argument("--dry-run", action="store_true", help="仅选题，不写文件，不导出 PDF")
    p.add_argument("--created-by", type=str, default="build_paper.py",
                   help="paper.created_by 字段")
    return p


def _parse_interactions(pairs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in pairs or []:
        if "=" not in p:
            raise argparse.ArgumentTypeError(f"--interaction 需格式 id=N，实际 {p!r}")
        k, v = p.split("=", 1)
        try:
            n = int(v)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"--interaction {p} 数量非整数: {e}") from e
        if n <= 0:
            raise argparse.ArgumentTypeError(f"--interaction {p} 数量需 >= 1")
        out[k.strip()] = out.get(k.strip(), 0) + n
    return out


# ────────────────────────────────────────────────────────────────────
# 从 source 拉实例池
# ────────────────────────────────────────────────────────────────────


async def _load_item_pool(
    source: Path,
    adapter: str = "json",
    tmp_report_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """通过 run_import（dry-run）拉取已通过 schema+pydantic+registry 校验的 item_version 池.

    返回：validation_passed 的 item_version pydantic 对象 list；序列化到 dict 便于 weekly_batch 消费。
    """
    import tempfile
    rdir = tmp_report_dir or Path(tempfile.mkdtemp(prefix="build_paper_report_"))
    rpt = await run_import(
        source=source, adapter=adapter, mode="dry-run", report_dir=rdir,
    )
    if rpt.total_seen == 0 or rpt.validation_passed == 0:
        raise RuntimeError(
            f"实例池为空：源 {source} 共 {rpt.total_seen} 条，通过校验 {rpt.validation_passed} 条；"
            f"errors: {rpt.adapter_errors[:3]}; validation: {rpt.validation_errors[:3]}"
        )
    pool: list[dict[str, Any]] = []
    for ok in (rpt.validation_passed_items or []):
        if hasattr(ok, "model_dump"):
            pool.append(ok.model_dump(mode="json"))
        elif isinstance(ok, dict):
            pool.append(dict(ok))
    return pool


# ────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────


def _determinism_fingerprint(rows: list[dict[str, Any]]) -> str:
    """按 paper_item.item_version_id 顺序生成哈希，用于确定性验证."""
    ids = [str(r.get("item_version_id") or "") for r in rows]
    return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]


async def async_main(args: argparse.Namespace) -> int:
    interactions = _parse_interactions(args.interactions)
    sum_ia = sum(interactions.values())
    if sum_ia != args.num_items:
        print(
            f"[ERR] interaction 合计 {sum_ia} ≠ --num-items {args.num_items}",
            file=sys.stderr,
        )
        return 2

    scope = WeeklyScope(
        subject_pack_id=args.subject,
        gradeband=args.gradeband,
        kp_codes=tuple(args.kp_list) or (),
        kp_snapshot_ref=args.kp_snapshot,
    )
    constraints = WeeklyConstraints(
        num_items=args.num_items,
        interaction_distribution=interactions,
        seed=args.seed,
        paper_title=args.title,
    )
    outdir = Path(args.output)

    # 1) 实例池
    print(f"[INFO] 加载实例池 {args.source}（adapter={args.source_adapter}）...")
    pool = await _load_item_pool(args.source, adapter=args.source_adapter)
    print(f"[INFO] 实例池：{len(pool)} 条校验通过")

    # 2) dry-run 只选题，不写文件
    if args.dry_run:
        from src.core.render.weekly_batch import _select_items
        selected = _select_items(pool, constraints)
        fp = _determinism_fingerprint([{"item_version_id": s.get("item_version_id")}
                                       for s in selected])
        print(f"[DRY-RUN] 选题 {len(selected)} 条，fingerprint={fp}")
        print("[DRY-RUN] item_version_ids:", [s.get("item_version_id") for s in selected])
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    pdf_backend: str = "edge" if not args.no_pdf else "noop"

    # 3) 生成 copies 份
    results: list[dict[str, Any]] = []
    shared_item_fp: str | None = None
    for copy_idx in range(1, args.copies + 1):
        # copies > 1 时用相同 seed → 同题序（copies 仅影响 paper_id/paper_code）
        sub_dir = outdir if args.copies == 1 else (outdir / f"copy_{copy_idx:03d}")
        sub_dir.mkdir(parents=True, exist_ok=True)
        try:
            r = run_weekly_batch(
                scope=scope,
                constraints=constraints,
                output_dir=sub_dir,
                item_version_pool=pool,
                pdf_backend=pdf_backend,  # type: ignore[arg-type]
                created_by=args.created_by,
            )
        except Exception as e:
            # PDF 后端不可用 → 写 HTML 留痕并继续
            if args.no_pdf:
                raise
            print(f"[WARN] PDF 导出失败（copy={copy_idx}）：{e}；继续写元数据")
            # noop fallback
            r = run_weekly_batch(
                scope=scope, constraints=constraints, output_dir=sub_dir,
                item_version_pool=pool, pdf_backend="noop",  # type: ignore[arg-type]
                created_by=args.created_by,
            )
        fp = _determinism_fingerprint(r.paper_item_rows)
        if shared_item_fp is None:
            shared_item_fp = fp
        elif fp != shared_item_fp and args.copies > 1:
            print(
                f"[WARN] 确定性校验失败：copy_001 fp={shared_item_fp} ≠ "
                f"copy_{copy_idx:03d} fp={fp}",
                file=sys.stderr,
            )
        # 写元数据 JSON
        meta = {
            "paper": r.paper_row,
            "paper_items": r.paper_item_rows,
            "paper_pdf_path": str(r.paper_pdf_path),
            "solution_pdf_path": str(r.solution_pdf_path),
            "fingerprint": fp,
        }
        meta_path = sub_dir / f"{r.paper_id}-meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        results.append({
            "paper_id": r.paper_id,
            "paper_code": r.paper_code,
            "meta": str(meta_path),
            "paper_pdf": str(r.paper_pdf_path) if (not args.no_pdf and r.paper_pdf_path.exists()) else None,
            "fingerprint": fp,
        })
        # copies=1 时在 stdout 摘要；copies>1 时静默，循环最后统一打印

    # 4) 总览
    summary_path = outdir / "summary.json"
    summary_path.write_text(json.dumps({
        "copies": len(results),
        "subject_pack_id": args.subject,
        "gradeband": args.gradeband,
        "kp_snapshot": args.kp_snapshot,
        "num_items": args.num_items,
        "interactions": interactions,
        "seed": args.seed,
        "shared_fingerprint": shared_item_fp,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] 已生成 {len(results)} 份卷 → {outdir}")
    print(f"[OK] 总览：{summary_path}")
    if shared_item_fp:
        print(f"[OK] 确定性 fingerprint（{args.copies} 份共享）：{shared_item_fp}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except Exception as e:  # pragma: no cover - 顶层错误提示
        print(f"[FATAL] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
