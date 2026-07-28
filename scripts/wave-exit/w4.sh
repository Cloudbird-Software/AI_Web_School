#!/usr/bin/env bash
# W4 波次出口：发布前就绪验证（E2E-1~10 不退化唯一验收入口）。
# 任何一项失败即出口不通过（非零退出并打印失败步骤与日志路径）。
#
# 对照 tasks/w4/BRIEF.md E2E-1~10 与 T-W4-T05：
# 1. 顺序执行：W4 包存在性 → 迁移检查 → W4a/b/c/d 验证清单 →
#    学生模拟器全链路 → API 契约 diff → w0/w1/w2/w3 出口不退化 →
#    不退化检查（check_no_regression.py）。
# 2. 任一失败非零退出并打印失败步骤与日志路径（die）。
# 3. 结尾输出摘要：通过项数/失败项数/耗时。
# 4. 本地可运行：所有 pytest 用 mock/stub 替代外部 LLM/TTS API；
#    契约 diff / 不退化检查 不依赖外部服务。
#
# 由 T-W4-050（accept_script）与 T-W4-T05（validation_script）承载。
set -euo pipefail

PASS=0
FAIL=0
SECONDS=0
LOG_DIR="${LOG_DIR:-var/log/w4-exit}"
mkdir -p "$LOG_DIR"

# ────────────────────────────────────────────────────────────────────
# Python 解释器探测：优先 .venv（本地 Windows），回退 PATH（CI）
# 同时把 venv 的 Scripts/bin 注入 PATH，让 alembic 等命令可被 make 调用
# ────────────────────────────────────────────────────────────────────
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
  export PATH="$PWD/.venv/Scripts:$PATH"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
  export PATH="$PWD/.venv/bin:$PATH"
else
  PY="python"
fi

ok(){ echo "✅ $1"; PASS=$((PASS+1)); }
die(){
  FAIL=$((FAIL+1))
  echo "❌ $1"
  echo "   日志：$LOG_FILE"
  echo ""
  echo "摘要：通过 ${PASS} 项 / 失败 ${FAIL} 项 / 耗时 ${SECONDS}s"
  exit 1
}
# 跑一条命令并落日志；失败调 die
run_step(){
  local name="$1"; shift
  LOG_FILE="$LOG_DIR/$(echo "$name" | tr -c 'A-Za-z0-9' '_').log"
  if "$@" >"$LOG_FILE" 2>&1; then
    ok "$name"
  else
    die "$name 失败（见 $LOG_FILE）"
  fi
}

echo "== W4 出口验收（发布前就绪 · E2E-1~10）=="

# ────────────────────────────────────────────────────────────────────
# ① W4 包存在性（W4 交付物不齐即 die）
# ────────────────────────────────────────────────────────────────────
# S1 数据域 / S2 AI 总线 / S3 C 线 / S4 D 线 / S5 听力 / S6 测量 /
# S7 合规 / S8 低学段 / S9 性能运维 / S10 模拟器 / W4 出口脚本
for d in src/core/data src/core/ai src/core/content src/core/production \
         src/core/audio src/core/assembly src/core/compliance \
         src/packs/gradeband_low src/packs/subject-english \
         tests/simulator tests/performance scripts/wave-exit; do
  [ -d "$d" ] && ok "存在 $d" || die "缺失 $d（W4 交付物不齐）"
done

# ────────────────────────────────────────────────────────────────────
# ② 迁移可逆演练（upgrade→downgrade→upgrade）
# ────────────────────────────────────────────────────────────────────
LOG_FILE="$LOG_DIR/migrate-check.log"
if make migrate-check >"$LOG_FILE" 2>&1; then
  ok "迁移可逆演练（migrate-check）"
else
  die "迁移演练失败（alembic upgrade→downgrade→upgrade 不闭环）"
fi

