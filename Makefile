# 母题平台 · 一键命令入口
SHELL := /bin/bash
TASK ?=

# 从 .env 读取基础设施变量（compose 自检需要；密钥类变量如 DEEPSEEK_API_KEY 刻意不导出）
-include .env
# 仅当 .env 实际存在时才导出：无条件 export 会在 .env 尚未创建时（如 CI 的
# make check 首次运行）把空值导给子进程——空环境变量优先级高于 compose 的
# .env 插值（postgres 空密码秒退），也会让 alembic env.py 的 setdefault 失效
ifneq (,$(wildcard .env))
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB MINIO_ROOT_USER MINIO_ROOT_PASSWORD
endif

.PHONY: bootstrap up down migrate test accept contract golden golden-path nightly dashboard sync-rules model-bench demo-w0 demo-w2 demo-w3 setup check

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

## 组织治理基线（T-W0-009）：CI-Workflows check.yml 调用——依赖安装
setup: ; pip install -r requirements-dev.txt -r requirements.txt

## 组织治理基线（T-W0-009）：CI 全量检查（迁移自 pr-check.yml 的 PR 流水，本地亦可手动执行）
## T-W5-030/031：Go 工具链进 check（GO-1/GO-4 局部：gofmt/vet/test-race + X6 边界 lint + BAML-1 golden）
check:
	@[ -f .env ] || cp .env.example .env
	docker compose up -d --wait db
	alembic upgrade head
	python -m pytest tests/contract tests/golden -q
	if [ -d tests/unit ]; then python -m pytest tests/unit -q; fi
	GOLDEN_PATH_QUICK=1 python -m pytest tests/golden-path -q
	python tools/ci/check_sources.py
	$(MAKE) check-go
contract: ; python -m pytest tests/contract -q
golden: ; python -m pytest tests/golden -q
golden-path: ; python -m pytest tests/golden-path -q

## ── W5-R Go 工具链（T-W5-030/031；GO-1 gofmt / GO-4 test -race / X6 边界 lint / BAML-1 golden）──
go-fmt: ; @out=$$(gofmt -l cmd core registry baml_client tools 2>/dev/null); [ -z "$$out" ] || { echo "❌ gofmt 未通过:"; echo "$$out"; exit 1; }
go-build: ; go build ./... && go vet ./...
go-test: ; go test ./... -race -count=1
go-boundary: ; go run ./tools/go-lint/import-boundary
baml-golden-check: ; python3 tools/golden/baml_golden.py check
baml-golden-update: ; python3 tools/golden/baml_golden.py update
## 生成物提交入库；goimports 修复上游 codegen 边角问题（无 union 时死 import /
## type_builder 缺 fmt import，spike 实证；升级 BAML 版本时复验）
baml-generate:
	npx -y @boundaryml/baml@0.226.1 generate
	go run golang.org/x/tools/cmd/goimports@latest -w baml_client
	$(MAKE) go-fmt
check-go: go-fmt go-build go-test go-boundary baml-golden-check

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
