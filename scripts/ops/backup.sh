#!/usr/bin/env bash
# T-W4-040 数据库备份脚本：pg_dump 全量逻辑备份（schema + 数据）+ SHA256 校验和。
#
# 设计要点（架构 v2 §6 / OPC §6.6 S9）：
# - 通过 docker compose exec 在容器内执行 pg_dump（local socket 免密，密码不入脚本/日志）。
# - 自定义压缩格式 -Fc（单文件、压缩、支持并行恢复与选择性恢复）。
# - --no-owner --no-privileges：恢复至临时库时不携带原库 owner/权限，避免环境差异。
# - 输出文件 + .sha256 校验和文件，恢复前可校验完整性。
#
# 用法：
#   bash scripts/ops/backup.sh                  # 备份 POSTGRES_DB（默认从 .env 读）
#   BACKUP_DIR=/tmp bak bash scripts/ops/backup.sh  # 自定义备份目录
#
# 密码纪律（验收 #5）：不在脚本硬编码密码；容器内 pg_dump 走 local peer 认证，
# 无需 PGPASSWORD。若需远程备份，通过环境变量 PGPASSWORD 传入（不写入文件）。
set -euo pipefail

# ── 加载 .env（POSTGRES_* 等基础设施变量）──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . .env; set +a
fi

DB_USER="${POSTGRES_USER:-muti}"
DB_NAME="${POSTGRES_DB:-muti_dev}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"
COMPOSE_SERVICE="${DB_COMPOSE_SERVICE:-db}"
BACKUP_DIR="${BACKUP_DIR:-backups}"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}-${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"

echo "== T-W4-040 数据库备份 =="
echo "数据库: ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "输出:   ${BACKUP_FILE}"
echo ""

# ── pg_dump（自定义压缩格式 -Fc）──
# 通过 docker compose exec 在容器内执行；-T 禁用 TTY 以支持 stdout 重定向。
docker compose exec -T "$COMPOSE_SERVICE" \
  pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner --no-privileges \
  > "$BACKUP_FILE"

BACKUP_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE")

# ── SHA256 校验和 ──
cd "$BACKUP_DIR"
BACKUP_BASENAME="$(basename "$BACKUP_FILE")"
sha256sum "$BACKUP_BASENAME" > "${BACKUP_BASENAME}.sha256"
CHECKSUM=$(awk '{print $1}' "${BACKUP_BASENAME}.sha256")

echo ""
echo "✅ 备份完成"
echo "   文件:   ${BACKUP_FILE}"
echo "   大小:   ${BACKUP_SIZE} bytes"
echo "   SHA256: ${CHECKSUM}"
echo "   校验和: ${BACKUP_FILE}.sha256"
