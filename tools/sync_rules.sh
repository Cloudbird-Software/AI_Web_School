#!/usr/bin/env bash
# 把 .agent/rules/core.md 同步为各 IDE/CLI 的规则文件（单一事实源，多处生效）
set -euo pipefail
SRC=".agent/rules/core.md"
HEADER="<!-- 由 tools/sync_rules.sh 从 .agent/rules/core.md 生成，禁止直接编辑 -->\n\n"

mkdir -p .trae/rules
printf "$HEADER" > .trae/rules/core.md
cat "$SRC" >> .trae/rules/core.md

printf "$HEADER" > CLAUDE.md
cat "$SRC" >> CLAUDE.md

printf "$HEADER" > AGENTS.md
cat "$SRC" >> AGENTS.md

echo "✅ 已同步：.trae/rules/core.md, CLAUDE.md, AGENTS.md"
# 一致性校验：CI 中调用本脚本后 git diff 必须为空（防止有人改了生成物没改源）
if [[ "${CI:-false}" == "true" ]]; then
  git diff --exit-code .trae/rules/core.md CLAUDE.md AGENTS.md >/dev/null \
    && echo "✅ 规则文件与源一致" \
    || { echo "❌ 规则文件与 .agent/rules/core.md 不一致，请运行 make sync-rules"; exit 1; }
fi
