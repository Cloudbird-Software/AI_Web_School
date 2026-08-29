# 任务板（唯一调度事实源）
> 规则：①进行中任务的 owner_module 互斥（python tools/opc board 校验）；②状态只能：就绪→进行中→待验→完成 / 升级；③"进行中"超 3 天未更新=红灯。

## 升级队列（人类处理）
| 任务卡 | 原因 | 日期 |
|---|---|---|

## 进行中
| 任务卡 | owner_module | 模型 | 开始日期 |
|---|---|---|---|

## 待验
| 任务卡 | PR | Verifier |
|---|---|---|
| T-W5-033 | #64+#68（已合并；#65 被 ADR-0040 自动关闭后按指示以 #68 续作） | gate 切 Go 工具链全量落地：GO-2 errcheck（tool 钉 v1.9.0）/ GO-5 goleak（api TestMain）/ SQL-1 静态成对 check_pairs.py / SQL-2 sqlc（SHA256 钉扎二进制 v1.31.1，drift 检查进 check-go 链首）/ ci go-check job 入 gate。红线实证：verify PR #71（sqlc 漂移→go-check/gate 双红，日志含精确 diff 输出）+ 后续 cycle；供应链：x-tools/x-mod 下钉 ≥90 天、x 系许可 PURL 豁免（ADR-0026 先例）、grpc GHSA×age 死结以钉扎二进制解（ADR-0039 备忘） |
| W6-math 第一阶 | #84（已合并） | 数学轮确定性生成管线：3 母题（乘法/分数比较/单位换算）×30 实例唯一率 100%（52,602 参数点全域扫描）；生成器×验证器独立+地面真值防共谋；PCG 可回放逐字节一致。owner 授权提前启动 W6（2026-08-27 会话） |
| T-W5-008 | #89（已合并） | API 边界加固三件套：CORS 白名单/双维令牌桶限流/统一错误映射 + panic 防线与体限；与 006 合流（边界外圈+shield 内圈） |
| T-W5-016 | #87（已合并） | 评分链路可回放：scoring_trace 固定记录 + 评分器契约注册即校验（ADR-0005 模型仲裁语义结构化） |
| T-W5-021 | #91（已合并） | 语篇事实核查判定表显式化 + 阻断性归策略（修正冻结实现 review 结构性过不了门缺陷） |
| T-W5-022 | #90（已合并） | migratechain 双守卫（版本连续性 + alembic 链一一对应）——免 Docker 本地可验；**首战即拦截 012 跳号** |
| T-W5-012 | #92+#94（已合并） | 0030 角色职责分离（writer/reader 收敛 0014 误授）+ 审计独立双 Executor 事务（业务/审计双向语义）+ AES-GCM 零明文断言 |
| T-W5-010 | #96（已合并） | 会话入口 fail-closed 授权门（nil 账=500 拒绝）+ 防 oracle 探测排序（越权判据先于授权门） |
| T-W5-013 | #97（已合并） | 姓名脱敏边界修复 + Go 原生 fuzz（修复前 fuzz 即红、修复后 36 万 execs 零 crasher） |
| W6-math 第二三阶 | #88+#95（已合并） | **数学轮 10/10 母题达成**（≈16.2 万参数点全域扫描、唯一率 100%、同 seed 逐字节可回放）；开发中抓到 2 个真换算 bug |
| T-W5-028 | #105+#108 分支（已合并 #105；owner 补 FROZEN 登记与签署） | ADR-0006 待批准（openapi-v1.1.yaml 契约草案 + 契约测试三件）；红队两轮对抗审查闭环（B1/B2/M1 全修） |
| T-W5-027 | #108 分支 task/wave7-027（00fc5bb，adversary 回写等待中） | 宪法 49 条实证矩阵 + traceability 机器校验器（实仓绿）；载体差异（specs/contracts 检测面 fail-closed）已记录为 A8 首条 |
| W6-math 1-3 阶 | #84/#88/#95（已合并） | 数学 10/10 母题（≈16.2 万参数点全域验证） |
| W6-lang 1-2 阶 | #106/#107（已合并） | 语文：语料管线+char_in_corpus+字辨认（20/20 互异）；句子重组半确定档（BAML+六条可解性校验） |
| W6-eng 第一阶 | #110（已合并） | 英语：GSL 词表节选 + 词汇拼写/语法单选两母题（243 点全域互异、31 mutants） |
| T-W5-019 | #72（已合并） | 0025 留痕迁移（activated_by）+ core/estimator 并发安全（64 goroutine -race 恰一活跃）；事实修正：0016 已有偏唯一索引，真缺陷是无锁+无留痕 |
| T-W5-017 | #76（已合并） | core/events 事务显式传递（ErrNoTransaction fail-closed）+ go/parser 静态守卫（零 Commit/Rollback，红绿双向）+ fakeTx 回滚一致性 |
| T-W5-014 | #75（已合并） | core/ai 总线：fail-closed 三路径（PII 剥离/台账/预算）+ 0026 ai_call_ledger（append-only 触发器）+ 出站 https 强制 + 零新依赖；W6 LLM harness 地基 |
| T-W5-005 | #70（已合并） | 认证与主体绑定框架（core/auth + api/middleware，零新依赖）：HMAC 令牌/fail-closed 密钥/五类拒绝路径 -race 绿；owner review 通道完成 |
| T-W5-020 | #79（已合并） | 查重验证器重建真实内容摘要路径（修复冻结实现 digest↔主键互证的 X11 缺陷）；CanonicalJSON/ContentDigest 唯一口径 |
| T-W5-006 | #85（已合并） | 全端点接入认证：openapi 全 13 端点运行期扫描逐个匿名探测断言 401；D9 端到端闭合；X13 无无主体端点 |
| T-W5-011 | #83（已合并） | 0027 唯一性迁移 + core/compliance 双实现；64 并发授权版本严格连续；事实修正：0015 仅非唯一索引 |
| T-W5-002 | #82（已合并） | 0028 六引用面 FK + gate_failure 留痕表（append-only）+ CertificateVerifier/FailureTrail（显式事务面） |
| T-W5-023 | #81（已合并） | tools/scan 双守卫：FROZEN.txt 全量遍历 + 无排名扫描（补驼峰/聚合/GORM 三盲区）；接线方式留 owner 裁量 |
| T-W5-001 | #69（已合并） | 0024 迁移四表语句级 append-only 触发器（复用 0005 函数）；migrate_check.py 探针（真 UPDATE/WHERE FALSE/DELETE 三拒 + down 回滚段）在 CI 真实 PG 全绿；passage 审阅扩盖、item_version 契约排除逐表留痕 |
| T-W5-031 | #42（已合并） | Go 骨架：t_w5_031.sh 全绿（gofmt/build/vet/test-race + fuzz + X6 红绿双向 + healthz 脱敏）；遗留缺陷清单见 issue #43/#45，随修复 PR 关闭 |
| T-W5-035 | #48 | org 治理 CI 变更（Cloudbird-Software/.github#84 / ADR-0032）：gate aggregator 严格化 skipped≠success + EXPECTED_SKIP 白名单；Verifier=机器门禁（本 PR CI 全绿；T1 注入负向测试 Use-up-Plan PR#30 gate 红） |
| T-W5-036 | #49 | org 治理 CI 变更（Cloudbird-Software/.github#89 / ADR-0038）：契约兼容性检测门接线——contract job（CI-Workflows contract.yml）入 gate needs；检测面 specs/contracts/**（jsonschema breaking）+ alembic/versions/**（destructive DDL 须 ADR+downgrade 逆操作）；Verifier=机器门禁（本 PR CI 全绿，含 contract job 首跑） |
| T-W5-032 | #42（已合并） | 22 对 up/down + migrate-go-check 全绿；CodeRabbit 遗留缺陷见 issue #43/#45（负数步/固定口令/重定义/parity 切分），随修复 PR 关闭 |
| T-W0-010 | #35（已合并） | 警报处置完毕：15 闭 + 3 dismiss（issue #41）+ 3 窗口自愈 |
| T-W0-011 | #40（已合并） | CodeQL #25/#26 已自动关闭 ✅ |
| T-W5-030 | #37（已合并） | 任务卡 spike 结论 |

