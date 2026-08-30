#!/usr/bin/env bash
# W5-R 波次出口：Go+BAML 重建波（可信底座）出口验收。
#
# 对照 tests/holdout/w5r.md（H-W5R-1..11）与 ADR-0004 §四出口条款：
# 1. Go 工具链门禁不退化（GO-1 gofmt / GO-4 test -race / X6 边界 / sqlc diff）。
# 2. holdout w5r 全绿：伪造证书拒发、账本 append-only、认证/授权/归属、
#    PII 无 bypass、并发幂等恰一入账、AI 台账版本指纹、gate 切 Go、
#    实证矩阵在位。
# 3. 任一失败非零退出并打印失败步骤；结尾输出摘要与耗时。
#
# 依赖：docker（db 服务）、Go 工具链；w5r 的 DB 探针需要活库。
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

echo "== W5-R 出口验收（Go+BAML 重建波 · 可信底座）=="

# ── ① Go 门禁不退化 ──────────────────────────────────────────────────
go build ./... || die "go build 失败"
ok "go build ./..."
go vet ./... || die "go vet 失败"
ok "go vet ./..."
dirs=$(go list -f '{{.Dir}}' ./...) || die "go list 失败"
[ -n "$dirs" ] || die "包目录列表为空（GO-1 检查面失效）"
out=$(gofmt -l $dirs) && [ -z "$out" ] || { echo "$out"; die "gofmt 未通过（GO-1）"; }
ok "gofmt 零 diff（GO-1）"
go run ./tools/go-lint/import-boundary || die "import 边界违规（X6/GO-3）"
ok "core 零学科特判（X6/GO-3）"
if [ -x tools/bin/sqlc.exe ]; then
  tools/bin/sqlc.exe diff >/dev/null 2>&1 && ok "sqlc diff 等价（SQL-2）" || die "sqlc 生成物漂移（SQL-2）"
fi

# ── ② 基础设施就绪（DB 探针需要活库） ────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  [ -f .env ] || cp .env.example .env
  docker compose up -d --wait db || die "db 服务未能就绪"
  ok "PostgreSQL 16 就绪"
else
  die "需要 Docker（w5r holdout 的 DB 探针依赖活库）"
fi

# ── ③ 单测不退化 ─────────────────────────────────────────────────────
go test ./... -race -count=1 || die "go test -race 失败（GO-4）"
ok "go test ./... -race（GO-4）"

# ── ④ holdout w5r（H-W5R-1..11 唯一验收入口） ────────────────────────
python tools/ci/run_holdout.py tests/holdout/w5r.md || die "holdout w5r 存在 FAIL 条目"
ok "holdout w5r（H-W5R-1..11）"

echo ""
echo "摘要：通过 ${PASS} 项 / 失败 ${FAIL} 项 / 耗时 ${SECONDS}s"
[ "$FAIL" -eq 0 ]
