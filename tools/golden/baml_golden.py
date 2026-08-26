#!/usr/bin/env python3
"""BAML-1 golden 快照检查（T-W5-030 验收 #2 的 gate 化）。

为什么用文件哈希快照而非 LLM 回放：golden 的对象是 **prompt 资产本身**
（.baml 源文件），不是模型输出——prompt 变更必须显式过快照（意图可追溯），
模型输出的回归由 W6 共识基准集承担。CI 无需 Node/BAML CLI：只比对哈希。

用法：
    make baml-golden-update   # prompt 变更后显式刷新快照
    make baml-golden-check    # gate 检查：快照与 baml_src 不一致即红
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BAML_SRC = REPO / "baml_src"
SNAPSHOT = REPO / "tools" / "golden" / "baml_src.sha256"


def digest() -> str:
    """对 baml_src 全部 .baml 文件（排序后）做聚合 SHA-256。"""
    files = sorted(BAML_SRC.rglob("*.baml"))
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(BAML_SRC).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("check", "update"):
        print("用法: baml_golden.py check|update", file=sys.stderr)
        return 2
    current = digest()
    if sys.argv[1] == "update":
        SNAPSHOT.write_text(current + "\n", encoding="ascii")
        print(f"✅ golden 快照已更新: {current[:16]}…")
        return 0
    if not SNAPSHOT.exists():
        print("❌ golden 快照不存在；先运行 make baml-golden-update", file=sys.stderr)
        return 1
    expected = SNAPSHOT.read_text(encoding="ascii").strip()
    if current != expected:
        print(
            "❌ baml_src 变更未过 golden 快照（BAML-1）："
            "确认 prompt 变更意图后运行 make baml-golden-update",
            file=sys.stderr,
        )
        return 1
    print(f"✅ baml_src golden 一致: {current[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
