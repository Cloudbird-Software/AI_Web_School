# W5-R Holdout（Go+BAML 重建波 · 可信底座）

> 意图（人类视角）：**重写之后，这个系统的"可信"不是宣称出来的，而是攻击不进去的。**
> 伪证书发不了版、历史账改不动、陌生人进不来、未成年人没被授权就一个字节都写不进、PII 出事时宁可不干活也不泄密。
> 本文件与 `tasks/w5/BRIEF.md` 的 E2E-1..11 逐条对应，但只从外部效果验证，不依赖实现细节。
> 前置：`docker compose up -d --wait`、Go 服务已启动且迁移就绪；`HOLDOUT_BASE_URL`（默认 http://localhost:8080）；需要测试主体的条目用 `HOLDOUT_TOKEN` / `HOLDOUT_ALIAS` 注入。

## H-W5R-1 伪造证书直写发布被数据库拒绝
- 意图：铁律 2（未过校验门的产物禁止入已发布区）；E2E-1
- 类型：machine

```bash
docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA \
  -c "INSERT INTO item_version (item_version_id, item_id, status, objective, interaction_ref, content, scoring_ref, error_bindings, lineage, gate_certificate_id, published_at)
      VALUES ('iv_HOLDOUT_FAKE', 'it_HOLDOUT_FAKE', 'published', '{}', '{}', '{}', '{}', '{}', '{}', 'cert_FAKE', now())" \
  2>&1 | grep -qiE 'ERROR|violates'
```

## H-W5R-2 版本账 UPDATE/DELETE（含 WHERE FALSE）全部被判死刑
- 意图：铁律 1（三本账只增不改）；E2E-2
- 类型：machine

```bash
for t in item_version material_version corpus_version item_template_version; do
  docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA \
    -c "UPDATE $t SET created_at = created_at WHERE FALSE" 2>&1 | grep -qiE 'ERROR' \
    || { echo "$t UPDATE WHERE FALSE 未被拒绝"; exit 1; }
  docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA \
    -c "DELETE FROM $t WHERE FALSE" 2>&1 | grep -qiE 'ERROR' \
    || { echo "$t DELETE WHERE FALSE 未被拒绝"; exit 1; }
done
```

## H-W5R-3 无凭证访问在线链路一律 401/403
- 意图：铁律 8（每个请求必须有已认证主体）；E2E-3
- 类型：machine

```bash
BASE="${HOLDOUT_BASE_URL:-http://localhost:8080}"
for probe in "POST /sessions" "POST /sessions/sess_HOLDOUT/responses" "GET /reports/weakness/alias_HOLDOUT"; do
  m="${probe%% *}"; p="${probe#* }"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json' -d '{}')
  [ "$code" = "401" ] || [ "$code" = "403" ] || { echo "$m $p 返回 $code（期望 401/403）"; exit 1; }
done
```

## H-W5R-4 越权访问他人 alias 数据被拒，且响应不泄露服务端凭证
- 意图：铁律 8（学生只能访问自己 alias 的数据；凭证永不回传）；E2E-3
- 类型：machine

```bash
BASE="${HOLDOUT_BASE_URL:-http://localhost:8080}"
[ -n "${HOLDOUT_TOKEN:-}" ] && [ -n "${HOLDOUT_OTHER_ALIAS:-}" ] || { echo "需要 HOLDOUT_TOKEN 与 HOLDOUT_OTHER_ALIAS（属另一学生的 alias）"; exit 1; }
code=$(curl -s -o /tmp/holdout_w5r4.body -w '%{http_code}' "$BASE/reports/weakness/$HOLDOUT_OTHER_ALIAS" -H "Authorization: Bearer $HOLDOUT_TOKEN")
[ "$code" = "403" ] || { echo "越权访问返回 $code（期望 403）"; exit 1; }
! grep -qiE '(api[_-]?key|secret|password|token)["'"'"']?\s*[:=]' /tmp/holdout_w5r4.body || { echo "响应体疑似泄露服务端凭证"; exit 1; }
```

## H-W5R-5 无家长授权的 alias 开局被拒且零写入
- 意图：铁律（家长授权硬约束，合规降级=任务失败）；E2E-4
- 类型：machine

```bash
BASE="${HOLDOUT_BASE_URL:-http://localhost:8080}"
[ -n "${HOLDOUT_TOKEN_NOCONSENT:-}" ] || { echo "需要 HOLDOUT_TOKEN_NOCONSENT（无家长授权主体的测试凭证）"; exit 1; }
before=$(docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c "SELECT count(*) FROM response_event")
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/sessions" -H "Authorization: Bearer $HOLDOUT_TOKEN_NOCONSENT" -H 'Content-Type: application/json' -d '{"kind":"practice"}')
[ "$code" = "403" ] || { echo "未授权开局返回 $code（期望 403）"; exit 1; }
after=$(docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c "SELECT count(*) FROM response_event")
[ "$before" = "$after" ] || { echo "被拒请求产生了 response_event 写入（$before → $after）"; exit 1; }
```

## H-W5R-6 PII 保护无逃生门：全仓不存在 bypass 开关
- 意图：铁律 6（剥离失败 fail-closed，禁止降级放行开关）；E2E-5 静态面
- 类型：machine

