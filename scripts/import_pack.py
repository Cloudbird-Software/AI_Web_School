#!/usr/bin/env python3
"""CLI: 基于适配器的 item_version 导入管道（Issue #26 / W1-1）.

用法：
  # Dry-run（校验但不落库，默认模式）
  python scripts/import_pack.py --source specs/examples/math_item_example.json --adapter json

  # 查看所有适配器
  python scripts/import_pack.py --list-adapters

  # Commit（写入 DB；需要 POSTGRES_* 环境变量或 .env）
  python scripts/import_pack.py --source specs/examples/ --adapter json --commit

  # CSV 导入
  python scripts/import_pack.py --source my_pack.csv --adapter csv --dry-run

退出码：
  0 — 无致命错误（validation_errors 非 0 也不视为致命——会在报告中列出）
  1 — 适配器未找到 / 源路径不存在 等致命参数错误
  2 — 系统错误（依赖缺失等）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make cwd/project importable 无论从哪调用
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="import_pack.py",
        description="基于适配器的 item_version 导入管道（W1-1）："
                    "校验 + 幂等写入 + 生成导入报告 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", type=str, default=None,
                   help="源文件或目录路径（JSON adapter 支持目录递归；CSV adapter 支持单文件）。")
    p.add_argument("--adapter", type=str, default="json",
                   help="适配器名：json | csv | ...（默认 json；--list-adapters 查看全部）。")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run",
                      default="dry-run",
                      help="仅校验，不写入 DB（默认）。")
    mode.add_argument("--commit", dest="mode", action="store_const", const="commit",
                      help="校验通过后写入 DB（使用 DEFERRABLE 事务保证 current_version_id 环外键一致）。")
    p.add_argument("--report-dir", type=str, default=None,
                   help="报告输出目录（默认 <project>/out/import_reports）。")
    p.add_argument("--list-adapters", action="store_true",
                   help="打印已注册的适配器名并退出。")
    p.add_argument("--no-progress", action="store_true",
                   help="不打印每处理一条的进度（非 TTY 环境默认关闭）。")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="仅打印报告文件路径与致命错误。")
    return p


def _on_progress_factory(args):
    """根据 CLI 参数决定打印进度回调."""
    if args.quiet or args.no_progress:
        return None
    # 非 TTY 也不打印，避免日志污染
    if not sys.stderr.isatty():
        return None

    def _cb(count, total_seen, pydantic_obj, err):
        if err:
            prefix = f"[{count}] ❌ 校验失败"
            tail = f"：{err[:120]}"
        else:
            prefix = f"[{count}] ✓"
            obj = pydantic_obj
            tail = (
                f"  iv_id={obj.item_version_id[:12]}…"
                f"  item_id={obj.item_id}"
                f"  interaction={obj.interaction_ref.interaction_id}"
                f"  scorer={obj.scoring_ref.scorer_id}"
            )
        print(prefix + tail, file=sys.stderr)
    return _cb


def _print_summary(report, args):
    if args.quiet:
        print(report.to_dict().get("report_file", "<report not written>"))
        return

    d = report.to_dict()
    mode_label = "DRY-RUN" if report.mode == "dry-run" else "COMMIT"
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  📋 导入报告 [{mode_label}]  {report.timestamp}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Source   : {report.source}", file=sys.stderr)
    print(f"  Adapter  : {report.adapter}", file=sys.stderr)
    print(f"  Duration : {report.duration_ms} ms", file=sys.stderr)
    print(f"  Report   : {d.get('report_file', '(n/a)')}", file=sys.stderr)
    print(f"  ---", file=sys.stderr)
    print(f"  适配器产出 (total_seen)           : {report.total_seen}", file=sys.stderr)
    print(f"  ✅ 校验通过                        : {report.validation_passed}", file=sys.stderr)
    print(f"  ❌ 校验失败                        : {report.validation_failed}", file=sys.stderr)
    if report.mode == "commit":
        print(f"  --- DB 操作 ---", file=sys.stderr)
        print(f"  新增 item 行                     : {report.db_created_item}", file=sys.stderr)
        print(f"  新增 item_version 行              : {report.db_created_iv}", file=sys.stderr)
        print(f"  更新 item.current_version_id     : {report.db_updated_current}", file=sys.stderr)
        print(f"  ⏭️  幂等跳过（重复 iv_id）          : {report.db_skipped_duplicate_iv}", file=sys.stderr)
        print(f"  ⏭️  幂等跳过（item 已同版本）       : {report.db_skipped_duplicate_item}", file=sys.stderr)
        print(f"  💥 DB 错误                        : {report.db_error}", file=sys.stderr)
    print(f"  适配器错误                        : {len(report.adapter_errors)}", file=sys.stderr)
    print(f"  Warnings                          : {len(report.warnings)}", file=sys.stderr)
    # 打印前 3 条 validation_errors（如果有）
    if report.validation_errors:
        print(f"  --- 前 3 条校验错误 ---", file=sys.stderr)
        for i, e in enumerate(report.validation_errors[:3], 1):
            src = e.get("source", "?")
            line = e.get("line", 0)
            msg = (e.get("error") or "")[:200]
            print(f"    {i}. {src}:{line} — {msg}", file=sys.stderr)
    if report.adapter_errors:
        print(f"  --- 前 3 条适配器错误 ---", file=sys.stderr)
        for i, e in enumerate(report.adapter_errors[:3], 1):
            src = e.get("source", "?")
            msg = (e.get("message") or "")[:200]
            print(f"    {i}. {src}:{e.get('line', 0)} — {msg}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)


async def _main_async(argv) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --list-adapters 分支：无需任何依赖即可输出
    if args.list_adapters:
        from src.registry.adapters import list_adapters
        print("已注册适配器:")
        for name in list_adapters():
            print(f"  - {name}")
        return 0

    # 参数：source 必填
    if not args.source:
        parser.error("--source 是必填项（或用 --list-adapters 查看适配器）")
        return 2

    src_path = Path(args.source)
    if not src_path.exists():
        print(f"致命错误: 源路径不存在: {src_path}", file=sys.stderr)
        return 1

    from src.registry.importer import run_import

    try:
        report = await run_import(
            source=src_path,
            adapter=args.adapter,
            mode=args.mode,
            report_dir=args.report_dir,
            on_progress=_on_progress_factory(args),
        )
    except KeyError as e:
        # 未注册适配器（理论上 importer 会写 adapter_errors 但构造前就抛）
        print(f"致命错误: {e}", file=sys.stderr)
        from src.registry.adapters import list_adapters
        print(f"可用适配器: {list_adapters()}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"致命错误 (系统): {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2

    _print_summary(report, args)
    return 0


def main(argv=None) -> int:
    return asyncio.run(_main_async(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
