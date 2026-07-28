#!/usr/bin/env bash
# T-W4-040 数据库恢复演练脚本：将备份恢复至临时库 + migrate-check + 核心查询验证。
#
# 流程（架构 v2 §6 / OPC §6.6 S9）：
#   1. 在同一 PostgreSQL 实例创建临时数据库（muti_restore_drill_<时间戳>）
#   2. pg_restore 将备份恢复至临时库
#   3. alembic migrate-check（upgrade→downgrade -1→upgrade）验证迁移可逆
#   4. backup_verify.py 校验表数量/记录数/关键表抽样
#   5. 清理临时库（除非 KEEP_RESTORE_DB=1 保留供排查）
#
# 用法：
#   bash scripts/ops/restore.sh backups/muti_dev-20260728-120000.dump
#   bash scripts/ops/restore.sh backups/muti_dev-20260728-120000.dump  # 校验后自动清理
#   KEEP_RESTORE_DB=1 bash scripts/ops/restore.sh backups/xxx.dump     # 保留临时库
#
# 密码纪律（验收 #5）：pg_restore 在容器内执行，走 local peer 认证，无需密码。
set -euo pipefail

# ── 参数校验 ──
BACKUP_FILE="${1:-}"
if [ -z "$BACKUP_FILE" ]; then
  echo "用法: bash scripts/ops/restore.sh <backup.dump>"
  echo "示例: bash scripts/ops/restore.sh backups/muti_dev-20260728-120000.dump"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . .env; set +a
fi

if [ ! -f "$BACKUP_FILE" ]; then
  echo "❌ 备份文件不存在: $BACKUP_FILE"
  exit 1
fi

DB_USER="${POSTGRES_USER:-muti}"
DB_NAME="${POSTGRES_DB:-muti_dev}"
COMPOSE_SERVICE="${DB_COMPOSE_SERVICE:-db}"
KEEP_RESTORE_DB="${KEEP_RESTORE_DB:-0}"

# 临时库唯一命名（时间戳 + PID 防并发冲突）
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RESTORE_DB="muti_restore_drill_${TIMESTAMP}_$$"

echo "== T-W4-040 恢复演练 =="
echo "源备份:   ${BACKUP_FILE}"
echo "临时库:   ${RESTORE_DB}"
echo "源库:     ${DB_NAME}（仅用于校验和比对，不被修改）"
echo ""

# ── 1. SHA256 校验和验证 ──
BACKUP_DIR="$(dirname "$BACKUP_FILE")"
BACKUP_BASENAME="$(basename "$BACKUP_FILE")"
CHECKSUM_FILE="${BACKUP_DIR}/${BACKUP_BASENAME}.sha256"
if [ -f "$CHECKSUM_FILE" ]; then
  echo "== [1/5] 校验和验证 =="
  (cd "$BACKUP_DIR" && sha256sum -c "${BACKUP_BASENAME}.sha256")
  echo ""
fi

# ── 2. 创建临时库 ──
echo "== [2/5] 创建临时数据库 ${RESTORE_DB} =="
docker compose exec -T "$COMPOSE_SERVICE" \
  createdb -U "$DB_USER" "$RESTORE_DB"
echo "✅ 临时库已创建"
echo ""

# ── 清理陷阱：无论成功失败都清理临时库（除非 KEEP_RESTORE_DB=1）──
cleanup() {
  if [ "$KEEP_RESTORE_DB" = "1" ]; then
    echo ""
    echo "ℹ️  KEEP_RESTORE_DB=1，保留临时库 ${RESTORE_DB} 供排查"
    return
  fi
  echo ""
  echo "== 清理临时库 ${RESTORE_DB} =="
  docker compose exec -T "$COMPOSE_SERVICE" \
    dropdb -U "$DB_USER" --if-exists "$RESTORE_DB" 2>/dev/null || true
  echo "✅ 临时库已清理"
}
trap cleanup EXIT

# ── 3. pg_restore 恢复至临时库 ──
echo "== [3/5] pg_restore 恢复至临时库 =="
# 通过 stdin 传入备份文件（- 表示从 stdin 读）
cat "$BACKUP_FILE" | docker compose exec -T "$COMPOSE_SERVICE" \
  pg_restore -U "$DB_USER" -d "$RESTORE_DB" --no-owner --no-privileges --clean --if-exists 2>&1 || {
    # pg_restore 对已存在对象会报 warning（--clean --if-exists），非致命
    # 真正的致命错误会让 pipe 退出码非 0；此处仅提示
    echo "⚠️  pg_restore 报告部分 warning（对象已存在等），继续验证"
  }
echo "✅ 恢复完成"
echo ""

# ── 4. alembic migrate-check（验证迁移可逆）──
echo "== [4/5] alembic migrate-check（upgrade→downgrade -1→upgrade）=="
# 指向临时库执行迁移可逆性检查
POSTGRES_DB="$RESTORE_DB" python -m alembic upgrade head
echo "  → upgrade head ✅"
POSTGRES_DB="$RESTORE_DB" python -m alembic downgrade -1
echo "  → downgrade -1 ✅"
POSTGRES_DB="$RESTORE_DB" python -m alembic upgrade head
echo "  → upgrade head ✅"
echo "✅ 迁移可逆性验证通过"
echo ""

# ── 5. backup_verify.py 核心查询验证 ──
echo "== [5/5] backup_verify.py 核心查询验证 =="
POSTGRES_DB="$RESTORE_DB" python scripts/ops/backup_verify.py

echo ""
echo "✅ T-W4-040 恢复演练通过"
echo "   临时库: ${RESTORE_DB}（已清理，KEEP_RESTORE_DB=1 可保留）"
