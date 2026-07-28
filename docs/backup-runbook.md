# 数据库备份恢复演练手册（T-W4-040）

> 架构 v2 §6 / OPC §6.6 S9：发布前就绪——备份恢复演练。
> 脚本：`scripts/ops/backup.sh` / `scripts/ops/restore.sh` / `scripts/ops/backup_verify.py`

## 1. 前置条件

- Docker 已启动，PostgreSQL 16 容器健康（`docker compose up -d --wait`）。
- 项目根 `.env` 已配置 `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`（密码不入脚本）。
- Python 3.12 虚拟环境可用（alembic / psycopg 依赖已安装）。
- `alembic/` 迁移目录与 `docker-compose.yml` 在项目根。

## 2. 备份规程

### 2.1 命令

```bash
bash scripts/ops/backup.sh
# 自定义备份目录：
BACKUP_DIR=/tmp/bak bash scripts/ops/backup.sh
```

### 2.2 产物

- `backups/<DB_NAME>-<YYYYMMDD-HHMMSS>.dump`：pg_dump 自定义压缩格式（-Fc），含 schema + 数据。
- `backups/<DB_NAME>-<YYYYMMDD-HHMMSS>.dump.sha256`：SHA256 校验和。

### 2.3 密码纪律

`backup.sh` 通过 `docker compose exec` 在容器内执行 `pg_dump`，走 PostgreSQL local peer 认证，**无需 PGPASSWORD**。密码既不入脚本，也不入命令行参数（验收 #5）。

如需远程备份（非容器内），通过环境变量 `PGPASSWORD` 传入，不写入任何文件。

## 3. 恢复演练规程

### 3.1 命令

```bash
bash scripts/ops/restore.sh backups/<DB_NAME>-<YYYYMMDD-HHMMSS>.dump
# 保留临时库供排查：
KEEP_RESTORE_DB=1 bash scripts/ops/restore.sh backups/xxx.dump
```

### 3.2 流程（5 步）

| 步骤 | 动作 | 校验点 |
|---|---|---|
| 1 | SHA256 校验和验证 | `sha256sum -c` 通过，备份文件未损坏 |
| 2 | 创建临时库 `muti_restore_drill_<ts>_<pid>` | 临时库与源库隔离，互不影响 |
| 3 | `pg_restore --no-owner --no-privileges --clean --if-exists` | schema + 数据恢复至临时库 |
| 4 | `alembic migrate-check`（upgrade→downgrade -1→upgrade） | 迁移可逆性验证（在真实数据上） |
| 5 | `backup_verify.py` | 表数量/记录数/关键表抽样校验 |

演练结束自动清理临时库（`KEEP_RESTORE_DB=1` 可保留）。

### 3.3 关键表抽样

`backup_verify.py` 对以下关键表执行 `SELECT * LIMIT 1`，验证结构可查：

- `item` / `item_version`（统一内容模型核心）
- `response_event`（作答事件账，三本账之一）
- `gate_certificate`（校验签发账，三本账之一）
- `kp_node`（知识图谱节点）
- `paper`（卷追溯）

## 4. 演练记录

### 4.1 实测运行（2026-07-28）

**环境**：PostgreSQL 16 @ localhost:5432/muti_w4_perf（本地，Windows 11，Python 3.12.10）

**备份**：

```
== T-W4-040 数据库备份 ==
数据库: muti@localhost:5432/muti_w4_perf
输出:   backups/muti_w4_perf-20260728-135136.dump

✅ 备份完成
   文件:   backups/muti_w4_perf-20260728-135136.dump
   大小:   93465 bytes
   SHA256: 241379141977b4d527e221a9f7b0e4f842a714ce4b2342fa23decc6d4f785bd4
   校验和: backups/muti_w4_perf-20260728-135136.dump.sha256
```

**恢复演练**：

```
== T-W4-040 恢复演练 ==
源备份:   backups/muti_w4_perf-20260728-135136.dump
临时库:   muti_restore_drill_20260728-135226_15517

== [1/5] 校验和验证 ==
muti_w4_perf-20260728-135136.dump: OK

== [2/5] 创建临时数据库 muti_restore_drill_20260728-135226_15517 ==
✅ 临时库已创建

== [3/5] pg_restore 恢复至临时库 ==
✅ 恢复完成

== [4/5] alembic migrate-check（upgrade→downgrade -1→upgrade）==
  → upgrade head ✅
  → downgrade -1 ✅
  → upgrade head ✅
✅ 迁移可逆性验证通过

== [5/5] backup_verify.py 核心查询验证 ==
== backup_verify: 校验数据库 muti_restore_drill_20260728-135226_15517 ==
[1/3] 表数量校验
   实际表数: 36（含分区子表与 alembic_version）
   ✅ 全部 31 张期望表均存在
   ℹ️  response_event 分区子表: 4 个

[2/3] 记录数统计
   gate_certificate                            1
   review_policy                               1
   （其余表 0 行——测试库事务回滚隔离，仅迁移种子数据）
   合计                                        2

[3/3] 关键表抽样（SELECT * LIMIT 1）
   ✅ item: 6 列，抽样 空表
   ✅ item_version: 14 列，抽样 空表
   ✅ response_event: 13 列，抽样 空表
   ✅ gate_certificate: 7 列，抽样 有数据
   ✅ kp_node: 12 列，抽样 空表
   ✅ paper: 12 列，抽样 空表

✅ backup_verify 通过：31 表齐全，关键表可查

✅ T-W4-040 恢复演练通过
== 清理临时库 muti_restore_drill_20260728-135226_15517 ==
✅ 临时库已清理
```

**结论**：备份 → 校验和 → 恢复 → 迁移可逆 → 表完整性 → 清理，全链路通过。备份文件 93KB（含 schema + 迁移种子数据），SHA256 校验一致。

## 5. 故障排查

| 症状 | 原因 | 处置 |
|---|---|---|
| `createdb: database exists` | 临时库名冲突（并发演练） | 脚本用 `<时间戳>_<PID>` 命名，冲突概率极低；若发生，删旧库重跑 |
| `pg_restore: errors` | 对象已存在（--clean --if-exists 的 warning） | 非致命，脚本继续；致命错误会让 pipe 退出码非 0 |
| `alembic downgrade` 失败 | 某迁移不可逆（生产数据依赖） | 记录失败迁移，升级给人类；不要在生产库直接跑 migrate-check |
| `backup_verify: 缺少期望表` | 备份不完整或迁移版本不一致 | 核对源库 `alembic_version` 表与备份时的迁移头 |
| `.env: command not found` | .env 文件含 UTF-8 BOM | 用无 BOM 编码重写（`[System.IO.File]::WriteAllText` + UTF8Encoding($false)） |

## 6. 非目标（任务卡声明）

异地多活、实时备份、对象存储备份、自动故障切换——均非本卡范围。
