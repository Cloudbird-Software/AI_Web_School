#!/usr/bin/env python3
"""测试冻结校验器（T-W5-034 · specs/test-freeze/README.md 的机器执行体）。

三种用法：
  python tools/ci/check_test_freeze.py            # 本地/CI：哈希 + 覆盖校验
  python tools/ci/check_test_freeze.py --base origin/main   # PR 模式：追加清单变更拦截
  python tools/ci/check_test_freeze.py --resign   # 人类例外：重算 MANIFEST.sha256（需 [TEST-FREEZE-APPROVE]）

退出码：0 全绿；1 有任何违规。只用标准库。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FREEZE_DIR = ROOT / "specs" / "test-freeze"
MANIFEST = FREEZE_DIR / "MANIFEST.txt"
HASHES = FREEZE_DIR / "MANIFEST.sha256"
APPROVE_MARKER = "[TEST-FREEZE-APPROVE]"

# 纯新增豁免目录（README §三）：该目录下新增文件不强制登记
ADD_EXEMPT_PREFIX = "tests/golden/items/"


def fail(msg: str) -> None:
    print(f"❌ {msg}")


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def load_protected_paths() -> list[str]:
    """读取 MANIFEST.txt，返回规范化（正斜杠、去尾斜杠另记）路径列表。"""
    paths: list[str] = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line)
    return paths


def expand_manifest(paths: list[str]) -> set[str]:
    """把目录条目展开为仓库内实际存在的文件集合（相对路径，正斜杠）。"""
    files: set[str] = set()
    for p in paths:
        if p.endswith("/"):
            base = ROOT / p
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    files.add(f.relative_to(ROOT).as_posix())
        else:
            if (ROOT / p).is_file():
                files.add(p)
    return files


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_hash_manifest() -> dict[str, str]:
    out: dict[str, str] = {}
    if not HASHES.is_file():
        return out
    for line in HASHES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel.strip()] = digest.strip()
    return out


def resign(paths: list[str]) -> int:
    files = expand_manifest(paths)
    lines = [
        "# 由 check_test_freeze.py --resign 生成，禁止手改；变更须走 specs/test-freeze/README.md §五 人类例外流程",
    ]
    for rel in sorted(files):
        lines.append(f"{sha256_of(ROOT / rel)}  {rel}")
    HASHES.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"已重签 {len(files)} 个受保护文件 → {HASHES.relative_to(ROOT)}")
    print("提醒：本次变更的提交信息必须含 [TEST-FREEZE-APPROVE]，且 PR 须 owner 本人批准。")
    return 0


def check_hashes() -> int:
    errors = 0
    recorded = load_hash_manifest()
    if not recorded:
        fail(f"{HASHES.relative_to(ROOT)} 为空或不存在——冻结清单缺失即红线")
        return 1
    for rel, digest in sorted(recorded.items()):
        f = ROOT / rel
        if not f.is_file():
            fail(f"受保护文件被删除：{rel}")
            errors += 1
        elif sha256_of(f) != digest:
            fail(f"受保护文件被篡改：{rel}")
            errors += 1
    if errors == 0:
        ok(f"哈希冻结：{len(recorded)} 个受保护文件全部一致")
    return errors


def check_coverage(paths: list[str]) -> int:
    """受保护目录下的每个文件都必须登记在哈希清单（豁免：纯新增黄金用例）。"""
    errors = 0
    recorded = set(load_hash_manifest())
    for rel in sorted(expand_manifest(paths)):
        if rel not in recorded and not rel.startswith(ADD_EXEMPT_PREFIX):
            fail(f"受保护目录下出现未登记文件：{rel}（须走 README §五 登记）")
            errors += 1
    if errors == 0:
        ok("覆盖完整：受保护目录无未登记文件")
    return errors


def check_pr_diff(base: str) -> int:
    """PR 模式：冻结清单本体被改 → 要求任一提交信息含 [TEST-FREEZE-APPROVE]。"""
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD", "--", "specs/test-freeze/"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        fail(f"无法执行 git diff（{e}）——PR 模式必须有 git 环境")
        return 1
    changed = [f for f in diff if f.endswith(("MANIFEST.txt", "MANIFEST.sha256"))]
    if not changed:
        ok("清单本体未变更")
        return 0
    log = subprocess.run(
        ["git", "log", "--format=%B", f"{base}..HEAD"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    if APPROVE_MARKER in log:
        ok(f"清单变更 {changed} 携带 {APPROVE_MARKER} 人类批准标记")
        return 0
    fail(f"冻结清单被修改（{changed}）但无 {APPROVE_MARKER} 标记——测试资产变更必须人类批准")
    return 1


def main(argv: list[str]) -> int:
    paths = load_protected_paths()
    if "--resign" in argv:
        return resign(paths)
    errors = check_hashes() + check_coverage(paths)
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
        errors += check_pr_diff(base)
    if errors:
        print(f"\n❌ 测试冻结校验失败（{errors} 处违规）。规则见 specs/test-freeze/README.md")
        return 1
    print("\n✅ 测试冻结校验全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
