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

.PHONY: bootstrap up down migrate migrate-go migrate-go-check test accept contract golden golden-path nightly dashboard sync-rules model-bench demo-w0 demo-w2 demo-w3 setup check test-freeze-check holdout go-errcheck sql-pairs sqlc-generate sqlc-diff

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

## T-W5-032 Go 侧迁移执行器（golang-migrate 双轨期与 alembic 并行，ADR-0004 §四）
migrate-go: ; go run ./tools/migrate -dsn "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:5432/$(POSTGRES_DB)" up

## T-W5-032 全链校验（验收 #1–#4）：成对性 + alembic/go parity + down/up 全量
## + append-only 探针。用独立临时 PG16 实例：0014 的 pii_vault_reader 是集群
## 级角色，若与别库（如 make check 已 upgrade 的 $(POSTGRES_DB)）的 ACL 依赖
## 共存，down 0014 的 DROP ROLE 会因跨库共享依赖失败
MIGCHECK_CONTAINER := mig-check-pg
MIGCHECK_PORT := 55432
migrate-go-check:
	@command -v docker >/dev/null || { echo "❌ migrate-go-check 需要 Docker"; exit 1; }
	@trap 'docker rm -f $(MIGCHECK_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	docker run -d --rm --name $(MIGCHECK_CONTAINER) -e POSTGRES_PASSWORD=migrate-check \
	  -p 127.0.0.1:$(MIGCHECK_PORT):5432 \
	  postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777 >/dev/null; \
	docker inspect -f '{{.State.Running}}' $(MIGCHECK_CONTAINER) 2>/dev/null | grep -q true \
	  || { echo "❌ 临时 PG 容器未启动"; exit 1; }; \
	python tools/sql/migrate_check.py \
	  --admin-dsn "postgresql://postgres:migrate-check@localhost:$(MIGCHECK_PORT)/postgres" \
	  --pg-dump "docker exec -i $(MIGCHECK_CONTAINER) pg_dump -U postgres"
# 注：就绪等待在 migrate_check.py 的 wait_for_server（TCP 层）。不能用容器内
# pg_isready（unix socket）探测——官方镜像 initdb 阶段的临时服务器仅监听
# socket，会误报就绪（CI 实证：竞态导致 check 挂在"临时 PG 未就绪"）。

## 测试与验收
test: ; python -m pytest tests/ -x -q

## 组织治理基线（T-W0-009）：CI-Workflows check.yml 调用——依赖安装
## T-W0-010：--require-hashes 哈希锁定安装（Scorecard Pinned-Dependencies）
setup: ; pip install --require-hashes -r requirements-dev.txt -r requirements.txt

## 组织治理基线（T-W0-009）：CI 全量检查（迁移自 pr-check.yml 的 PR 流水，本地亦可手动执行）
## T-W5-030/031：Go 工具链进 check（GO-1/GO-4 局部：gofmt/vet/test-race + X6 边界 lint + BAML-1 golden）
## T-W5-032：migrate-go-check 进 check（验收 #5：SQL 迁移 parity/可逆/append-only 在 PR 阶段拦截）
check:
	@[ -f .env ] || cp .env.example .env
	docker compose up -d --wait db
	alembic upgrade head
	python -m pytest tests/contract tests/golden -q
	if [ -d tests/unit ]; then python -m pytest tests/unit -q; fi
	GOLDEN_PATH_QUICK=1 python -m pytest tests/golden-path -q
	python tools/ci/check_sources.py
	$(MAKE) check-go
	$(MAKE) sql-pairs
	$(MAKE) migrate-go-check
contract: ; python -m pytest tests/contract -q
golden: ; python -m pytest tests/golden -q
golden-path: ; python -m pytest tests/golden-path -q

## ── W5-R Go 工具链（T-W5-030/031；GO-1 gofmt / GO-4 test -race / X6 边界 lint / BAML-1 golden）──
## GO-1：gofmt 必须吃目录路径。`go list ./...` 给的是 import 路径——gofmt 对每个
## import 路径 lstat 失败但整体 exit 0（CI 实证：全部 no such file 仍绿=门空转，
## verify PR #71 cycle B 抓获），必须 -f {{.Dir}} 取真实目录且空目录列表本身即红。
go-fmt:
	@dirs=$$(go list -f '{{.Dir}}' ./...) || { echo "❌ go list 失败"; exit 1; }; 	[ -n "$$dirs" ] || { echo "❌ 包目录列表为空——GO-1 检查面失效"; exit 1; }; 	out=$$(gofmt -l $$dirs); [ -z "$$out" ] || { echo "❌ gofmt 未通过:"; echo "$$out"; exit 1; }
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
## T-W5-033 GO-2：errcheck 全仓扫描（baml_client 为 BAML 生成物，随 baml-generate 再生，豁免留痕）
go-errcheck:
	@go tool errcheck $$(go list ./... | grep -v baml_client)
