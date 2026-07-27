#!/usr/bin/env bash
# W3 波次出口：学生侧业务闭环演示与门禁检查（E2E 唯一验收入口）。
# 任何一项失败即出口不通过（非零退出并打印失败步骤）。
#
# 对照 tasks/w3/BRIEF.md E2E-1~E2E-8：
# 1. 顺序执行：W3 包存在性 → 迁移检查 → 全量测试 → 学科边界 →
#    冻结契约清单 → golden 回归 → w0/w1/w2 出口不退化 →
#    学生侧业务闭环演示（demo-w3-business.py）。
# 2. 任一失败非零退出并打印失败步骤（die）。
# 3. 结尾输出摘要：通过项数/失败项数/耗时。
set -euo pipefail
PASS=0
ok(){ echo "✅ $1"; PASS=$((PASS+1)); }
die(){
  echo "❌ $1"
  echo ""
  echo "摘要：通过 ${PASS} 项 / 失败 1 项（$1）/ 耗时 ${SECONDS}s"
  exit 1
}

echo "== W3 出口验收（学生侧闭环 · 数据飞轮）=="

# ────────────────────────────────────────────────────────────────────
# ① W3 包存在性（W3 交付物不齐即 die）
# ────────────────────────────────────────────────────────────────────
# S1/S2 组卷（assembly）/ S3 在线会话（session）/ S4 评分执行（scoring）/
# S5 弱项报告（report）/ S6 复习排程（review）/ S8 数据域（data）/ S7 英语包
for d in src/core/assembly src/core/session src/core/scoring \
         src/core/report src/core/review src/core/data \
         src/packs/subject-english; do
  [ -d "$d" ] && ok "存在 $d" || die "缺失 $d（W3 交付物不齐）"
done

# ────────────────────────────────────────────────────────────────────
# ② 迁移可逆演练（upgrade→downgrade→upgrade）
# ────────────────────────────────────────────────────────────────────
make migrate-check >/dev/null 2>&1 && ok "迁移可逆演练（migrate-check）" \
  || die "迁移演练失败（alembic upgrade→downgrade→upgrade 不闭环）"

# ────────────────────────────────────────────────────────────────────
# ③ 全量测试套件绿（含 contract/golden/golden-path/unit）
# ────────────────────────────────────────────────────────────────────
python -m pytest tests/ -q >/dev/null 2>&1 && ok "全量测试套件绿（pytest tests/）" \
  || die "全量测试套件红（python -m pytest tests/ -q 失败）"

# ────────────────────────────────────────────────────────────────────
# ④ 学科边界：核心域/注册表无学科包 import（X6 宪法铁律）
# ────────────────────────────────────────────────────────────────────
# 同 pr-check.yml 扫描模式：(import|from)\s+(packs|subject_)
if grep -rnE '(import|from)\s+(packs|subject_)' src/core/ src/registry/ 2>/dev/null; then
  die "核心域/注册表引用了学科包（X6 违规）"
else
  ok "核心域/注册表无学科包 import（X6）"
fi

# ────────────────────────────────────────────────────────────────────
# ⑤ 冻结契约清单行数未减少（X8 波内契约冻结，W3 基线 5 条路径，已含 paper-model.md）
# ────────────────────────────────────────────────────────────────────
FROZEN_COUNT=$(grep -c '^specs/' specs/contracts/FROZEN.txt || true)
if [ "$FROZEN_COUNT" -ge 5 ]; then
  ok "冻结契约清单 $FROZEN_COUNT 条路径（≥基线 5）"
else
  die "冻结契约清单路径数减少：$FROZEN_COUNT < 5（X8 违规）"
fi

# ────────────────────────────────────────────────────────────────────
# ⑥ 黄金数据集回归（50 母题实例化期望输出逐字节一致）
# ────────────────────────────────────────────────────────────────────
python -m pytest tests/golden -q >/dev/null 2>&1 && ok "黄金数据集回归绿（tests/golden）" \
  || die "黄金回归红（tests/golden 失败）"

# ────────────────────────────────────────────────────────────────────
# ⑦ W0/W1/W2 出口不退化
# ────────────────────────────────────────────────────────────────────
bash scripts/wave-exit/w0.sh >/dev/null 2>&1 && ok "W0 出口脚本不退化（w0.sh）" \
  || die "W0 出口脚本退化（w0.sh 失败）"
bash scripts/wave-exit/w1.sh >/dev/null 2>&1 && ok "W1 出口脚本不退化（w1.sh）" \
  || die "W1 出口脚本退化（w1.sh 失败）"
bash scripts/wave-exit/w2.sh >/dev/null 2>&1 && ok "W2 出口脚本不退化（w2.sh）" \
  || die "W2 出口脚本退化（w2.sh 失败）"

# ────────────────────────────────────────────────────────────────────
# ⑧ 学生侧业务闭环演示（E2E-1~E2E-8：练习→报告→复习→诊断→飞轮→时长保护）
# ────────────────────────────────────────────────────────────────────
# 演示脚本输出保留在终端（现场演示）：数据准备→练习闭环→弱项报告→
# 复习队列→诊断闭环→CTT 标定/Elo→时长保护
echo "── 学生侧业务闭环现场演示（scripts/demo-w3-business.py）──"
if python scripts/demo-w3-business.py; then
  ok "学生侧业务闭环演示 PASS（练习/报告/复习/诊断/飞轮/时长保护全线贯通）"
else
  die "学生侧业务闭环演示失败（demo-w3-business.py 非零退出）"
fi

echo ""
echo "摘要：通过 ${PASS} 项 / 失败 0 项 / 耗时 ${SECONDS}s"
echo "🎉 W3 出口通过：学生侧练习-诊断-报告-复习闭环与数据飞轮全线贯通"
