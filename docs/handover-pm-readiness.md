# 交接文档：CLI 端到端链路就绪状态与 AI 项目经理进场手册

> 2026-08-31。前序工作：仓库全面审查 + P0 问题修复 + StepFun（阶跃星辰）LLM
> 接入 + 跨学科端到端验证。本文档面向即将进场的 AI 项目经理（下称 PM），
> 说明当前系统状态、已验证的链路、怎么开始干活、遗留问题在哪。

---

## 一、当前系统状态（一句话）

**出题 → 校验门 → 入账发布 → 跨学科组卷 → HTML 渲染 → LLM 半确定档生成 →
入账，CLI 全链路已打通并实证**；作答 → 评分 → 诊断 → 标定 → 复习队列各环节
各自可用（前序工作已修通），但「一张卷从组卷到学生作答到弱项报告」的完整
闭环还需要 PM 串联编排。

### 数据库实况（本地 dev 库 muti_dev）

| pack | item 数 | 说明 |
|---|---|---|
| subject-math | 300 | 10 母题 × 30 实例（A 级确定性） |
| subject-lang | 123 | 120 确定性（4 母题）+ 3 LLM 草稿（C 级） |
| subject-english | 60 | 2 母题 × 30 实例 |

已发布的卷（out/papergen/ 下有 HTML+JSON 制品）：数学练习卷（10 题）、
数学诊断卷、语文练习卷、语文诊断卷、英语练习卷、含 LLM 题的语文混合卷。

---

## 二、本次修复清单（PM 无需重做）

### P0-1 渲染契约分歧（kind vs type）— 已修
生成器产 blocks 用 `kind` 键、渲染 IR 转换器要 `type` 键。已在
core/render/item_to_ir.go 加方言归一层（type 缺失时按 kind 分发；text 块
value 缺失时取 rendered/template）。

### P0-2 语文/英语 ingest 入账断链 — 已修（三处根因）
1. **谱系参数不在 params.normalized**（wordrel/pinyin/gram_sc/vocab_spell）：
   公式一 np 输入恒为空 → 全部实例共享同一 item_version_id（内容寻址破坏，
   第二条起 already-ingested）。已全部改为 `params.normalized` 嵌套。
2. **objective 是简化形态**（`{"kp": "..."}`）：组卷装配层只认标准
   kp_set 结构（与数学轮同构），语英题全部无法组卷。已在两包加
   `objective()` helper 统一产出
   `{kp_set:[{dimension,code}],kp_set_mode,cognitive_level,gradeband,graph_release}`。
3. **content 缺 blocks 方言**（平铺 stem/options）：渲染层唯一内容方言是
   blocks，缺 blocks 的题渲染成空卷面（只剩 q1/q2 占位）。已在两包加
   `scBlocks()/fillBlocks()` helper，单选题产「题干 text 块 + 每选项一行
   text 块」，填空题产「题干 text 块 + fill 块」（fill 块类型走 `type` 键）。

### P0-3 错误推断生产者 — 已修（前序）
评分桥不再硬编码空推断，从 error_bindings + 作答数据生成错误推断。

### P0-4 复习队列写入路径 — 已修（前序）
core/review.SyncService：重放作答事件流 → 幂等写复习队列。

### P0-5 参数标定 CLI — 已修（前序）
cmd/calibrate：CTT 统计 → item_param 表（scene 隔离、min-sample 门、幂等）。

### P0-6 PG 提交路径 correct_count/wrong_marks — 已修（前序）

### StepFun（阶跃星辰）LLM 接入 — 本次完成
- baml_src/clients.baml 注册命名 client `StepFun`（OpenAI 兼容网关，
  base_url=https://api.stepfun.com/step_plan/v1，模型 step-3.7-flash，
  **api key 走环境变量 STEPFUN_API_KEY，零明文入仓**）。
- GenerateSentenceReorg 函数签名加 `vocab_candidates` 参数：把词表判定域
  显式递给出站面（此前 LLM 不知道词表，answer/distractors 全靠猜，可解性
  校验 30 连拒）；prompt 同时明确「句子给完整形式，挖空由系统完成」。
