#!/usr/bin/env bash
# T-W5-034 验收脚本：测试冻结与端到端 Holdout 体系
# 本文件是冻结资产（specs/test-freeze/MANIFEST.sha256），开发 agent 只能跑不能改。
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

echo "== 1. 冻结校验器全绿 =="
python tools/ci/check_test_freeze.py

echo "== 2. 红绿双向实证：篡改受保护文件必红 =="
TARGET="tools/accept/t_w5_034.sh"
BACKUP="$(mktemp)"
cp "$TARGET" "$BACKUP"
trap 'cp "$BACKUP" "$TARGET"; rm -f "$BACKUP"' EXIT
echo "# tamper probe" >> "$TARGET"
if python tools/ci/check_test_freeze.py >/dev/null 2>&1; then
  echo "❌ 篡改受保护文件后校验器仍绿——冻结是假的"
  exit 1
fi
cp "$BACKUP" "$TARGET"
python tools/ci/check_test_freeze.py >/dev/null
echo "✅ 篡改即红、恢复即绿"

echo "== 3. holdout 解析器正确性（格式解析，不依赖服务） =="
TMP_MD="$(mktemp --suffix=.md)"
trap 'cp "$BACKUP" "$TARGET" 2>/dev/null || true; rm -f "$BACKUP" "$TMP_MD"' EXIT
cat > "$TMP_MD" <<'EOF'
# 解析夹具
## H-TST-1 必过的 machine 项
- 意图：夹具
- 类型：machine

```bash
true
```

## H-TST-2 必败的 machine 项
- 意图：夹具
- 类型：machine

```bash
false
```

## H-TST-3 人工项
- 意图：夹具
- 类型：human
EOF
if python tools/ci/run_holdout.py "$TMP_MD" >/dev/null 2>&1; then
  echo "❌ 含必败项的 holdout 被判绿"
  exit 1
fi
out="$(python tools/ci/run_holdout.py "$TMP_MD" 2>&1 || true)"
echo "$out" | grep -q "H-TST-1" && echo "$out" | grep -q "H-TST-3" \
  || { echo "❌ holdout 摘要缺条目"; exit 1; }
echo "✅ 解析与判定语义正确"

echo "== 4. CI 门禁接线 =="
grep -q 'test-freeze:' .github/workflows/ci.yml \
  && grep -q 'needs: \[hygiene, deps-audit, check, deps, repo-gate, test-freeze\]' .github/workflows/ci.yml \
  || { echo "❌ test-freeze 未并入 gate"; exit 1; }

echo "== 5. CODEOWNERS 测试资产归属 =="
grep -q '^/tests/ @' .github/CODEOWNERS && grep -q '^/scripts/ @' .github/CODEOWNERS \
  || { echo "❌ CODEOWNERS 未单列 /tests/ 与 /scripts/"; exit 1; }

echo "✅ T-W5-034 验收全绿"
