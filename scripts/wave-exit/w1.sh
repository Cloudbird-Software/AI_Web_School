#!/usr/bin/env bash
# W1 波次出口：核心域落地验收。任何一项失败即出口不通过。
#
# 对照 T-W1-008 验收标准：
# 1. 本脚本存在且可执行（make_accept.sh 通过 accept_script 调用本脚本）
# 2. 出口检查项：
#    (a) W1 五大核心包存在：src/core/models/ src/registry/ src/core/events/
#        src/core/gate/ src/core/content/
#    (b) make migrate-check 全绿（迁移可逆）
#    (c) make test 全绿（全量测试套件）
#    (d) 核心域无学科包 import（X6 宪法铁律）
#    (e) 冻结契约清单 FROZEN.txt 行数未减少（W0 基线 4 条路径）
# 3. 执行 bash scripts/wave-exit/w1.sh 全绿
# 4. CI nightly workflow（本卡未新增步骤：nightly 已覆盖 migrate-check +
#    contract + golden + golden-path；W1 出口脚本为本地/手动验收工具，
#    nightly 不重复执行以避免双重红灯告警）
# 5. 依赖项全部完成才可验收（本脚本检查的五大包即 W1-002/003/004/005/006/007
#    交付物；缺一即 die）
set -euo pipefail
ok(){ echo "✅ $1"; }; die(){ echo "❌ $1"; exit 1; }

echo "== W1 出口验收 =="

# ────────────────────────────────────────────────────────────────────
# (a) W1 五大核心包存在
# ────────────────────────────────────────────────────────────────────
# 任务卡原文写 src/core/registries/，实际路径为 src/registry/（见 T-W1-004
# owner_module: src/registry）——架构 v2 §2.3 注册表与 src/core 平级。
for d in src/core/models src/registry src/core/events src/core/gate src/core/content; do
  [ -d "$d" ] && ok "存在 $d" || die "缺失 $d（W1 交付物不齐）"
done

# ────────────────────────────────────────────────────────────────────
# (b) 迁移可逆演练（upgrade→downgrade→upgrade）
# ────────────────────────────────────────────────────────────────────
make migrate-check >/dev/null 2>&1 && ok "迁移可逆演练（migrate-check）" \
  || die "迁移演练失败（alembic upgrade→downgrade→upgrade 不闭环）"

# ────────────────────────────────────────────────────────────────────
# (c) 全量测试套件绿
# ────────────────────────────────────────────────────────────────────
# make test = python -m pytest tests/ -x -q；-x 遇首错即停，便于定位。
make test >/dev/null 2>&1 && ok "测试套件绿（make test）" \
  || die "测试套件红（make test 失败）"

# ────────────────────────────────────────────────────────────────────
# (d) 核心域无学科包 import（X6 宪法铁律）
# ────────────────────────────────────────────────────────────────────
# 同 pr-check.yml 的扫描模式：(import|from)\s+(packs|subject_)
# 为什么扫 src/core/ 而非全仓：核心域零学科特判（A5/A7）；学科包自身相互
# 引用是允许的。src/registry/ 是平台级类型系统，与 src/core 同级，也扫。
if grep -rnE '(import|from)\s+(packs|subject_)' src/core/ src/registry/ 2>/dev/null; then
  die "核心域/注册表引用了学科包（X6 违规）"
else
  ok "核心域/注册表无学科包 import（X6）"
fi

# ────────────────────────────────────────────────────────────────────
# (e) 冻结契约清单行数未减少
# ────────────────────────────────────────────────────────────────────
# W0 末 FROZEN.txt 基线 4 条路径（item-model.md / response_event.md /
# interaction.yaml / scorer.yaml）。契约只能新增不能删除（X8 波内契约冻结）。
# wc -l < file 不含末行换行符差异；用 grep -c '^specs/' 精确数路径行。
FROZEN_COUNT=$(grep -c '^specs/' specs/contracts/FROZEN.txt || true)
if [ "$FROZEN_COUNT" -ge 4 ]; then
  ok "冻结契约清单 $FROZEN_COUNT 条路径（≥基线 4）"
else
  die "冻结契约清单路径数减少：$FROZEN_COUNT < 4（X8 违规）"
fi

echo ""
echo "🎉 W1 出口通过：核心域落地完成，可派发 W2 任务卡"
