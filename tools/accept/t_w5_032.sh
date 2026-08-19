#!/usr/bin/env bash
# T-W5-032 专属验收：迁移移植全链。
# 验收 #1 成对性 / #2 down-up 全量可逆 / #3 alembic parity / #4 append-only
# 行为——四项全部由 tools/sql/migrate_check.py 一次跑完；
# #5 CI 接线由本脚本结构性检查（make check 必须调用 migrate-go-check）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "== 验收 #1–#4：全链校验（成对性 / parity / down→up / append-only 探针）=="
if [ -n "${MIGCHECK_ADMIN_DSN:-}" ]; then
  # 直连既有 PostgreSQL（无 docker 的开发机/沙箱：自备 PG，DSN 指向管理员库）。
  # 注意：0014 的 pii_vault_reader 是集群级角色，集群里若有其他库已 apply
  # 0014（如测试用 muti_dev），down 全量的 DROP ROLE 会因跨库 ACL 依赖失败——
  # 直连模式要求目标集群不被其他已迁移库共用
  python tools/sql/migrate_check.py --admin-dsn "$MIGCHECK_ADMIN_DSN"
else
  # 默认路径：独立临时 PG16 实例（digest 与 compose 同锚定）
  make migrate-go-check
fi

echo "== 验收 #5：migrate-go-check 已接入 make check（PR 阶段拦截）=="
# #43：目标级解析——只认 check: 的 recipe 块，注释/其他目标里的同形文本不再放行
CHECK_RECIPE=$(awk '/^check:/{f=1;next} /^[a-zA-Z_-]+:/{f=0} f' Makefile)
echo "$CHECK_RECIPE" | grep -q 'migrate-go-check' || {
  echo "❌ make check 的 recipe 未调用 migrate-go-check"; exit 1; }

echo "== 迁移执行器可构建（golang-migrate 锁版本接入）=="
go build ./tools/migrate && go vet ./tools/migrate

echo "✅ T-W5-032 专属验收通过"