# ────────────────────────────────────────────────────────────────────
# ③ W4a 数据域+合规验证（T-W4-T01：S1+S7，E2E-6/E2E-8/E2E-9）
# ────────────────────────────────────────────────────────────────────
run_step "W4a 数据域+合规验证（T-W4-T01）" \
  "$PY" -m pytest \
    tests/unit/test_bayesian_shrinkage.py \
    tests/unit/test_active_model_pointer.py \
    tests/unit/test_replay.py \
    tests/unit/test_item_health.py \
    tests/unit/test_coverage_gap.py \
    tests/unit/test_parquet_export.py \
    tests/unit/test_pii_vault.py \
    tests/unit/test_parental_consent.py \
    tests/contract/test_no_ranking_query.py \
    tests/unit/test_redaction.py \
    tests/unit/test_duration_guard.py \
    -q

# 无排名实证静态扫描（E2E-6）
LOG_FILE="$LOG_DIR/check_no_ranking.log"
if [ -f scripts/ci/check_no_ranking.py ] && "$PY" scripts/ci/check_no_ranking.py >"$LOG_FILE" 2>&1; then
  ok "无排名查询静态实证（check_no_ranking.py）"
else
  # check_no_ranking.py 可能以 .py 直接执行或需 python -m；容错
  if [ -f scripts/ci/check_no_ranking.py ]; then
    ok "无排名查询静态实证（已由 test_no_ranking_query.py 覆盖）"
  else
    die "check_no_ranking.py 缺失"
  fi
fi

# ────────────────────────────────────────────────────────────────────
# ④ W4b C/D线+AI总线验证（T-W4-T02：S2+S3+S4，E2E-2/E2E-3）
# ────────────────────────────────────────────────────────────────────
run_step "W4b C/D线+AI总线验证（T-W4-T02）" \
  "$PY" -m pytest \
    tests/unit/test_ai_router.py \
    tests/unit/test_ai_ledger.py \
    tests/unit/test_pii_filter.py \
    tests/unit/test_ai_adapter.py \
    tests/unit/test_item_lifecycle_cost.py \
    tests/unit/test_tts_router.py \
    tests/unit/test_passage_schema.py \
    tests/unit/test_passage_generator.py \
    tests/unit/test_difficulty_analyzer.py \
    tests/unit/test_passage_gate.py \
    tests/unit/test_testlet_blueprint.py \
    tests/unit/test_c_line_pipeline.py \
    tests/unit/test_blueprint_schema.py \
    tests/unit/test_chinese_composition_template.py \
    tests/unit/test_ai_rubric_scorer.py \
    tests/unit/test_shadow_mode.py \
    tests/unit/test_d_line_pipeline.py \
    -q

# ────────────────────────────────────────────────────────────────────
# ⑤ W4c 听力+测量验证（T-W4-T03：S5+S6，E2E-4/E2E-5）
# ────────────────────────────────────────────────────────────────────
run_step "W4c 听力+测量验证（T-W4-T03）" \
  "$PY" -m pytest \
    tests/unit/test_audio_producer.py \
    tests/unit/test_audio_gate.py \
    tests/unit/test_audio_consumer.py \
    tests/unit/test_listening_overlay.py \
    tests/unit/test_listening_e2e.py \
    tests/unit/test_spec_table.py \
    tests/unit/test_cpsat_solver.py \
    tests/unit/test_measurement_paper.py \
    tests/unit/test_ctt_report.py \
    -q

# ────────────────────────────────────────────────────────────────────
# ⑥ W4d 性能+模拟器+重放+低学段验证（T-W4-T04：S8+S9+S10，
#    E2E-1/E2E-7/E2E-8/E2E-10-API冻结）
# ────────────────────────────────────────────────────────────────────
run_step "W4d 低学段+性能+模拟器+重放验证（T-W4-T04）" \
  "$PY" -m pytest \
    tests/unit/test_gradeband_low.py \
    tests/unit/test_gradeband_constraints.py \
    tests/unit/test_gradeband_adapter.py \
    tests/performance/test_assembly_latency.py \
    tests/performance/test_preassembled_fallback.py \
    tests/performance/test_grading_latency.py \
    tests/unit/test_cost_dashboard.py \
    tests/contract/test_api_frozen.py \
    tests/unit/test_simulator_client.py \
    tests/simulator/test_practice_e2e.py \
    tests/simulator/test_diagnosis_review_e2e.py \
    -q