## 就绪（按优先级）
> 当前就绪波次：**W5-R Go+BAML 重建波**（2026-08-19 重排，ADR-0004 / issue #34；详见 tasks/w5/BRIEF.md、tasks/w5/REANCHORING.md）。W2–W4 已全部完成，历史就绪清单见 git 历史。
> 派工顺序：**先基建（批次 R：T-W5-030..033），后原卡（批次 A–F，重锚定到 Go，语义不变）**；原卡间依赖不变，同批次内可并行，跨批次须等前批合并。
> 铁律：W5-R 期间不接受任何新功能任务。

| 任务卡 | 标题 | model_floor | 依赖 |
|---|---|---|---|
> 批次 R（T-W5-030..033）已全部完成（2026-08-27），批次 A 解锁。

| **批次 A（可并行 8 条，重锚定 Go）** | | | |
| T-W5-001 | 内容版本账 append-only 物理强制补齐 | T0 | — |
| T-W5-005 | 认证与主体绑定框架 | T0 | — |
| T-W5-011 | 家长授权账版本原子性与并发安全 | T1 | — |
| T-W5-014 | AI 总线 fail-closed、台账全覆盖与出站加固 | T0 | — |
| T-W5-017 | 事件写入事务边界归位 | T0 | — |
| T-W5-019 | 估计器指针切换并发安全 | T1 | — |
| T-W5-020 | 查重验证器走真实内容摘要路径 | T0 | — |
| T-W5-023 | CI 守卫盲区修复（冻结契约 / 无排名扫描） | T1 | — |
| **批次 A'（可并行，无依赖但优先级次之）** | | | |
| T-W5-009 | 渲染出口安全（PDF 沙箱与失败检测） | T1 | — |
| T-W5-026 | 打包与部署正确性 | T1 | — |
| **批次 B** | | | |
| T-W5-002 | 门证书外键补建与门失败留痕落地 | T0 | T-W5-001 |
| T-W5-006 | 全端点接入认证与学生主体绑定 | T0 | T-W5-005 |
| T-W5-008 | API 边界加固（CORS/限流/错误映射） | T1 | T-W5-005 |
| T-W5-012 | PII 保险库权限模型与审计独立事务 | T0 | T-W5-011 |
| T-W5-015 | TTS 链路 PII 剥离与台账对齐 | T1 | T-W5-014 |
| T-W5-016 | 评分链路可回放与评分器契约校验 | T0 | T-W5-014 |
| T-W5-021 | 语篇事实核查判定与阻断策略修正 | T1 | T-W5-020 |
| **批次 C** | | | |
| T-W5-003 | 发布服务证书验真与内容寻址 fail-loud | T0 | T-W5-002 |
| T-W5-004 | 会话题序不可变与结构性 DB 约束 | T1 | T-W5-002 |
| T-W5-007 | 服务端凭证治理与敏感字段屏蔽 | T0 | T-W5-006 |
| T-W5-010 | 家长授权接入在线会话入口 | T0 | T-W5-006 |
| T-W5-013 | 姓名脱敏边界修复与强断言 | T1 | T-W5-012 |
| T-W5-028 | API v1.1 契约变更申请（认证引入） | T0 | T-W5-006 |
| **批次 D** | | | |
| T-W5-018 | 作答提交幂等与并发安全 | T0 | T-W5-010, T-W5-017 |
| T-W5-022 | 迁移可逆全量验证与 PR 阶段拦截 | T1 | T-W5-004 |
| T-W5-027 | 强制实证矩阵（宪法条款 ↔ 可执行实证） | T0 | T-W5-003/006/014/017 |
| **批次 E** | | | |
| T-W5-024 | 黄金路径端到端补齐至 ≥10 种交互类型 | T0 | T-W5-018, T-W5-016 |
| T-W5-025 | 关键覆盖空洞补齐（并发/门/认证/API 集成） | T1 | T-W5-002/006/018/020 |
| **批次 F（出口）** | | | |
| T-W5-029 | W5 出口脚本与不退化基线 | T0 | T-W5-024/025/027/028 |
| **W5 验证卡** | | | |
| T-W5-T01 | 验证卡 · 门与账的物理强制 | T0 | T-W5-001/002/003/004/020/021 |
| T-W5-T02 | 验证卡 · 认证、合规与事务并发 | T0 | T-W5-006/007/010/013/014/018 |
| T-W5-T03 | 验证卡 · W5 出口与实证矩阵 | T0 | T-W5-029 |