- **tier C 公式二入账修复**：ingest 此前对 C 级也硬算公式一，而 publish 按
  tier 分流走公式二 → 内容寻址必然脱钩。cmd/ingest 的
  computeInstanceVersionID 已改为与 publish.verifyContentAddress 同款判定
  树（A/B→公式一，C/D→公式二，eb 提升为 []any 对齐 canonical 类型面）。
- 实测：`langgen -reorg 3` 出 3 接受/0 拒绝，3 条 LLM 草稿全部入账并可组卷。

---

## 三、PM 怎么开始（操作手册）

### 环境准备

```bash
# 1) DB（docker-compose 或本机 PG16，凭据见 .env）
psql "postgresql://muti:muti-dev-pass@localhost:5432/muti_dev" -c 'select 1'

# 2) LLM key（StepFun 平台密钥，已验证可用）
export STEPFUN_API_KEY=<密钥>   # 向人类索取，勿入仓

# 3) 常用 DSN 环境变量
export SCHOOL_DATABASE_URL="postgresql://muti:muti-dev-pass@localhost:5432/muti_dev"
```

### 全链路命令序列（已实证可重放）

```bash
# ① 语料装载（种子数据 → 账本）
go run ./cmd/seedload ...

# ② 出题（三个学科包，确定性档）
go run ./cmd/mathgen ...        # out/mathgen/
go run ./cmd/langgen -n 30 -out out/langgen/
go run ./cmd/enggen -n 30 -out out/enggen/

# ②' 出题（语文半确定档：StepFun LLM 草稿）
go run ./cmd/langgen -n 5 -reorg 3 -provider stepfun \
  -model step-3.7-flash -model-version 2026-08 -out out/langgen-reorg/

# ③ 入账（校验门 → item/item_version → 发布；学科包按模板前缀自动分派）
go run ./cmd/ingest -dsn $SCHOOL_DATABASE_URL -in out/langgen/ \
  -pack-digest sha256:086b8dce03c5ce4a72b0024cfd936940619162360ca2d8a834a7497e1360705f
# 注：pack-digest 仓库无真源必须显式传。当前约定值：
#   语/英共用 sha256:086b8d...（= content/sources/corpus/manifest.yaml 的 sha256）
#   数学   沿用 sha256:573d95fbb61001c7b9ab811634024765a98096c29f7971b0ccdb23844d89fd7b

# ④ 组卷（蓝图 JSON → 编排 → HTML/JSON 制品）
go run ./cmd/papergen -blueprint out/bp-lang.json -out out/papergen/

# ⑤ 作答/评分/诊断/标定/复习（cmd/school 子命令 + cmd/calibrate）
go run ./cmd/school ...          # 见 cmd/school 各子命令
SCHOOL_DATABASE_URL=$SCHOOL_DATABASE_URL go run ./cmd/calibrate -scene practice
```

### 组卷蓝图要点
- R-Z-02 冻结契约：**同卷内同母题至多一题** → 每科一张卷的题量上限 =
  该科在池母题数（当前：数学 10、语文 4+1、英语 2）。`item_count_range`
  超过即 InfeasibleError。这不是 bug，是曝光互斥设计；要出更大的卷先扩母题。
- 蓝图示例见 out/bp-*.json（practice/diagnosis 两种 purpose 都已验证）。

### 质量门（提交前必过）
```bash
make check-go   # sqlc-diff + gofmt + build + test + import-boundary + baml-golden + errcheck
```

---

## 四、合成数据与压测建议（PM 的第一批活）

1. **扩母题**：当前语文 4+1、英语 2 个母题是跨科组卷的硬瓶颈。每个新母题
   = 生成器（packs/subject{lang,english}/xxx_gen.go）+ 独立校验器 + 注册进
   BuildDeterministicSuite/BuiltinGenerators + 模板注册表对齐。参照
   wordrel_gen.go 的形态（index 索引空间 + 答案位确定性轮换 + error_bindings）。