## T-W5-033 SQL-1 静态面：up/down 成对 + 非空 down + 版本号唯一（运行时可逆由 migrate-go-check 承担）
sql-pairs: ; python tools/sql/check_pairs.py
## T-W5-033 SQL-2：sqlc 为版本+SHA256 双钉扎的发布二进制（非 go.mod 依赖）。
## 为什么：sqlc v1.31.1 传递树含 grpc v1.80.0——GHSA-hrxh-6v49-42gf（高危）唯一
## 修复版 v1.82.1 发布 <90 天，组织供应链 age≥90 硬红与漏洞高危硬红在该传递依赖上
## 互斥（dep-review.yml 无 allow-ghsas 透传）。二进制+SHA256 钉扎与 baml-generate 的
## npx@精确版本同构，模块图零污染；grpc v1.82.1 满 90 天（2026-10-13 后）可评估
## 回迁 go tool 指令并删除本段注释。
SQLC_VERSION := v1.31.1
SQLC_BIN := tools/bin/sqlc.exe
ifeq ($(shell uname -s 2>/dev/null | grep -qi linux && echo yes),yes)
  SQLC_OS := linux
  SQLC_PKG := sqlc_1.31.1_linux_amd64.tar.gz
  SQLC_SHA256 := 497ae4fcdfa64c5b0c311ffe4c2bd991e43991e82e5367792ed78bc2dca27354
else ifeq ($(shell uname -s 2>/dev/null | grep -qi darwin && echo yes),yes)
  SQLC_OS := darwin
  $(error darwin 校验和未钉扎——请补充 sqlc_1.31.1_darwin_amd64.tar.gz 的 SHA256 后使用)
else
  SQLC_OS := windows
  SQLC_PKG := sqlc_1.31.1_windows_amd64.zip
  SQLC_SHA256 := 352711fa7dcb05dcdfefca0ad71b2c9a74fd090f8d7fc609419de4cbc725429f
endif
tools/bin/sqlc.exe:
	@mkdir -p tools/bin .sqlc_tmp
	curl -fsSL -o .sqlc_tmp/$(SQLC_PKG) "https://github.com/sqlc-dev/sqlc/releases/download/$(SQLC_VERSION)/$(SQLC_PKG)"
	echo "$(SQLC_SHA256)  .sqlc_tmp/$(SQLC_PKG)" | sha256sum -c -
ifeq ($(SQLC_OS),linux)
	tar -xzf .sqlc_tmp/$(SQLC_PKG) -C tools/bin sqlc
	mv tools/bin/sqlc tools/bin/sqlc.exe
else
	unzip -o -q .sqlc_tmp/$(SQLC_PKG) -d tools/bin
endif
	@rm -rf .sqlc_tmp
	@chmod +x tools/bin/sqlc.exe 2>/dev/null || true
sqlc-generate: tools/bin/sqlc.exe ; tools/bin/sqlc.exe generate -f sqlc.yaml
sqlc-diff: tools/bin/sqlc.exe ; tools/bin/sqlc.exe diff -f sqlc.yaml
## sqlc-diff 置于链首：生成物漂移是最上游的身份问题——先判定再谈编译/静态检查，
## 避免漂移导致的编译/errcheck 失败掩盖根因（红队 Major 2）
check-go: sqlc-diff go-fmt go-build go-test go-boundary baml-golden-check go-errcheck

## 任务验收：唯一完成标准
accept: ## make accept TASK=T-W0-001
	@[ -n "$(TASK)" ] || { echo "用法: make accept TASK=<id>"; exit 1; }
	bash tools/make_accept.sh $(TASK)

## T-W5-034 测试冻结校验（specs/test-freeze/；受保护测试资产被篡改/删除/未登记即红）
test-freeze-check: ; python tools/ci/check_test_freeze.py

## T-W5-034 端到端 Holdout（人类意图的效果测试；先校验冻结完整性再执行）
WAVE ?=
holdout: ## make holdout WAVE=w5r|w6|w7|w8|final
	@[ -n "$(WAVE)" ] || { echo "用法: make holdout WAVE=<w5r|w6|w7|w8|final>"; exit 1; }
	python tools/ci/check_test_freeze.py
	python tools/ci/run_holdout.py tests/holdout/$(WAVE).md

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
