#!/usr/bin/env bash
# W6 波次出口：引擎解锁波（题目生产飞轮）出口验收。
#
# 对照 tests/holdout/w6.md（H-W6-1..6）与 tasks/w6/BRIEF.md §W6 端到端出口：
# 1. 生成管线产能烟测：mathgen 批量可执行（确定性档不依赖 LLM 供给）。
# 2. holdout w6 全绿：量产达标/语数英半确定全链/台账在位/三源分离/预算熔断。
#    H-W6-2/3（LLM 评价者全链）与 H-W6-6（预算熔断端点）依赖生产 LLM 供给
#    与 ops 端点落地——未落地前本脚本如实红，不降级口径（宪法 A8）。
# 3. 任一失败非零退出并打印失败步骤；结尾输出摘要与耗时。
#
# 依赖：docker（db 服务）、Go 工具链；H-W6-2/3/6 需要服务就绪与
# HOLDOUT_TOKEN_OPS（见 w6.md 条目内说明）。
set -euo pipefail

PASS=0
FAIL=0
SECONDS=0

ok(){ echo "✅ $1"; PASS=$((PASS+1)); }
die(){
  FAIL=$((FAIL+1))
  echo "❌ $1"
  echo ""
  echo "摘要：通过 ${PASS} 项 / 失败 ${FAIL} 项 / 耗时 ${SECONDS}s"
  exit 1
}

echo "== W6 出口验收（引擎解锁波 · 题目生产飞轮）=="

# ── ① 基础设施就绪（飞轮探针需要活库） ──────────────────────────────
if command -v docker >/dev/null 2>&1; then
  [ -f .env ] || cp .env.example .env
  docker compose up -d --wait db || die "db 服务未能就绪"
  ok "PostgreSQL 16 就绪"
else
  die "需要 Docker（w6 holdout 的 DB 探针依赖活库）"
fi

# ── ② 迁移至 head（飞轮探针按最新 schema 取证） ─────────────────────
set -a; . ./.env; set +a
go run ./tools/migrate -dsn "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}" up \
  || die "Go 侧迁移未达 head"
ok "迁移至 head（golang-migrate）"

# ── ③ 数学确定性档产能烟测（量产机器可执行） ────────────────────────
go run ./cmd/mathgen -templates all -n 30 -out out/w6-exit/ || die "mathgen 批量失败（确定性产能面）"
ok "mathgen 10 母题 × 30 实例批量（同 seed 可回放）"

# ── ④ holdout w6（H-W6-1..6 唯一验收入口） ──────────────────────────
python tools/ci/run_holdout.py tests/holdout/w6.md || die "holdout w6 存在 FAIL 条目"
ok "holdout w6（H-W6-1..6）"

echo ""
echo "摘要：通过 ${PASS} 项 / 失败 ${FAIL} 项 / 耗时 ${SECONDS}s"
[ "$FAIL" -eq 0 ]
