# tools/sql — SQL 迁移工具面

| 脚本 / 命令 | 依赖 | 运行时机 | 职责 |
| --- | --- | --- | --- |
| `check_pairs.py` | 纯标准库 | `make sql-pairs`（check / CI go-check job） | SQL-1 静态成对深检：命名合规、up/down 成对、非空 down、版本重复、子目录禁令 |
| `migrate_check.py` | Docker + 临时 PG + psycopg | `make migrate-go-check`（make check 内） | T-W5-032 运行时全量验证：parity / down→up cycle / append-only 探针 |
| `gen_migrations_from_alembic.py` | Docker + 临时 PG | 本地按需（产物入库，CI 只校验不重生成） | 从 alembic 在线捕获 DDL 生成 `db/migrations/*.sql` |
| `migratechain/` | 纯标准库（Go 命令） | **本地任意时刻**（T-W5-022，见下） | 迁移链静态守卫 A/B：版本连续性 + alembic↔golang-migrate 链一致性 |

## migratechain — 迁移链静态守卫（T-W5-022 补缺口）

CI 曾两次在迁移链上红：「版本号序列非 0001..00NN 连续」（migrate_check.py）与
「Multiple head revisions」（alembic，0022 曾并错链）。这两类问题此前只在
PR 阶段（`make check` → `migrate-go-check`，需 Docker + 临时 PG）才暴露。
migratechain 把它们提取为**免 Docker、纯标准库**的两个独立守卫，任何人
push 前可本地运行：

```bash
go run ./tools/sql/migratechain            # 仓库任意子目录可跑（上溯找 go.mod 锚根）
go run ./tools/sql/migratechain -root /path/to/repo   # 显式指定仓库根
go test ./tools/sql/migratechain/ -race    # 双向自测（红/绿 fixture + 实仓冒烟）
```

退出码：`0` 干净；`1` 有违规（gate 应拦截）；`2` 操作错误（找不到仓库根 /
迁移面缺失或为空——守卫静默空转等于没扫，按失败处理而非通过）。

### 守卫 A（continuity）：db/migrations 版本连续性

`db/migrations/*.up.sql` 的 NNNN 前缀必须恰为 `0001..N` 连续——从 0001 起、
无断档、无重复。同一语义此前只活在 `migrate_check.py` 的 `check_pairs()`
（Docker 门后），本地无 Docker 时验不了；这是它的静态独立化。

### 守卫 B（alembic-chain）：alembic ↔ golang-migrate 链一致性

1. **单 head 线性链**：`alembic/versions/*.py` 的 revision 集合恰为
   `0001..N`，且 `0001.down_revision = None`、其余每 revision 的
   down_revision 恰为前一 revision——链位置与 NNNN 序号一致，alembic 的
   应用顺序与 golang-migrate 的字典序完全同构。Multiple head / 第二链根 /
   分叉 / 乱序 / 断档 / down_revision 指向不存在的 revision，逐条报出可定位
   revision。
2. **双向一一对应**：每 alembic revision 在 `db/migrations` 恰有同名一对
   `NNNN_*.up.sql / .down.sql`，反之 db/migrations 不允许出现 alembic 没有
   的版本；alembic 文件名前缀必须等于 revision 声明；SQL stem 与 alembic
   stem 逐字一致（`gen_migrations_from_alembic.py` 再生按 stem 产文件，
   改名会静默破坏再生）。
3. 解析器只吃模块级赋值行（`revision: str = "0022"` /
   `down_revision = "0026"` / `down_revision: Union[str, None] = None` 三种
   仓内真实形态），文档串里的提法（0022 的「down_revision 改为 '0021'…」）
   不构成声明，不误报。

### 与既有检查的分工（不重复造轮子）

| 检查 | 归属 | migratechain 是否复检 |
| --- | --- | --- |
| 命名合规 / up/down 成对 / 非空 down / 子目录 | `check_pairs.py` | 否（映射存在性断言除外） |
| 版本前缀重复 | `check_pairs.py` + 守卫 A | 双报（纵深防御，同一根因） |
| 版本序列 0001..N 连续 | `migrate_check.py check_pairs()`（Docker 门后） | 守卫 A 独立化（免 Docker） |
| alembic 单 head 线性链 | （仅 alembic 运行时报） | 守卫 B 静态化 |
| alembic↔golang-migrate 一一对应 | （仅 parity 运行时间接验证） | 守卫 B 静态化 |
| 迁移可逆 / parity / append-only | `migrate_check.py`（需 Docker） | 否（运行时面，不替代） |

### 接线建议（Makefile 为人类专属，agent 禁改——留 owner 落地）

migratechain 零依赖、亚秒级，建议二选一：

1. **CI**：在 `ci.yml` 的 `go-check` job（现跑 `make check-go sql-pairs`）
   追加一步 `go run ./tools/sql/migratechain`；
2. **本地**：由有 Makefile 权限者把同命令并进 `check-go` 或 `sql-pairs` 之后
   的目标串（如 `check-go: sqlc-diff go-fmt go-build go-test go-boundary
   baml-golden-check go-errcheck` 后追加）。

注意：`go test ./tools/sql/migratechain/` 里的实仓冒烟用例
（`TestRealRepo_Head_IsGreen`）已随 `make go-test`（`go test ./... -race`）
进 gate——仓库迁移链一旦破坏，单元测试先于 CI 守卫红，接线前已有兜底。
