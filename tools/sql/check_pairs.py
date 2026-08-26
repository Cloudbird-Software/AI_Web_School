#!/usr/bin/env python3
"""SQL-1 迁移成对静态检查（T-W5-033 验收 #5 的免基础设施面）。

migrate-go-check（make check 内，运行时 up→down→up 全量演练）依赖 Docker；
本脚本提供零依赖的静态审计，进 CI go-check job：

  1. db/migrations/ 下每个 NNNN_*.up.sql 必须有同名 .down.sql（只升不降 = 红）；
  2. .down.sql 不得为空文件（空文件 = 伪回滚，SQL-1 的"成对"必须是真成对）；
  3. 不允许只有 down 没有 up 的孤儿（半对同样破坏可逆演练的步进语义）；
  4. 版本号 NNNN 前缀不得重复（重复会让 golang-migrate 的版本排序出现歧义）；
  5. 目录内不允许出现子目录（golang-migrate 只读单层；子目录=静默逃逸面，红）。

编码面：down 以 utf-8-sig 读取——BOM-only 文件不是"非空 down"（红队审查 Minor 2）。

运行时可逆性（down 真能执行）仍由 migrate-go-check 承担——静态配对 +
运行时可逆共同构成 SQL-1 的完整 gate 面。只用标准库。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "db" / "migrations"
NAME_RE = re.compile(r"^(\d{4,})_[^/]+\.(up|down)\.sql$")


def main() -> int:
    if not MIGRATIONS.is_dir():
        print(f"❌ 迁移目录不存在: {MIGRATIONS}")
        return 1

    ups: dict[str, str] = {}
    downs: dict[str, str] = {}
    violations: list[str] = []

    for f in sorted(MIGRATIONS.iterdir()):
        if f.is_dir():
            violations.append(f"迁移目录不允许子目录（单层语义，golang-migrate 同）: {f.name}")
            continue
        m = NAME_RE.match(f.name)
        if not m:
            # 与 golang-migrate 命名规约不符的文件直接红（会被 source/file 拒载）
            violations.append(f"命名不符合 NNNN_*.{{up,down}}.sql: {f.name}")
            continue
        version, kind = m.groups()
        bucket = ups if kind == "up" else downs
        if version in bucket:
            violations.append(f"版本号重复: {bucket[version]} 与 {f.name}")
        bucket[version] = f.name

    for version, up_name in sorted(ups.items()):
        down_name = downs.get(version)
        if down_name is None:
            violations.append(f"只有 up 没有 down: {up_name}（SQL-1：只升不降）")
            continue
        down_path = MIGRATIONS / down_name
        # 空文件 = 伪回滚（0 字节绕过"非空"文本判断的所有空白变体）
        if down_path.read_text(encoding="utf-8-sig", errors="strict").strip() == "":
            violations.append(f"down 为空文件（伪回滚）: {down_name}")

    for version, down_name in sorted(downs.items()):
        if version not in ups:
            violations.append(f"只有 down 没有 up: {down_name}（孤儿半对）")

    if violations:
        for v in violations:
            print(f"❌ {v}")
        return 1

    print(f"✅ SQL-1 静态成对检查通过：{len(ups)} 对 up/down（db/migrations/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
