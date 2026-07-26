#!/usr/bin/env bash
# W0 波次出口：地基验收。任何一项失败即出口不通过。
set -euo pipefail
ok(){ echo "✅ $1"; }; die(){ echo "❌ $1"; exit 1; }

echo "== W0 出口验收 =="

# 1. 环境自检
make bootstrap >/dev/null 2>&1 && ok "bootstrap 环境自检" || die "bootstrap 失败"

# 2. DevOS 文件齐备
for f in specs/constitution.md .agent/rules/core.md .agent/routing.yaml \
         .agent/roles/builder.md .agent/roles/verifier.md .agent/roles/judge.md .agent/roles/scribe.md \
         tasks/board.md tasks/templates/task-card.md tools/opc; do
  [ -f "$f" ] && ok "存在 $f" || die "缺失 $f"
done

# 3. 规则同步一致
CI=true bash tools/sync_rules.sh >/dev/null && ok "规则文件同步一致" || die "规则文件不同步"

# 4. 任务板校验
python tools/opc board >/dev/null && ok "任务板校验" || die "任务板校验失败"

# 5. 迁移可逆
make migrate-check >/dev/null 2>&1 && ok "迁移可逆演练" || die "迁移演练失败（或 alembic 未初始化）"

# 6. CI 配置存在且语法可解析
python - <<'EOF' && ok "CI workflow 语法" || die "CI workflow 语法错误"
import yaml, glob
for f in glob.glob(".github/workflows/*.yml"): yaml.safe_load(open(f, encoding="utf-8"))
EOF

# 7. 最小链路演示（OPC §6.1：创建题目记录→过门（占位验证器）→查询）+ 测试套件
python scripts/demo-w0-min-link.py >/dev/null 2>&1 && ok "最小链路（建题→过门→查询）" || die "最小链路失败"
python -m pytest tests/ -q >/dev/null 2>&1 && ok "测试套件绿" || die "测试套件红"

echo ""
echo "🎉 W0 出口通过：可以开始派发 W1 任务卡"
