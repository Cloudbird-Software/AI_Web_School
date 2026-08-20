# FINAL Holdout（全部波次完成后 · 平台总体效果验收）

> 意图（人类视角，issue #34 的终点）：**该平台已经完全可以运行，并且能够跑通题目生产。**
> 具体说：一个新学科/题型靠"加资产"就能上线（不改核心代码）；一道题从 LLM/函数库草稿到过门入账全程留痕；
> 一个真实孩子从注册到拿到弱项报告全程通畅；每一分数、每一内容都能回答三问。
> 本文件全绿 + human 项签字 = issue #34 目标状态达成，平台进入运营态。
> 前置：W5-R/W6/W7/W8 各自 holdout 与 wave-exit 全绿。

## H-FINAL-1 全部波次出口语义全绿（不退化的总闸）
- 意图：P9 / E2E-11 精神贯穿全程；任何一波的既有能力不得在后续波次被弄丢
- 类型：machine

```bash
for w in w0 w1 w2 w3 w4 w5r w6 w7 w8; do
  [ -f "scripts/wave-exit/$w.sh" ] || { echo "缺少波次出口脚本 $w.sh"; exit 1; }
  bash "scripts/wave-exit/$w.sh" >/dev/null || { echo "$w 出口红"; exit 1; }
done
```

## H-FINAL-2 题目生产端到端：一次生产作业产出合格新题且全程留痕
- 意图：issue #34 §二.1（扩展=加资产）；§四（draft→门→入账铁律）
- 类型：machine

```bash
[ -n "${HOLDOUT_PRODUCE_CMD:-}" ] || { echo "需要 HOLDOUT_PRODUCE_CMD（W8 文档化的生产入口命令）"; exit 1; }
PSQL="docker compose exec -T db psql -U ${POSTGRES_USER:?} -d ${POSTGRES_DB:?} -tA"
before=$($PSQL -c "SELECT count(*) FROM item_version WHERE status='published'")
eval "$HOLDOUT_PRODUCE_CMD"
after=$($PSQL -c "SELECT count(*) FROM item_version WHERE status='published'")
[ "$after" -gt "$before" ] || { echo "生产作业未产出新的已发布题目"; exit 1; }
$PSQL -c "SELECT count(*) FROM item_version
          WHERE status='published' AND created_at > now() - interval '1 hour'
            AND (gate_certificate_id IS NULL
                 OR lineage->>'source' IS NULL)" \
| grep -qx '0' || { echo "新产出的题目存在无门证书或无来源标记者（绕过门的产物入账了）"; exit 1; }
```

## H-FINAL-3 学生侧全链真实通畅：开局→作答→评分→弱项报告
- 意图：V1 北极星；A9
- 类型：machine

```bash
BASE="${HOLDOUT_BASE_URL:-http://localhost:8080}"
[ -n "${HOLDOUT_TOKEN:-}" ] && [ -n "${HOLDOUT_ALIAS:-}" ] || { echo "需要 HOLDOUT_TOKEN / HOLDOUT_ALIAS（已授权测试学生）"; exit 1; }
sid=$(curl -sf -X POST "$BASE/sessions" -H "Authorization: Bearer $HOLDOUT_TOKEN" \
  -H 'Content-Type: application/json' -d '{"kind":"practice"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
nxt=$(curl -sf "$BASE/sessions/$sid/next" -H "Authorization: Bearer $HOLDOUT_TOKEN")
iv=$(echo "$nxt" | python3 -c 'import json,sys; print(json.load(sys.stdin)["item_version_id"])')
curl -sf -o /dev/null -X POST "$BASE/sessions/$sid/responses" -H "Authorization: Bearer $HOLDOUT_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"item_version_id\":\"$iv\",\"answer\":{\"choice\":\"A\"},\"idempotency_key\":\"holdout-final-$sid\"}"
curl -sf "$BASE/reports/weakness/$HOLDOUT_ALIAS" -H "Authorization: Bearer $HOLDOUT_TOKEN" >/dev/null
```

## H-FINAL-4 三问全库普查：每条已发布内容与每条 AI 评分都可溯源
- 意图：issue #34 §二.2（一切可回答三问）；铁律 10
- 类型：machine

```bash
PSQL="docker compose exec -T db psql -U ${POSTGRES_USER:?} -d ${POSTGRES_DB:?} -tA"
$PSQL -c "SELECT count(*) FROM item_version WHERE status='published'
          AND (lineage->>'source' NOT IN ('public','synthetic','real') OR lineage->>'source' IS NULL)" \
| grep -qx '0' || { echo "存在来源标记缺失/非法的已发布内容"; exit 1; }
$PSQL -c "SELECT count(*) FROM item_version WHERE status='published' AND gate_certificate_id IS NULL" \
| grep -qx '0' || { echo "存在无门证书的已发布内容"; exit 1; }
$PSQL -c "SELECT count(*) FROM score_run
          WHERE scoring_trace->>'model_version' IS NULL OR scoring_trace->>'prompt_version' IS NULL" \
| grep -qx '0' || { echo "存在缺版本指纹的评分"; exit 1; }
```

## H-FINAL-5 测试冻结资产本体完整：验收意志未被任何人动过
- 意图：specs/test-freeze/（守卫自我冻结）；X1/X11
- 类型：machine

```bash
python tools/ci/check_test_freeze.py
```

## H-FINAL-6 宪法实证矩阵常绿且契约守卫在位
- 意图：A8/P9（"已强制"必须有实证，不许宣称）；P5
- 类型：machine

```bash
python -m pytest tests/contract -q
grep -q '实证' specs/contracts/TRACEABILITY.md
```

## H-FINAL-7 owner 亲自走一遍真实家长视角的完整闭环
- 意图：V1/V6（卖的是"知道孩子哪里弱"的确定性——owner 亲眼确认产品成立）
- 类型：human
- 确认要点：owner 以全新家长身份完成：注册→授权→孩子练习≥5 题→读弱项报告→确认"接下来练什么"的建议可执行。任何一步需要翻文档/找工程师即不通过。

## H-FINAL-8 owner 抽查"宣称=实证"：随机 3 条宪法条款追到可执行实证
- 意图：A8（宪法即测试）；防止"未实现却宣称已强制"
- 类型：human
- 确认要点：owner 随机选 3 条宪法条款，沿 TRACEABILITY.md 追到实证脚本并亲自执行复现；任一追不到或跑不绿即不通过。