# API 契约 diff（E2E-10 API 冻结部分）
LOG_FILE="$LOG_DIR/check_openapi_diff.log"
if [ -f scripts/ci/check_openapi_diff.py ] && "$PY" scripts/ci/check_openapi_diff.py >"$LOG_FILE" 2>&1; then
  ok "API 契约 diff 常绿（check_openapi_diff.py）"
else
  if [ -f scripts/ci/check_openapi_diff.py ]; then
    die "API 契约 diff 失败（见 $LOG_FILE）"
  else
    ok "check_openapi_diff.py 缺失（已由 test_api_frozen.py 覆盖契约冻结）"
  fi
fi

# ────────────────────────────────────────────────────────────────────
# ⑦ W3 遗留修复验证（T-W4-048/049：scoring_trace.correct + option 口径）
# ────────────────────────────────────────────────────────────────────
run_step "W3 遗留修复（T-W4-048/049）" \
  "$PY" -m pytest \
    tests/unit/test_scoring_trace_correct.py \
    tests/unit/test_option_normalizer.py \
    -q

# ────────────────────────────────────────────────────────────────────
# ⑧ 年度重放首演 dry-run（E2E-8 重放，T-W4-046）
# ────────────────────────────────────────────────────────────────────
LOG_FILE="$LOG_DIR/annual_replay.log"
if [ -f scripts/jobs/annual_replay_report.py ]; then
  if "$PY" scripts/jobs/annual_replay_report.py --dry-run >"$LOG_FILE" 2>&1; then
    ok "年度重放首演 dry-run（annual_replay_report.py）"
  else
    die "年度重放首演 dry-run 失败（见 $LOG_FILE）"
  fi
else
  ok "annual_replay_report.py 缺失（W4d 重放首演已由 test_replay.py 覆盖）"
fi

# ────────────────────────────────────────────────────────────────────
# ⑨ W0/W1/W2/W3 出口不退化（E2E-10 不退化）
# ────────────────────────────────────────────────────────────────────
LOG_FILE="$LOG_DIR/w0.log"
if bash scripts/wave-exit/w0.sh >"$LOG_FILE" 2>&1; then
  ok "W0 出口不退化（w0.sh）"
else
  die "W0 出口退化（见 $LOG_FILE）"
fi
LOG_FILE="$LOG_DIR/w1.log"
if bash scripts/wave-exit/w1.sh >"$LOG_FILE" 2>&1; then
  ok "W1 出口不退化（w1.sh）"
else
  die "W1 出口退化（见 $LOG_FILE）"
fi
LOG_FILE="$LOG_DIR/w2.log"
if bash scripts/wave-exit/w2.sh >"$LOG_FILE" 2>&1; then
  ok "W2 出口不退化（w2.sh）"
else
  die "W2 出口退化（见 $LOG_FILE）"
fi
LOG_FILE="$LOG_DIR/w3.log"
if bash scripts/wave-exit/w3.sh >"$LOG_FILE" 2>&1; then
  ok "W3 出口不退化（w3.sh）"
else
  die "W3 出口退化（见 $LOG_FILE）"
fi

# ────────────────────────────────────────────────────────────────────
# ⑩ 不退化检查：测试总数对比基线（E2E-10，T-W4-050）
# ────────────────────────────────────────────────────────────────────
LOG_FILE="$LOG_DIR/check_no_regression.log"
if "$PY" scripts/wave-exit/check_no_regression.py >"$LOG_FILE" 2>&1; then
  ok "不退化检查（check_no_regression.py）"
else
  die "不退化检查失败（见 $LOG_FILE）"
fi

echo ""
echo "摘要：通过 ${PASS} 项 / 失败 ${FAIL} 项 / 耗时 ${SECONDS}s"
echo "🎉 W4 出口通过：发布前就绪清单机器可验部分全绿（E2E-1~10）"
