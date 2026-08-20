#!/usr/bin/env python3
"""Holdout 测试执行器（T-W5-034 · tests/holdout/*.md 的机器执行体）。

用法：
  python tools/ci/run_holdout.py tests/holdout/w5r.md

Holdout 文件格式（见 tests/holdout/README.md）：
  ## H-XXX-N 标题
  - 意图：……
  - 类型：machine | human
  machine 条目含且仅含一个 ```bash 代码块，即该条目的可执行探针。

执行语义：
  - machine 条目：以 bash -euo pipefail 执行，退出码 0 = PASS，否则 FAIL。
  - human 条目：不执行，汇总为"待人工确认"清单（不判红，但波次出口需人类逐项签字）。
  - 任一 machine FAIL → 退出码 1。
只用标准库。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ITEM_RE = re.compile(r"^## (H-[A-Z0-9]+-\d+)\s+(.*)$")
TYPE_RE = re.compile(r"^-\s*类型[:：]\s*(machine|human)\s*$")


def parse_items(path: Path) -> list[dict]:
    """把 holdout markdown 解析为条目列表：id/title/kind/code。"""
    items: list[dict] = []
    current: dict | None = None
    in_bash = False
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ITEM_RE.match(line)
        if m:
            current = {"id": m.group(1), "title": m.group(2).strip(),
                       "kind": None, "code": []}
            items.append(current)
            in_bash = False
            continue
        if current is None:
            continue
        t = TYPE_RE.match(line)
        if t:
            current["kind"] = t.group(1)
        if line.strip().startswith("```"):
            in_bash = line.strip() == "```bash" if not in_bash else False
            continue
        if in_bash:
            current["code"].append(line)
    for it in items:
        it["code"] = "\n".join(it["code"]).strip()
    return items


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法: python tools/ci/run_holdout.py tests/holdout/<wave>.md")
        return 2
    path = Path(argv[0])
    if not path.is_file():
        print(f"❌ holdout 文件不存在: {path}")
        return 2
    items = parse_items(path)
    if not items:
        print(f"❌ 未解析到任何 holdout 条目（## H-XXX-N）: {path}")
        return 2

    passed: list[str] = []
    failed: list[str] = []
    human: list[str] = []
    for it in items:
        if it["kind"] != "machine":
            human.append(f"{it['id']} {it['title']}")
            continue
        if not it["code"]:
            failed.append(it["id"])
            print(f"❌ {it['id']} {it['title']} —— machine 条目缺少 bash 探针")
            continue
        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", it["code"]],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            passed.append(it["id"])
            print(f"✅ {it['id']} {it['title']}")
        else:
            failed.append(it["id"])
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
            print(f"❌ {it['id']} {it['title']}")
            for ln in tail:
                print(f"     {ln}")

    print("\n== Holdout 摘要 ==")
    print(f"machine 通过 {len(passed)} / 失败 {len(failed)}；待人工确认 {len(human)} 项")
    for h in human:
        print(f"  ☐ {h}")
    if failed:
        print(f"❌ HOLDOUT RED: {', '.join(failed)}")
        return 1
    print("✅ HOLDOUT GREEN（machine 项全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
