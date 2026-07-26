#!/usr/bin/env bash
# 任务验收：唯一完成标准。用法: bash tools/make_accept.sh T-W0-001
set -euo pipefail
TASK="${1:?用法: make_accept.sh <task-id>}"

# 1. 找任务卡
CARD=$(find tasks -name "${TASK}.md" | head -1)
[ -n "$CARD" ] || { echo "❌ 未找到任务卡 ${TASK}"; exit 1; }
echo "== 验收 ${TASK}（${CARD}）=="

# 2. 反削弱检查：本分支不得删除/弱化既有测试
if git rev-parse --verify main >/dev/null 2>&1 && [ "$(git branch --show-current)" != "main" ]; then
  REMOVED=$(git diff main...HEAD -- tests/ | grep -c '^-.*assert' || true)
  SKIPS=$(git diff main...HEAD -- tests/ | grep -c '^+.*\(skip\|xfail\)' || true)
  [ "$REMOVED" -eq 0 ] || { echo "❌ 检测到删除既有断言 ${REMOVED} 处（违反 X1）"; exit 1; }
  [ "$SKIPS" -eq 0 ] || { echo "❌ 检测到新增 skip/xfail ${SKIPS} 处（违反 X1）"; exit 1; }
fi

# 3. 通用门禁：契约测试 + 黄金数据集 + 黄金路径快版
python -m pytest tests/contract -q || { echo "❌ 契约测试"; exit 1; }
python -m pytest tests/golden -q || { echo "❌ 黄金数据集"; exit 1; }
GOLDEN_PATH_QUICK=1 python -m pytest tests/golden-path -q || { echo "❌ 黄金路径(快版)"; exit 1; }

# 4. 任务专属验收：任务卡 acceptance 段指定的脚本（若存在）
SPECIFIC=$(grep -E '^\s*accept_script:' "$CARD" | awk '{print $2}' || true)
if [ -n "${SPECIFIC:-}" ]; then
  bash "$SPECIFIC" || { echo "❌ 任务专属验收 ${SPECIFIC}"; exit 1; }
fi

echo "✅ ${TASK} 验收通过"
