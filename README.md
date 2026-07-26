# 母题平台（muti-platform）

小学语数英个性化练习平台。架构见 `specs/constitution.md`（L0 宪法）与《工程架构设计方案 v2》（外部文档）。
开发方式：OPC（1 人类 + AI agent 军团），SDD 范式——**规格是唯一事实源，CI 信号是唯一信任**。

## 快速开始（新机器）
```bash
cp .env.example .env   # 填入密钥
make bootstrap         # 环境搭建+自检
make sync-rules        # 同步 IDE 规则文件
python tools/opc board # 任务板校验
make demo-w0           # W0 出口验收
```

## 目录
- `specs/` 规格库（constitution.md=最高约束；contracts/ 冻结契约；modules/ 模块规格；adr/ 决策记录）
- `tasks/` 任务卡与任务板
- `.agent/` DevOS：roles/ 角色 prompt、rules/ 规则源、routing.yaml、telemetry/ 遥测
- `src/` 平台代码（core/ 核心域 + packs/ 学科与学段包 + registry/ 注册表 + workbench/）
- `tests/` contract（契约）/ golden（黄金数据集）/ golden-path（黄金路径）/ model-bench（模型基准赛，私有）
- `tools/` opc CLI、sync_rules.sh、make_accept.sh
- `content/` 内容资产与引入管线（sources/=来源许可登记）

## 铁律速记
三本账只增不改｜门不过不入库｜构建者不自证｜信号大于汇报｜核心零学科特判｜禁改测试｜密钥不入库
