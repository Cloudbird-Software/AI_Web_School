#!/usr/bin/env bash
# W2 波次出口：出口演示与门禁检查（T-W2-044，E2E-9 唯一验收入口）。
# 任何一项失败即出口不通过（非零退出并打印失败步骤）。
#
# 对照 T-W2-044 验收标准与 tasks/w2/BRIEF.md §3 E2E-1~E2E-10：
# 1. 顺序执行：迁移检查 → 全量测试（含契约）→ golden 回归 →
#    E2E-1 业务演示（demo-w2-business.py 生成真实 PDF）→
#    E2E-5 直写失败实证 → w0/w1 出口不退化检查。
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
# #38 可诊断性：此前的静默检查（>/dev/null 2>&1）让 nightly 红灯只有结论
# 没有证据（连续 18 天 scheduled 失败无一处失败细节）。cap 把输出落临时
# 文件，成功即弃，失败时打印尾部——红灯必须可诊断（宪法 P4 配套义务）。
CHECK_LOG=$(mktemp /tmp/w2-check.XXXXXX.log)
trap 'rm -f "$CHECK_LOG"' EXIT
cap(){ # cap <成功描述> <失败描述> <命令...>
  local okmsg=$1 failmsg=$2; shift 2
  if "$@" > "$CHECK_LOG" 2>&1; then
    ok "$okmsg"
  else
    echo "── 失败输出（尾部 120 行）：$* ──"
    tail -n 120 "$CHECK_LOG"
    die "$failmsg"
  fi
}

echo "== W2 出口验收（T-W2-044 · E2E-9）=="

# ────────────────────────────────────────────────────────────────────
# ① W2 包存在性（W2 交付物不齐即 die）
# ────────────────────────────────────────────────────────────────────
# S1 实例化引擎 / S8 渲染底座 / S3 知识图谱 / S4 B 线装配 / S6 数学包 /
# S7 语文包 / S9 只读 API + 教研工作台
for d in src/core/instantiation src/core/render src/core/knowledge \
         src/core/production src/core/gate \
         src/packs/subject-math src/packs/subject-chinese \
         src/api src/workbench; do
  [ -d "$d" ] && ok "存在 $d" || die "缺失 $d（W2 交付物不齐）"
done

# ────────────────────────────────────────────────────────────────────
# ② 迁移可逆演练（upgrade→downgrade→upgrade）
# ────────────────────────────────────────────────────────────────────
cap "迁移可逆演练（migrate-check）" \
  "迁移演练失败（alembic upgrade→downgrade→upgrade 不闭环）" make migrate-check

# ────────────────────────────────────────────────────────────────────
# ③ 全量测试套件绿（含 contract/golden/golden-path/unit，E2E-10）
# ────────────────────────────────────────────────────────────────────
cap "全量测试套件绿（pytest tests/）" \
  "全量测试套件红（python -m pytest tests/ -q 失败）" python -m pytest tests/ -q

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
# ⑤ 冻结契约清单行数未减少（X8 波内契约冻结，W0 基线 4 条路径）
# ────────────────────────────────────────────────────────────────────
FROZEN_COUNT=$(grep -c '^specs/' specs/contracts/FROZEN.txt || true)
if [ "$FROZEN_COUNT" -ge 4 ]; then
  ok "冻结契约清单 $FROZEN_COUNT 条路径（≥基线 4）"
else
  die "冻结契约清单路径数减少：$FROZEN_COUNT < 4（X8 违规）"
fi

# ────────────────────────────────────────────────────────────────────
# ⑥ 黄金数据集回归（E2E-3：50 母题实例化期望输出逐字节一致）
# ────────────────────────────────────────────────────────────────────
cap "黄金数据集回归绿（tests/golden，E2E-3）" \
  "黄金回归红（tests/golden 失败）" python -m pytest tests/golden -q

# ────────────────────────────────────────────────────────────────────
# ⑦ W0/W1 出口不退化（E2E-10）
# ────────────────────────────────────────────────────────────────────
cap "W0 出口脚本不退化（w0.sh）" \
  "W0 出口脚本退化（w0.sh 失败）" bash scripts/wave-exit/w0.sh
cap "W1 出口脚本不退化（w1.sh）" \
  "W1 出口脚本退化（w1.sh 失败）" bash scripts/wave-exit/w1.sh

# ────────────────────────────────────────────────────────────────────
# ⑧ 门物理阻断实证（E2E-5：绕过写入服务直写 serving 区在 DB 层失败）
# ────────────────────────────────────────────────────────────────────
cap "门物理阻断实证绿（test_gate_bypass.py，E2E-5）" \
  "门物理阻断实证红（直写 serving 区未被 DB 层拒绝）" \
  python -m pytest tests/unit/test_gate_bypass.py -q

# ────────────────────────────────────────────────────────────────────
# ⑨ E2E-1 业务端到端演示（生成真实 PDF 试卷 + 解析册）
# ────────────────────────────────────────────────────────────────────
# 演示脚本输出保留在终端（现场演示）：DSL→实例化→门→签发→组卷→PDF→追溯→作答阻断
echo "── E2E-1 业务链路现场演示（scripts/demo-w2-business.py）──"
if python scripts/demo-w2-business.py; then
  ok "业务端到端演示 PASS（真实 PDF 已产出到 out/，E2E-1）"
else
  die "业务端到端演示失败（demo-w2-business.py 非零退出）"
fi

echo ""
echo "摘要：通过 ${PASS} 项 / 失败 0 项 / 耗时 ${SECONDS}s"
echo "🎉 W2 出口通过：母题引擎、校验门、渲染追溯全线贯通，可派发 W3 任务卡"