```bash
! grep -rniE '(bypass|disable|skip)[_-]?(pii|redact|mask)|pii[_-]?(bypass|off)' \
  cmd core api packs registry tools baml_src \
  --include='*.go' --include='*.baml' --include='*.py' 2>/dev/null \
  | grep -vE 'check_test_freeze|holdout' || { echo "发现疑似 PII bypass 开关"; exit 1; }
```

## H-W5R-7 同一会话并发重复提交：恰好 1 条事件、恰好推进 1 步
- 意图：铁律 9（写入端点幂等且对并发加锁）；E2E-6
- 类型：machine

```bash
BASE="${HOLDOUT_BASE_URL:-http://localhost:8080}"
[ -n "${HOLDOUT_TOKEN:-}" ] && [ -n "${HOLDOUT_SESSION:-}" ] || { echo "需要 HOLDOUT_TOKEN 与进行中的 HOLDOUT_SESSION"; exit 1; }
before=$(docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c "SELECT count(*) FROM response_event WHERE session_id='$HOLDOUT_SESSION'")
seq 10 | xargs -P10 -I{} curl -s -o /dev/null -X POST "$BASE/sessions/$HOLDOUT_SESSION/responses" \
  -H "Authorization: Bearer $HOLDOUT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"item_version_id":"iv_HOLDOUT_CONC","answer":{"choice":"A"},"idempotency_key":"holdout-conc-1"}'
after=$(docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c "SELECT count(*) FROM response_event WHERE session_id='$HOLDOUT_SESSION'")
[ "$((after - before))" = "1" ] || { echo "并发重复提交产生 $((after - before)) 条事件（期望恰好 1）"; exit 1; }
```

## H-W5R-8 AI 评分可回放：版本指纹随分数入账
- 意图：铁律 10（AI 评分必须可回放）；E2E-7
- 类型：machine

```bash
docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c \
  "SELECT count(*) FROM score_run WHERE scoring_trace->>'model_version' IS NULL OR scoring_trace->>'prompt_version' IS NULL" \
  | grep -qx '0' || { echo "存在缺 model_version/prompt_version 的评分记录"; exit 1; }
```

## H-W5R-9 内容查重真实生效
- 意图：X11 反面（校验门不能是摆设）；E2E-8
- 类型：machine

```bash
docker compose exec -T db psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c \
  "SELECT count(*) FROM gate_verdict WHERE detail->>'validator' = 'dedup'" | grep -vqE '^0$' \
  || { echo "查重验证器从未产出任何判定记录（疑似未接入或伪造命中）"; exit 1; }
```

## H-W5R-10 gate 已切 Go 工具链且全部在位
- 意图：E2E-9（CI 可信度）；GO-1..5 / BAML-1 / SQL-1/2
- 类型：machine

```bash
grep -q 'go-fmt\|gofmt' Makefile && grep -q 'errcheck\|go-vet\|go vet' Makefile \
  && grep -q '\-race' Makefile && grep -q 'goleak' Makefile Makefile.* tools -r 2>/dev/null \
  && grep -q 'baml-golden' Makefile && grep -q 'migrate-go-check' Makefile \
  || { echo "Go 工具链检查未全部进入 Makefile/gate"; exit 1; }
grep -q 'check-go' .github/workflows/ci.yml || grep -q 'make check' .github/workflows/ci.yml \
  || { echo "CI 未执行 Go 检查链"; exit 1; }
```

## H-W5R-11 实证矩阵存在且被 CI 校验
- 意图：A8/P9（宪法即测试，宣称必须有实证）；E2E-10
- 类型：machine

```bash
grep -q '实证' specs/contracts/TRACEABILITY.md || { echo "TRACEABILITY.md 未含实证矩阵"; exit 1; }
for clause in A8 D9 D10 D11 P9 X11 X12 X13; do
  grep -q "$clause" specs/contracts/TRACEABILITY.md || { echo "实证矩阵缺条款 $clause"; exit 1; }
done
grep -rq 'TRACEABILITY' .github/workflows/ Makefile tools/ || { echo "实证矩阵无 CI/工具校验"; exit 1; }
```

## H-W5R-12 不退化：W0–W4 出口语义在 Go 实现下等价全绿
- 意图：E2E-11（重写不许丢既有能力）
- 类型：machine

```bash
for w in w0 w1 w2 w3 w4; do
  bash "scripts/wave-exit/$w.sh" >/dev/null || { echo "$w 出口脚本红"; exit 1; }
done
```

## H-W5R-13 owner 确认：测试冻结的人类闸已开启
- 意图：specs/test-freeze/README.md §二.3（机器标记可被伪造，人审才是终线）
- 类型：human
- 确认要点：仓库分支保护/org ruleset 已开启 require_code_owner_review；用一个测试 PR 验证 cloudbrid-agent App 触碰 tests/ 无法自合。

## H-W5R-14 owner 抽查：任意 3 条内容版本可回答三问
- 意图：issue #34 §二.2（从哪来/谁验证/哪个版本）；V2
- 类型：human
- 确认要点：随机抽 3 条已发布 item_version，owner 仅凭库内台账回答"来源（public/synthetic/real）、验证证据（门证书）、版本指纹（prompt/模型/评分器）"，三问皆可答才算过。
