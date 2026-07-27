#!/usr/bin/env python3
"""T-W2-015 CI 拦截：扫描 content/ 下所有 license_id 引用，校验是否已登记且 approved.

用法（CI / 本地）:
    python tools/ci/check_sources.py [--content-dir content] [--registry content/sources/registry.yaml]

退出码:
    0  全部 license_id 已登记且 approved（或无任何引用）
    1  发现未登记 / decision!=approved / 已过期 的 license_id
    2  registry.yaml 自身 schema 校验失败

宪法 R-Q-18：无登记或 decision!=approved 的来源不得入库。
本脚本作为 CI 拦截器，在 pr-check.yml 中执行（hooks 由调用方接入）。

为什么扫描 YAML/JSON 而非源码：license_id 通过数据文件（corpora/seeds/items）
引用入库；扫描代码不必要且会误报字符串字面量。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# 让脚本独立可执行（不依赖项目 PYTHONPATH）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

from src.core.content.source_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    SourceRegistry,
)


# ────────────────────────────────────────────────────────────────────
# 配置
# ────────────────────────────────────────────────────────────────────

# 扫描的文件扩展名（YAML/JSON 是内容数据载体）
_SCAN_EXTS: set[str] = {".yaml", ".yml", ".json"}

# registry.yaml 自身不扫描（避免自指）
_REGISTRY_FILENAME: str = "registry.yaml"

# license_id 字段的正则匹配（YAML 行如 `  license_id: lic-xxx` 或 JSON `"license_id": "lic-xxx"`）
_LICENSE_KEY_RE = re.compile(
    r"""["']?license_id["']?\s*[:=]\s*["']?([A-Za-z0-9_\-.]+)["']?""",
    re.MULTILINE,
)


@dataclass
class Violation:
    """单条违规."""

    file: Path
    line: int
    license_id: str
    reason: str  # "未登记" / "未批准" / "已过期"


# ────────────────────────────────────────────────────────────────────
# 扫描器
# ────────────────────────────────────────────────────────────────────


def _iter_content_files(content_dir: Path) -> Iterable[Path]:
    """递归遍历 content/ 下的 YAML/JSON 文件，跳过 registry.yaml 自身."""
    for path in content_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SCAN_EXTS:
            continue
        if path.name == _REGISTRY_FILENAME:
            continue
        yield path


def _extract_license_ids(path: Path) -> list[tuple[int, str]]:
    """从单个文件提取 (行号, license_id) 列表.

    使用正则而非 YAML/JSON 解析：license_id 可能出现在任何层级；
    正则更鲁棒（YAML 注释、嵌套结构都能命中）。
    """
    hits: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for m in _LICENSE_KEY_RE.finditer(text):
        # 计算行号（match.start() 之前的换行符数 + 1）
        line_no = text.count("\n", 0, m.start()) + 1
        hits.append((line_no, m.group(1)))
    return hits


# ────────────────────────────────────────────────────────────────────
# 主逻辑
# ────────────────────────────────────────────────────────────────────


def check(content_dir: Path, registry_path: Path) -> tuple[list[Violation], SourceRegistry]:
    """执行扫描.

    Returns:
        (违规列表, 已加载的 SourceRegistry)
    """
    reg = SourceRegistry.from_yaml(registry_path)

    violations: list[Violation] = []
    for f in _iter_content_files(content_dir):
        for line_no, lid in _extract_license_ids(f):
            rec = reg.get_license(lid)
            if rec is None:
                violations.append(Violation(f, line_no, lid, "未登记"))
            elif rec.decision != "approved":
                violations.append(
                    Violation(f, line_no, lid, f"未批准（decision={rec.decision}）")
                )
            elif not reg.is_approved(lid):
                # decision=approved 但 is_approved=False → 过期
                violations.append(Violation(f, line_no, lid, "已过期"))

    return violations, reg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI 拦截：license_id 必须已登记且 approved")
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=_PROJECT_ROOT / "content",
        help="content/ 根目录（默认 <project>/content）",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="registry.yaml 路径",
    )
    args = parser.parse_args(argv)

    if not args.content_dir.is_dir():
        print(f"❌ content 目录不存在: {args.content_dir}", file=sys.stderr)
        return 2
    if not args.registry.is_file():
        print(f"❌ registry.yaml 不存在: {args.registry}", file=sys.stderr)
        return 2

    # Step 1: 加载 registry（schema 自校验）
    try:
        violations, reg = check(args.content_dir, args.registry)
    except Exception as e:
        print(f"❌ registry.yaml 校验失败: {e}", file=sys.stderr)
        return 2

    # Step 2: 输出
    print(f"== 来源登记表加载 {len(reg)} 条 ==")
    approved = reg.all_approved()
    print(f"  approved: {len(approved)}")
    print(f"  rejected/expired: {len(reg) - len(approved)}")
    print()

    if not violations:
        print(f"✅ 扫描 {args.content_dir} 无违规 license_id 引用")
        return 0

    print(f"❌ 发现 {len(violations)} 条违规:")
    for v in violations:
        rel = v.file.relative_to(args.content_dir.parent) if v.file.is_relative_to(args.content_dir.parent) else v.file
        print(f"  - {rel}:{v.line}  license_id={v.license_id}  reason={v.reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
