#!/usr/bin/env bash
# T-W5-031 专属验收：Go 骨架（gofmt/build/vet/test-race + fuzz 目标存在性
# + X6 import 边界红绿实证 + healthz 脱敏断言存在性）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "== GO-1 gofmt =="
out=$(gofmt -l cmd core api packs registry baml_client tools 2>/dev/null)
[ -z "$out" ] || { echo "❌ gofmt 未通过: $out"; exit 1; }

echo "== GO-1 build/vet =="
go build ./... && go vet ./...

echo "== GO-4 test -race（含 fuzz 种子语料）=="
go test ./... -race -count=1

echo "== 原生 fuzz 目标存在（BRIEF 技术基线）=="
grep -rq 'func Fuzz.*testing.F' core api cmd registry || {
  echo "❌ 未找到任何 Fuzz 目标"; exit 1; }

echo "== X6/GO-3 边界 lint：绿（当前代码）=="
go run ./tools/go-lint/import-boundary

echo "== X6/GO-3 边界 lint：红（注入 core→packs 违规必须被拦）=="
# mktemp 唯一文件名（O_EXCL 创建，后缀 .go 供 lint 扫描）：不覆盖也不在
# 退出时误删工作树里任何既有文件，清理只作用于本次创建的文件
VIOLATION=$(mktemp --suffix=.go core/gate/zz_boundary_violation_gen.XXXXXX)
trap 'rm -f "$VIOLATION"' EXIT
cat > "$VIOLATION" <<'EOF'
package gate

// 验收脚本临时注入的违规文件（结束后删除）：core 不得 import packs。
import _ "github.com/Cloudbird-Software/AI_Web_School/packs"
EOF
if go run ./tools/go-lint/import-boundary >/dev/null 2>&1; then
  echo "❌ 边界 lint 未拦截 core→packs 违规"; exit 1
fi
echo "✅ 违规被正确拦截"

echo "== healthz 脱敏断言存在（对齐 T-W0-010 语义）=="
grep -q 'len(body) != 1' api/api_test.go || {
  echo "❌ healthz 最小字段断言缺失"; exit 1; }

echo "✅ T-W5-031 专属验收通过"
