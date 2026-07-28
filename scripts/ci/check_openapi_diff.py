#!/usr/bin/env python3
"""T-W4-042 CI 守卫：openapi-v1.yaml 冻结契约 diff 检测.

落地 tasks/w4/T-W4-042.md 验收 #2：「任何后续 PR 若修改 openapi-v1.yaml 则
CI 失败（人类批准例外）」.

工作机制：
- 用 git diff 比较 PR 基线（默认 origin/main）与当前 HEAD 对
  ``specs/contracts/api/openapi-v1.yaml`` 的修改。
- 检测到任何行级增删 → 退出码非零，打印 diff 摘要。
- 例外：当 commit message 含 ``[FROZEN-APPROVE]`` 标记时放行（人类批准例外）。
  例外标记必须由维护者在合并 PR 时刻意写入，避免误用。

为什么不直接用 git diff --exit-code：本脚本需要给出对人类友好的 diff 摘要
（哪些路径/方法被改了），并支持例外标记机制；git diff --exit-code 只给原始 diff。

用法（CLI，CI 集成）::

    python scripts/ci/check_openapi_diff.py [--base origin/main] [--head HEAD]

用法（库）::

    from scripts.ci.check_openapi_diff import collect_diff, has_frozen_changes
    diffs = collect_diff(base="origin/main", head="HEAD")
    if diffs:
        raise SystemExit(1)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 冻结契约文件路径（相对项目根）
FROZEN_CONTRACT = "specs/contracts/api/openapi-v1.yaml"

# 例外标记：commit message 含此标记时放行（人类显式批准）
EXEMPTION_MARKER = "[FROZEN-APPROVE]"

# 项目根（本脚本位于 scripts/ci/，parents[2] 为项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DiffSummary:
    """openapi-v1.yaml diff 摘要."""

    added_lines: int
    removed_lines: int
    added_paths: list[str]
    removed_paths: list[str]
    raw_diff: str

    @property
    def has_changes(self) -> bool:
        """是否有任何冻结契约改动."""
        return self.added_lines > 0 or self.removed_lines > 0


def _run_git(args: list[str]) -> str:
    """运行 git 命令，返回 stdout（失败抛异常）."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 (exit {result.returncode}): {result.stderr}"
        )
    return result.stdout


def _extract_paths(diff_text: str) -> tuple[list[str], list[str]]:
    """从 diff 文本中提取新增/删除的 path 行（``  /xxx:``）.

    OpenAPI path 行在 yaml 中缩进 2 空格、以 / 开头、以 : 结尾。
    新增行以 ``+  /`` 开头，删除行以 ``-  /`` 开头。
    """
    added: list[str] = []
    removed: list[str] = []
    path_re = re.compile(r"^[+-]\s+(\/[^:]+):")
    for line in diff_text.splitlines():
        m = path_re.match(line)
        if not m:
            continue
        path = m.group(1)
        if line.startswith("+"):
            added.append(path)
        elif line.startswith("-"):
            removed.append(path)
    return added, removed


def collect_diff(base: str = "origin/main", head: str = "HEAD") -> DiffSummary:
    """收集 base..head 范围内对 openapi-v1.yaml 的 diff.

    Args:
        base: 基线 ref（默认 origin/main）。
        head: 当前 ref（默认 HEAD）。

    Returns:
        DiffSummary：增删行数 + 新增/删除路径列表 + 原始 diff 文本。
    """
    # git diff <base> <head> -- <path>
    try:
        diff_text = _run_git(
            ["diff", f"{base}..{head}", "--", FROZEN_CONTRACT]
        )
    except RuntimeError as e:
        # 基线不存在（如本地无 origin/main）→ 视为无 diff（不阻断）
        # 这种情形主要在本地 dev 环境，CI 环境必有 origin/main
        if "not found" in str(e).lower() or "unknown revision" in str(e).lower():
            return DiffSummary(0, 0, [], [], "")
        raise

    added_lines = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed_lines = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    added_paths, removed_paths = _extract_paths(diff_text)
    return DiffSummary(
        added_lines=added_lines,
        removed_lines=removed_lines,
        added_paths=added_paths,
        removed_paths=removed_paths,
        raw_diff=diff_text,
    )


def is_exempt(head: str = "HEAD") -> bool:
    """检查当前 HEAD 的 commit message 是否含例外标记.

    例外标记 ``[FROZEN-APPROVE]`` 表示人类显式批准本次冻结契约修改
    （通常在 PR 合并时刻意写入 commit body）。
    """
    try:
        msg = _run_git(["log", "-1", "--format=%B", head])
    except RuntimeError:
        return False
    return EXEMPTION_MARKER in msg


def has_frozen_changes(base: str = "origin/main", head: str = "HEAD") -> bool:
    """便捷接口：是否有未豁免的冻结契约改动."""
    summary = collect_diff(base=base, head=head)
    if not summary.has_changes:
        return False
    return not is_exempt(head=head)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：检测冻结契约 diff，有改动且未豁免则退出码非零."""
    parser = argparse.ArgumentParser(
        description="检测 openapi-v1.yaml 冻结契约是否被修改（T-W4-042）"
    )
    parser.add_argument(
        "--base", default="origin/main",
        help="基线 ref（默认 origin/main）",
    )
    parser.add_argument(
        "--head", default="HEAD",
        help="当前 ref（默认 HEAD）",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="只输出结论，不打印 diff 摘要",
    )
    args = parser.parse_args(argv)

    summary = collect_diff(base=args.base, head=args.head)

    if not summary.has_changes:
        print(f"✅ {FROZEN_CONTRACT} 未被修改（相对 {args.base}）")
        return 0

    if is_exempt(head=args.head):
        print(
            f"✅ {FROZEN_CONTRACT} 有修改但含 {EXEMPTION_MARKER} 例外标记"
            f"（人类批准放行）"
        )
        if not args.quiet and summary.raw_diff:
            print("\n--- diff 摘要 ---")
            print(summary.raw_diff)
        return 0

    # 未豁免的修改 → 阻断
    print(
        f"❌ {FROZEN_CONTRACT} 被修改（+{summary.added_lines}/-{summary.removed_lines} 行）"
        f"，未含 {EXEMPTION_MARKER} 例外标记"
    )
    if summary.added_paths:
        print(f"  新增路径: {summary.added_paths}")
    if summary.removed_paths:
        print(f"  删除路径: {summary.removed_paths}")
    print(
        f"\n冻结契约修改需人类批准：在合并 commit message 中加入 {EXEMPTION_MARKER} 标记。"
    )
    if not args.quiet and summary.raw_diff:
        print("\n--- 完整 diff ---")
        print(summary.raw_diff)
    return 1


if __name__ == "__main__":
    sys.exit(main())
