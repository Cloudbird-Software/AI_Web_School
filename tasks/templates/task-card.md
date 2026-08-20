# 任务卡
```yaml
id: T-<wave>-<seq>
wave: W<n>
title: <一句话目标>
spec: []                    # 规格路径锚点（如 specs/modules/xxx.md#2.1），禁止复制规格正文
context_paths: []           # agent 需读的代码/测试目录
deliverables: []            # 文件级交付清单
acceptance: make accept TASK=T-<wave>-<seq>
accept_script:              # 必填：任务专属验收脚本路径（tools/accept/），必须先于实现存在，
                            # 且已登记进 specs/test-freeze/MANIFEST.sha256（测试冻结：agent 只能跑不能改）
model_floor: T0|T1|T2       # 最低梯队；先试低档，失败升级
token_budget: 400k          # T0:800k / T1:400k / T2:150k
owner_module:               # 互斥目录，如 src/core/instantiation
depends_on: []              # 依赖任务卡 id；冻结后方可并行
non_goals: []               # 必填至少 1 条，防镀金
escalation: fail×2 → 升梯队 → Judge → 人类
```

## 目标说明（≤5 行）

## 验收标准（逐条可执行）
1.
