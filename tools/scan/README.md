# tools/scan — T-W5-023 CI 守卫盲区修复（Go 重锚定）

冻结实现（Python）的两个守卫有洞：

1. `scripts/ci/check_openapi_diff.py` 只硬编码守护 openapi-v1.yaml 单文件，
   `specs/contracts/FROZEN.txt` 里其余冻结契约不在任何机器守卫的面上。
2. `scripts/ci/check_no_ranking.py` 是 Python AST 面，Go 重写后无等价物；
   且跨 alias 聚合、路由 prefix 拼接等形态原本就漏。

本目录提供两个**可独立运行**的 Go 命令补上这两个盲区（tasks/w5/T-W5-023.md）。

## 命令

| 守卫 | 运行 | 作用 |
| --- | --- | --- |
| A 冻结契约遍历 | `go run ./tools/scan/frozencontract [-root REPO_ROOT]` | 遍历 FROZEN.txt **全部**条目，断言每条 (a) 文件在盘上 (b) 被 `tests/contract/` 以完整路径引用；缺口逐条 fail-loud |
| B 无排名扫描 | `go run ./tools/scan/norank [-root REPO_ROOT]` | 静态扫 `core/ api/ db/queries/` 中跨用户排名查询模式，命中即红 |

退出码语义一致：`0` = 干净；`1` = 有违规（gate 应拦截）；`2` = 操作错误
（找不到仓库根 / 扫描面缺失 / 扫描面为空）。扫描面缺失按操作错误而非通过处理——
守卫静默空转等于没扫（GO-1 教训）。

两个命令的根目录都可省略：从当前目录逐级上溯找锚文件（B 找 `go.mod`，
A 找 FROZEN.txt），因此可直接在仓库任意子目录运行，也兼容 CI checkout 后
的任意工作目录。

## 守卫 B：检测类别与误报面控制

只扫两类载体：**形似 SQL 的字符串常量**（先过 SQL 关键词门，再匹配模式）
与 **db/queries/*.sql 全文**（按 sqlc `-- name:` 分节）。注释与普通文档字符串不扫。

| 类别 | 形态 |
| --- | --- |
| sql_order_by_score | `ORDER BY ... <成绩列>`（score/total_points/correct_count/accuracy/percentile…，词边界精确匹配） |
| sql_window_rank | `RANK()/DENSE_RANK()/PERCENT_RANK()/CUME_DIST()` 空参窗口函数 |
| sql_rownum_over_score | `ROW_NUMBER() OVER (... 成绩列 ...)` |
| sql_rank_column | SELECT/WHERE/GROUP BY/HAVING 子句中出现独立词 rank |
| cross_alias_agg_score | 同语句内聚合(成绩列) + student_alias 维度且别名未绑定主体参数（即"按学生分组比分数"的无界形态）；有 `student_alias_id = $n` 边界的单生统计不误报 |
| orm_order_by_score | GORM 风格 `.Order("<成绩列>...")` / `.OrderBy(...)` 字面量参数 |
| route_rank_path | 路径形字符串含 `/rank|/ranking|/leaderboard`（子片段即命中 → prefix 拼接后仍被覆盖） |
| func_name_rank / query_name_rank | 函数名/sqlc 查询名具排名语义（snake 锚定对齐冻结实现 + 驼峰双锚定补丁，如 `GetStudentRanking`；`EnsureNoRanking` 这类守卫名不误报） |

### 白名单（理由必填）

```go
// Go：违规所在行行尾（或字符串字面量起止区间内的任一行）
var okay = "SELECT * FROM t ORDER BY score DESC" // norank-allow 单用户自评历史列内排序，评审 PR#61

// 函数名违规写在 doc 注释里：
// norank-allow 兼容旧移动端别名的只读门面，实现体仅转发本人数据接口（PR #61）
func GetStudentRanking() {}
```

```sql
-- db/queries/*.sql：写在同一查询 stanza 内任一行
-- norank-allow 教师端分组视图组内仅呈现等级不带序数（ADR-0047）
-- name: TeacherGradeBoard :one
SELECT student_alias_id, MAX(correct_rate) FROM responses GROUP BY student_alias_id;
```

写了标记但**没有理由**（裸 `norank-allow` / 只有冒号）→ 判为
`whitelist_no_reason` 照样红——白名单必须留痕，防止逃逸口被无脑滥用。

## 守卫 A：覆盖事实源约定

每个冻结契约必须被 `tests/contract/**` 下任一文本文件（.py/.md/.yaml/.yml/
.txt/.json/.sql，≤1MB）以**完整相对路径**字面引用。现有契约测试都以
`Path("specs/contracts/...")` 形式书写引用，天然满足；判定是机械子串匹配，
无任何硬编码清单——新契约进入 FROZEN.txt 即自动纳入断言面，配套契约测试
未落地则该条目以 `[uncovered]` fail-loud 列出（任务卡验收 #1「无法机器判定的
条目 fail-loud」）。另断言：文件在盘上存在、清单条目不重复、清单非空。

注意分工：本工具管「冻结面无遗漏且各有活的契约测试」；「冻结契约内容不许改」
仍由 diff 守卫（Python 版 + 未来 CI diff 面）负责，两者互补不互替。

## 与 make check 的接线（待协调者裁决）

Makefile check 目标为人类专属不可由 agent 改动。建议接法二选一：

1. 在 gate 聚合工作流（复用 CI-Workflows 处）追加一步：
   `go run ./tools/scan/norank && go run ./tools/scan/frozencontract`
2. 由有 Makefile 权限者把上面两条并成一行加进 `check-go` 之后的目标串。

两工具均零依赖（纯标准库），CI 无需额外安装。

## 自测

```bash
go test -race ./tools/scan/...
```

双向自测覆盖：违规样例树每个类别必红、合法样例树（含 per-user 边界聚合、
合法路由、生成物/testdata 排除）零误报、白名单有效/残缺两种走向、退出码
0/1/2 三态、以及当前仓库 HEAD 的实仓冒烟（工具自己跑真仓库应为绿——若未来
引入真实违规，测试先于 CI 红）。