## 完成
| 任务卡 | 完成日期 | 备注 |
|---|---|---|
| T-W0-001 | 2026-07-26 | PR #7 已合并；bootstrap 全绿 |
| T-W0-002 | 2026-07-26 | PR #8 已合并；三演示 PR (#4/#5/#6) 全部按预期拦红并关闭 |
| T-W0-003 | 2026-07-26 | PR #9 已合并；7 单测绿且 tests/unit 已纳管 |
| T-W0-004 | 2026-07-26 | PR #2 已合并；四契约冻结，contract-watch 生效 |
| T-W0-005 | 2026-07-26 | PR #10 已合并；DB 三表+0001 |
| T-W0-006 | 2026-07-26 | PR #11 已合并 |
| T-W0-007 | 2026-07-26 | PR #3 已合并；泄密扫描引号盲区修复+tests/unit 纳管+contract-watch 新增豁免 |
| T-W0-008 | 2026-07-26 | W0 反馈修正：最小链路真实化（demo-w0-min-link.py）+镜像 digest 锁定+dev 依赖锁定 |
| T-W1-001 | 2026-07-26 | src/ 项目骨架与依赖落地；全部 W1 交付物在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-002 | 2026-07-26 | 迁移 0002：item 族 12 表（含 material_license，见卡片补注）+触发器，make migrate-check 全绿；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-003 | 2026-07-26 | src/core/models ORM + Pydantic 实体模型；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-004 | 2026-07-26 | src/registry/ 双注册表加载与校验（实现路径 src/registry/，与 OPC 蓝图一致）；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-005 | 2026-07-26 | 迁移 0003 + src/core/events append-only 写入服务；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-006 | 2026-07-26 | 迁移 0004 + src/core/gate/models.py（字段名勘误见卡片补注：cert_id/run_id/verdict_id）；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-007 | 2026-07-26 | src/core/content 内容版本写入服务（含门强制骨架）；PR 在 task/T-W1-006-gate-tables 分支待合并 |
| T-W1-008 | 2026-07-26 | scripts/wave-exit/w1.sh 出口全绿 + CI 集成；PR 在 task/T-W1-006-gate-tables 分支待合并 |

