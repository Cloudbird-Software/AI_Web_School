# 母题平台 · 一键命令入口
SHELL := /bin/bash
TASK ?=

# 从 .env 读取基础设施变量（compose 自检需要；密钥类变量如 DEEPSEEK_API_KEY 刻意不导出）
-include .env
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB MINIO_ROOT_USER MINIO_ROOT_PASSWORD

.PHONY: bootstrap up down migrate test accept contract golden golden-path nightly dashboard sync-rules model-bench demo-w0 demo-w2 demo-w3

## 环境一键搭建与自检（新机器第一步）
bootstrap:
	@command -v docker >/dev/null || { echo "❌ 需要 Docker"; exit 1; }
	@[ -f .env ] || { cp .env.example .env; echo "已生成 .env，请填入密钥后重跑"; exit 1; }
	docker compose up -d --wait
	@echo "== 自检 =="
	@docker compose exec -T db pg_isready -U $$POSTGRES_USER >/dev/null && echo "✅ PostgreSQL" || { echo "❌ PostgreSQL"; exit 1; }
	@docker compose exec -T redis redis-cli ping | grep -q PONG && echo "✅ Redis" || { echo "❌ Redis"; exit 1; }
	@curl -sf http://localhost:9000/minio/health/live >/dev/null && echo "✅ MinIO" || { echo "❌ MinIO"; exit 1; }
	python -V | grep -q "3.12" && echo "✅ Python 3.12" || echo "⚠️ 建议 Python 3.12"
	@echo "✅ bootstrap 完成"

up: ; docker compose up -d --wait
down: ; docker compose down

## 数据库迁移（一切 DDL 走这里，禁止手工改库）
migrate: ; alembic upgrade head
migrate-check: ; alembic upgrade head && alembic downgrade -1 && alembic upgrade head && echo "✅ 迁移可逆"

## 测试与验收
test: ; python -m pytest tests/ -x -q
contract: ; python -m pytest tests/contract -q
golden: ; python -m pytest tests/golden -q
golden-path: ; python -m pytest tests/golden-path -q

## 任务验收：唯一完成标准
accept: ## make accept TASK=T-W0-001
	@[ -n "$(TASK)" ] || { echo "用法: make accept TASK=<id>"; exit 1; }
	bash tools/make_accept.sh $(TASK)

## 仪表盘与模型基准赛
dashboard: ; python tools/opc dashboard
model-bench: ; python tools/bench/run.py --suite tests/model-bench/suite.yaml $(MODEL:%=--model %)

## 规则同步：.agent/rules/ → IDE 规则文件
sync-rules: ; bash tools/sync_rules.sh

## W0 出口演示
demo-w0: ; bash scripts/wave-exit/w0.sh

## W2 出口演示与门禁检查（E2E-9 唯一验收入口）
demo-w2: ; bash scripts/wave-exit/w2.sh

## W3 出口演示与门禁检查（学生侧闭环唯一验收入口）
demo-w3: ; bash scripts/wave-exit/w3.sh

nightly: migrate-check contract golden golden-path
