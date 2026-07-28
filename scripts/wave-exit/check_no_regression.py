#!/usr/bin/env python
"""T-W4-050 不退化检查：测试总数对比基线（W3 出口 = 1476）.

E2E-10（不退化）机器可验部分：
- 收集当前 tests/ 下全部 test item 数（pytest --collect-only）；
- 与基线（W3 出口时的测试数）对比；
- 当前数 < 基线 ⇒ 失败（测试被删除/弱化）；
- 当前数 >= 基线 ⇒ 通过（只增不减，X1 纪律）。

用法:
    python scripts/wave-exit/check_no_regression.py
    python scripts/wave-exit/check_no_regression.py --baseline 1476

退出码：0=通过；1=退化（测试数减少）。

设计要点：
- 用 --collect-only 计数，不实际执行测试（执行由 w4.sh 其他步骤承载）；
- collect-only 计的是参数化展开后的 test item 数，与 pytest 报告的
  passed/failed 总数口径一致，可比；
- 基线值由 W3 出口固化（tasks/w4/BRIEF.md 输入「main，1476 测试全绿」）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# W3 出口基线（BRIEF.md 输入：main，1476 测试全绿）
DEFAULT_BASELINE = 1476

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def collect_test_count() -> int:
    """用 pytest --collect-only 数当前 tests/ 下 test item 数.

    Returns:
        test item 数（参数化展开后）。

    Raises:
        RuntimeError: collect 失败（语法错误等）。
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(_PROJECT_ROOT / "tests"),
            "--collect-only", "-q",
            "--tb=short",
            "-p", "no:cacheprovider",
        ],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    # collect-only -q 输出形如：
    #   tests/unit/test_x.py::test_a
    #   tests/unit/test_x.py::test_b
    #   ...
    #   1476 tests collected in 3.2s
    # 退出码 0=成功；非0=有收集错误（但仍可能输出部分计数）
    out = result.stdout + result.stderr
    # 优先解析末尾 "N tests collected"
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    if m:
        return int(m.group(1))
    # 退化：数 "path::test_name" 行（容错）
    lines = [
        ln for ln in result.stdout.splitlines()
        if "::" in ln and not ln.strip().startswith("=")
    ]
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="不退化检查：测试总数对比基线")
    parser.add_argument(
        "--baseline", type=int, default=DEFAULT_BASELINE,
        help=f"基线测试数（默认 {DEFAULT_BASELINE}，W3 出口）",
    )
    args = parser.parse_args()

    print(f"== 不退化检查（基线 {args.baseline}）==")
    current = collect_test_count()
    print(f"当前测试数：{current}")
    print(f"基线测试数：{args.baseline}")
    diff = current - args.baseline
    if diff < 0:
        print(f"❌ 退化：测试数减少 {-diff}（{current} < {args.baseline}）")
        print("   请检查是否删除/弱化了既有测试（违反 X1）")
        return 1
    if diff == 0:
        print(f"✅ 持平：测试数 = 基线 {args.baseline}")
    else:
        print(f"✅ 只增不减：测试数 +{diff}（{current} >= {args.baseline}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
