# 任务板（唯一调度事实源）
> 规则：①进行中任务的 owner_module 互斥（python tools/opc board 校验）；②状态只能：就绪→进行中→待验→完成 / 升级；③"进行中"超 3 天未更新=红灯。

## 升级队列（人类处理）
| 任务卡 | 原因 | 日期 |
|---|---|---|

## 进行中
| 任务卡 | owner_module | 模型 | 开始日期 |
|---|---|---|---|

## 就绪（按优先级）
| 任务卡 | 标题 | model_floor | 依赖 |
|---|---|---|---|

## 待验
| 任务卡 | PR | Verifier |
|---|---|---|

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
