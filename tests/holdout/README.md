# 端到端 Holdout 测试（人类意图的效果验收）

> 依据：issue #34 §十一修正案；宪法 A8 / P9 / V1（终点是真实小学生拿到弱项报告）；T-W5-034。
> 本目录是**冻结资产**（specs/test-freeze/ 保护）：开发 agent 只能经 `make holdout WAVE=<name>` 执行，永远不能修改。

## 是什么

Holdout 测试回答一个问题：**这一波（以及全部波次做完后），系统在人类想要的效果上真的成立吗？**

与单元/契约/黄金测试的区别：那些测试由开发者围绕实现写；holdout 由**人类围绕意图写**——在实现开始之前写死，写法是"外部观察到的效果"（攻击被拒、账查得到、孩子拿得到报告），不引用任何内部实现细节。因此实现 agent 无法针对测试"应试"，测试也不因重构而失效。

## 文件与执行

| 文件 | 对应波次出口 | 执行 |
|---|---|---|
| `w5r.md` | W5-R 可信底座（issue #34 W5-R） | `make holdout WAVE=w5r` |
| `w6.md` | W6 引擎解锁（题目生产飞轮） | `make holdout WAVE=w6` |
| `w7.md` | W7 首个真实用户 | `make holdout WAVE=w7` |
| `w8.md` | W8 规模与增长 | `make holdout WAVE=w8` |
| `final.md` | **全部波次完成后的总体验收**（issue #34 目标状态："平台完全可运行且跑通题目生产"） | `make holdout WAVE=final` |

- 波次出口 = 该波 `scripts/wave-exit/w<N>.sh` 全绿 **且** 对应 holdout 的 machine 项全绿 **且** human 项由 owner 逐项签字确认（记录进 `tasks/w<N>/BRIEF.md` 出口结论）。三者缺一，波次不算结束。
- `final.md` 是全平台最终验收：W8 出口后执行，machine 全绿 + human 签字 = issue #34 的目标状态达成。

## 条目格式（run_holdout.py 解析约定）

```
## H-<WAVE>-<N> <标题>
- 意图：<这条测试保护的是哪条人类意图/宪法条款>
- 类型：machine

```bash
<可执行探针：退出码 0 = 通过>
```
```

- `类型：machine`：含且仅含一个 bash 探针，由 `tools/ci/run_holdout.py` 执行；非零退出即 FAIL。
- `类型：human`：无探针，执行器只列出、不判定，由 owner 人工确认。
- 探针只许观察**效果**（HTTP 响应、数据库状态、台账记录、进程行为），禁止 import 或调用被测系统内部代码。
- 环境约定：`HOLDOUT_BASE_URL`（默认 `http://localhost:8080`）指向被测服务；数据库经 `docker compose exec -T db psql` 访问（`POSTGRES_USER`/`POSTGRES_DB` 来自 `.env`）。前置：`docker compose up -d --wait` 且服务与迁移已就绪。
