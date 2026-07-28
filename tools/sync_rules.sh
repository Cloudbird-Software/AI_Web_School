#!/usr/bin/env bash
# 把 .agent/rules/core.md 同步为各 IDE/CLI 的规则文件（单一事实源，多处生效）
# 现状口径（2026-07-28 精简）：AGENTS.md 为唯一完整版；.trae/rules/core.md 为指针
# （Trae 新版可读 AGENTS.md；指针同时引导人类/工具到正确位置）。CLAUDE.md 已停用。
set -euo pipefail
SRC=".agent/rules/core.md"
HEADER="<!-- 由 tools/sync_rules.sh 从 .agent/rules/core.md 生成，禁止直接编辑 -->\n\n"

printf "$HEADER" > AGENTS.md
cat "$SRC" >> AGENTS.md

mkdir -p .trae/rules
printf "$HEADER" > .trae/rules/core.md
printf '规则全文见根目录 AGENTS.md（单一事实源为 .agent/rules/core.md，本文件仅指针）。\n' >> .trae/rules/core.md

echo "✅ 已同步：AGENTS.md（完整版）, .trae/rules/core.md（指针）"
# 一致性校验：CI 中调用本脚本后 git diff 必须为空（防止有人改了生成物没改源）
if [[ "${CI:-false}" == "true" ]]; then
  git diff --exit-code .trae/rules/core.md AGENTS.md >/dev/null \
    && echo "✅ 规则文件与源一致" \
    || { echo "❌ 规则文件与 .agent/rules/core.md 不一致，请运行 make sync-rules"; exit 1; }
fi