2. **压力测试面**：mathgen/langgen/enggen 批量生成唯一率与摘要碰撞；
   ingest 重放幂等（already-ingested 计数）；papergen 同蓝图同种子确定性
   （paper_id 内容寻址回放）；session 并发提交幂等。
3. **红队面**：喂篡改 JSONL（digest 漂移）、私造模板 id、非注册表
   interaction/scorer、C 级缺谱系键——全部应 fail-closed 留痕（gate_failure
   账可查）。append-only 触发器会物理拒绝 UPDATE/DELETE（三本账铁律）。
4. **LLM 批量**：`-reorg N` 走 StepFun；单题 ~7-8s（step-3.7-flash 推理
   模型）；可解性校验 fail-closed 会丢 draft（现在通过率高，词表注入后
   3/3）。注意 langgen 的 runReorg 总 ctx 5 分钟，大批量需分批跑。
5. **标定**：作答事件攒够后 `cmd/calibrate -scene practice -min-sample 30`；
   D5 禁止跨场景混估。

---

## 五、遗留问题与边界（如实）

1. **OCR 未上**（已知，人类在找资源）。
2. **paper/paper_item 未落库**：papergen 只产本地制品，落库挂后续卡
   （与曝光预留同事务，见 papergen 输出的提示行）。曝光互斥的静态/在线
   轨查询已就绪（core/assembly/exposure.go），缺写入面接线。
3. **卷头 QR 位图缺位**（#152 前现状，fail-loud 如实留痕）。
4. **sentence_reorg LLM 题质量参差**：可解性校验只保证结构合法（挖空词
   恰好一次、词表内、无多解），不保证教学合理性。实测出现过「上下学」拆词
   这种怪题。建议加语义级过滤或人工抽检。
5. **词表规模小**（语文 45 词、demo 语料）：vocab_candidates 全量注入目前
   无压力；词表扩张到数百词后建议改滑动窗口采样（sentence_reorg.go 的
   vocabCandidates 注释已标）。
6. **MathMCP/服务面**：cmd/school 有 HTTP 面雏形，PM 若要走 API 而非 CLI，
   先看 cmd/school 路由与认证主体绑定（铁律 8：无主体端点是禁令）。
7. **ai_call_ledger**：langgen CLI 用进程内 MemoryLedger（D10 台账只有
   内存形态）；生产服务面要用 PGLedger 落 ai_call_ledger 表（0026 迁移已
   就绪）。PM 若做 LLM 压测统计，走服务面或给 langgen 加 PG ledger 选项。
8. **三本账纪律**：开发库清账要走 `ALTER TABLE ... DISABLE TRIGGER USER`
   （owner 权限），这是唯一合法入口——生产库禁止。

---

## 六、本次触碰的文件清单（git diff 可查）

- packs/subjectlang/{aliases,char_recognize,pinyin_gen,wordrel_gen,radical_gen,sentence_reorg}.go
- packs/subjectenglish/{util,gram_sc,vocab_spell}.go
- packs/subjectmath/distinct.go（[]string canonical 支持，前序）
- cmd/ingest/{ingest,packs}.go + internal/bamlai/bamlai.go（tier C 公式二 + 信封四键）
- baml_src/clients.baml（新）、baml_src/generators/lang_sentence.baml
- baml_client/*（BAML 重新生成物）
- cmd/calibrate/main.go（errcheck 清理）
- tools/golden/baml_src.sha256（golden 快照随 prompt 变更已 update）

## 七、验收基准（PM 进场自检）

```bash
make check-go                                                          # 全绿
go run ./cmd/langgen -n 30 -out out/langgen/                           # 120/120
go run ./cmd/enggen -n 30 -out out/enggen/                             # 60/60
# ingest 三科入账 accepted=全量 rejected=0
# papergen 各科蓝图出卷 HTML 内容正确（非 q1/q2 占位）
STEPFUN_API_KEY=... go run ./cmd/langgen -reorg 3 ...                  # ≥1 接受
```

以上全部通过 = 「PM 进场就绪」状态成立（2026-08-31 实测通过）。