## 备注
- **验证卡命名差异（P10）**：W1 四张验证卡实际文件名为 `tasks/w1/T-W01-T01.md`~`T-W01-T04.md`（`W01` 而非 `W1`）。为免外部引用断裂，保持原名不改；**W2 起验证卡统一采用 `T-W2-T0X` 格式**。
- **W2 遗留人类任务**：①`specs/contracts/db/paper-model.md` 冻结契约待人类/Scribe 补发（T-W2-037 暂以架构 v2 §4.6+附录 A 为规格源）；②`tools/opc` 暂不校验验证卡（T-W2-T0X 格式与 prerequisites 字段），W2 执行期靠 EXECUTION.md 人工复核，工具改进后置。
- **W2（45 卡）全部完成**（2026-07-27）：母题引擎/校验门/图谱/语料/黄金 50/数学包/语文包/渲染追溯/API 工作台；w2.sh 18/18 全绿；demo-w2-business PASS。
- **W3（业务闭环波）全部完成**（2026-07-28）：组卷引擎/诊断组卷/作答会话/评分联通/弱项报告/复习排程/英语包/数据域（item_param+CTT+Elo）；w3.sh 16/16 全绿；demo-w3-business 学生闭环 PASS。
- **W4（发布前就绪波，51 卡）全部完成**（2026-07-29）：家长授权与 PII 保险库/AI 总线与 TTS/测量与参数收缩/运维与备份/工作台签发闭环；w4.sh 全绿。
- **波次重规划（2026-07-30，依据 specs/adr/0003-vision-v2-and-wave-replan.md）**：宪法升级至 v2.0（北极星愿景 V1–V6 + A8–A10 + D9–D11 + P9 + X11–X13）；新波次为 **W5 信任硬化 → W6 引擎解锁与飞轮加速 → W7 首个真实用户 → W8 规模与增长**，总览见 `tasks/roadmap.md`。
- **W5 冻结新功能**：W5 期间除修复与实证外不接受任何新能力开发（见 tasks/w5/BRIEF.md 非目标）。
- **W7/W8 暂不物化任务卡**：按 P5 波内契约冻结纪律，仅维护 `tasks/w7/ISSUES.md`、`tasks/w8/ISSUES.md` 台账，进入该波前再拆卡。
- **旧 GitHub Issue（#20–#30，10 条）处置**：裁决见 `tasks/LEGACY-ISSUES.md`；#24/#26/#28 重写为 T-W6-012 / T-W6-008 / T-W6-009，其余关闭。
