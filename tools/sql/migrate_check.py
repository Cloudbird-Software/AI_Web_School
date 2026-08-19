#!/usr/bin/env python
"""T-W5-032 全链校验：SQL 迁移与 alembic 语义等价 + 可逆性 + append-only 行为。

四段验收（对应任务卡验收标准 #1/#2/#3/#4）：
0. pairs：22 对 up/down 成对齐全，版本号连续（SQL-1）。
1. parity：alembic upgrade head（库 A） vs go migrate up（库 B）→
   pg_dump --schema-only 逐语句 diff 为空（排除双方自带账本表）；
   0012 review_policy 种子行两侧一致（schema-only dump 不含数据，单独探针）。
2. cycle：库 B down 全量→0 → up→head（E2E-9 的 Go 侧事实源）。
3. append-only：全部挂触发器的账表 UPDATE/DELETE 必须抛 'append-only' 异常
   （触发器为 FOR EACH STATEMENT，空谓词亦触发，无需种子数据）；
   D1 四张核心账表（response_event/gate_certificate/gate_run/gate_verdict）
   必须在清单内。

用法：
    # compose db（make migrate-go-check 的调用形态，pg_dump 走容器内避免宿主机
    # 无 postgres 客户端——CI runner 只保证 docker）：
    python tools/sql/migrate_check.py \
        --admin-dsn "postgresql://muti:pass@localhost:5432/muti_dev" \
        --pg-dump "docker compose exec -T db pg_dump -U muti"
    # 裸机 PostgreSQL（pg_dump 在宿主机 PATH，从 DSN 取 host/port，密码经
    # PGPASSWORD 注入）：
    python tools/sql/migrate_check.py \
        --admin-dsn "postgresql://postgres:pass@127.0.0.1:5432/postgres"
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "db" / "migrations"
ALEMBIC_DIR = PROJECT_ROOT / "alembic" / "versions"
# D1 三本账的核心账表（任务卡验收 #4 的下限；实际探针覆盖全部 append-only 表）
CORE_LEDGER_TABLES = ("response_event", "gate_certificate", "gate_run", "gate_verdict")
# 迁移数以 alembic 版本目录为准（单事实源：新增迁移时此处自动跟上）
N_STEPS = len(list(ALEMBIC_DIR.glob("*.py")))


def sh(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=env)  # type: ignore[arg-type]


def must(cmd: list[str], what: str, env: dict[str, str] | None = None) -> None:
    r = sh(cmd, env)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"❌ {what} 失败")


def psql(admin_dsn: str, sql: str) -> None:
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql)


def db_dsn(admin_dsn: str, dbname: str) -> str:
    """把 admin DSN 的库名（末段路径）换成 scratch 库名，query 参数保留。"""
    base, _, query = admin_dsn.partition("?")
    scheme_auth, _, _ = base.rpartition("/")
    dsn = f"{scheme_auth}/{dbname}"
    return f"{dsn}?{query}" if query else dsn


def _parse_dsn(dsn: str) -> tuple[str, str, str, str]:
    """从 DSN 提取 user/password/host/port（pg_dump 与 alembic env 复用）。"""
    user, password, host, port = "", "", "localhost", "5432"
    m = re.search(r"://([^:@]*)(?::([^@]*))?@([^:/?]*)(?::(\d+))?", dsn)
    if m:
        user = m.group(1) or ""
        password = m.group(2) or ""
        host = m.group(3) or host
        port = m.group(4) or port
    # query 形式 host=（unix socket）优先
    m2 = re.search(r"[?&]host=([^&]*)", dsn)
    if m2:
        host = m2.group(1)
    return user, password, host, port


def check_pairs() -> None:
    """验收 #1（SQL-1）：up/down 成对齐全 + 版本号连续 0001..N。"""
    ups = sorted(p.name[: -len(".up.sql")] for p in MIGRATIONS_DIR.glob("*.up.sql"))
    downs = sorted(p.name[: -len(".down.sql")] for p in MIGRATIONS_DIR.glob("*.down.sql"))
    if ups != downs:
        only_up = set(ups) - set(downs)
        only_down = set(downs) - set(ups)
        raise SystemExit(f"❌ up/down 不成对：仅 up={only_up} 仅 down={only_down}")
    versions = [n.split("_", 1)[0] for n in ups]
    expected = [f"{i:04d}" for i in range(1, N_STEPS + 1)]
    if versions != expected:
        raise SystemExit(f"❌ 版本号序列非 0001..{N_STEPS:04d} 连续：{versions}")
    print(f"✅ {N_STEPS} 对 up/down 成对齐全、版本号连续（SQL-1）")


def alembic_up(dbname: str, admin_dsn: str) -> None:
    user, password, host, port = _parse_dsn(admin_dsn)
    env = dict(os.environ)
    env.update(
        {
            # DSN 优先（make 上下文 env 与 DSN 同源；直连场景 env 可能是别库的）
            "POSTGRES_USER": user or env.get("POSTGRES_USER", "postgres"),
            "POSTGRES_PASSWORD": password or env.get("POSTGRES_PASSWORD", "migrate-check"),
            "POSTGRES_DB": dbname,
            "POSTGRES_HOST": host,
            "POSTGRES_PORT": port,
        }
    )
    # socket 场景 host 是路径：psycopg 原生支持 host=/socket/dir（env.py 拼
    # URL 后 psycopg 按 unix socket 解析），无需特殊处理
    must(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        f"alembic upgrade head（{dbname}）",
        env=env,
    )


def go_migrate(dsn: str, *args: str) -> str:
    r = sh(["go", "run", "./tools/migrate", "-dsn", dsn, *args])
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"❌ go migrate {' '.join(args)} 失败")
    return r.stdout


def _normalize_dump(text: str) -> list[str]:
    """归一化 pg_dump 输出为可比较的语句多重集（排序后比较）。

    - 丢弃 -- 注释行与 \\restrict/\\unrestrict 行（PG16.x 尾部随机 token，
      每次导出都不同，非 schema 语义）
    - 按分号切分后丢弃会话级 SET/set_config 与迁移工具自带账本表
      （alembic_version / schema_migrations）
    - 排序比较：pg_dump 的对象输出顺序是工具实现细节，schema 等价的判定
      标准是语句多重集相等（每条语句自含对象全名，逐语句可定位）
    """
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.startswith("--") and not ln.startswith("\\")
    )
    stmts: list[str] = []
    for raw in body.split(";"):
        s = re.sub(r"\s+", " ", raw).strip()
        if not s:
            continue
        if "alembic_version" in s or "schema_migrations" in s:
            continue
        if re.match(r"^(SET |SELECT pg_catalog\.set_config)", s):
            continue
        stmts.append(s)
    return sorted(stmts)


def dump_schema(dsn: str, pg_dump: str, dbname: str) -> list[str]:
    """pg_dump 取 schema。

    --pg-dump 单 token（如 pg_dump）：宿主机二进制，从 DSN 取 host/port，
    密码经 PGPASSWORD 注入（避免无 TTY 时交互提示）。
    多 token（如 "docker compose exec -T db pg_dump -U muti"）：命令前缀，
    容器内经 socket 连接（postgres 镜像 local trust，免密），不追加 -h/-p。
    """
    parts = shlex.split(pg_dump)
    flags = ["--schema-only", "--no-owner", "--no-privileges"]
    env = dict(os.environ)
    if len(parts) == 1:
        user, password, host, port = _parse_dsn(dsn)
        cmd = parts + flags + ["-h", host, "-p", port]
        if user:
            cmd += ["-U", user]
        cmd.append(dbname)
        if password:
            env["PGPASSWORD"] = password
    else:
        cmd = parts + flags + [dbname]
    r = sh(cmd, env=env)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"❌ pg_dump {dbname} 失败")
    return _normalize_dump(r.stdout)


def discover_append_only_tables() -> list[str]:
    """从 SQL 产物发现全部 append-only 触发器表（探针清单随迁移自动扩张）。"""
    tables: set[str] = set()
    for f in sorted(MIGRATIONS_DIR.glob("*.up.sql")):
        for m in re.finditer(r"BEFORE UPDATE OR DELETE ON ([a-z_]+)", f.read_text(encoding="utf-8")):
            tables.add(m.group(1))
    missing = set(CORE_LEDGER_TABLES) - tables
    if missing:
        raise SystemExit(f"❌ D1 核心账表缺 append-only 触发器：{missing}")
    return sorted(tables)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-dsn", required=True, help="管理员库 DSN（用于建删 scratch 库）")
    ap.add_argument("--pg-dump", default="pg_dump", help="pg_dump 命令（多 token 视为命令前缀，如 compose exec 形态）")
    args = ap.parse_args()

    print("== 0/3 pairs：up/down 成对性 ==")
    check_pairs()

    go_db, py_db = "mig_check_go", "mig_check_py"
    for db in (go_db, py_db):
        psql(args.admin_dsn, f'DROP DATABASE IF EXISTS "{db}";')
        psql(args.admin_dsn, f'CREATE DATABASE "{db}";')
    go_dsn = db_dsn(args.admin_dsn, go_db)
    py_dsn = db_dsn(args.admin_dsn, py_db)

    print("== 1/3 parity：alembic head vs go migrate head ==")
    alembic_up(py_db, args.admin_dsn)
    print(go_migrate(go_dsn, "up").strip())
    py_dump = dump_schema(args.admin_dsn, args.pg_dump, py_db)
    go_dump = dump_schema(go_dsn, args.pg_dump, go_db)
    if py_dump != go_dump:
        import difflib

        diff = "\n".join(difflib.unified_diff(py_dump, go_dump, "alembic", "go-migrate", lineterm=""))
        raise SystemExit(f"❌ schema diff 非空：\n{diff[:4000]}")
    print(f"✅ schema 逐语句一致（{len(py_dump)} 语句）")

    # 种子数据一致性：0012 review_policy 是唯一迁移内 INSERT，schema-only
    # dump 看不到，单独两侧对数
    import psycopg

    with psycopg.connect(py_dsn) as py_conn, psycopg.connect(go_dsn) as go_conn:
        py_n = py_conn.execute("SELECT count(*) FROM review_policy").fetchone()[0]
        go_n = go_conn.execute("SELECT count(*) FROM review_policy").fetchone()[0]
    if (py_n, go_n) != (1, 1):
        raise SystemExit(f"❌ review_policy 种子行不一致：alembic={py_n} go={go_n}（期望各 1）")
    print("✅ review_policy 种子行两侧各 1（0012 INSERT 等价）")

    # parity 已完成，立即释放 py 库：pii_vault_reader 是集群级角色，若别的库
    # （本脚本的 py 库、或集群里其他已 upgrade 的库）还留着对它的 ACL 依赖，
    # go 侧 down 0014 的 DROP ROLE 会因跨库共享依赖而失败
    psql(args.admin_dsn, f'DROP DATABASE IF EXISTS "{py_db}";')

    print("== 2/3 cycle：down 全量 → up 全量 ==")
    out = go_migrate(go_dsn, "down", str(N_STEPS))
    if "version: 0" not in out and "全部回滚" not in out:
        raise SystemExit(f"❌ down 未回到 0：{out}")
    out = go_migrate(go_dsn, "up")
    print(f"✅ {out.strip()}")

    print("== 3/3 append-only：账表 UPDATE/DELETE 必须被拒 ==")
    tables = discover_append_only_tables()
    with psycopg.connect(go_dsn, autocommit=True) as conn:
        for t in tables:
            # UPDATE 探针的 SET 列不能假设 created_at 存在（review_policy 等
            # 无该列），且 GENERATED ALWAYS 身份列/生成列 SET 自身会先于触发
            # 器报错——优先 created_at，否则取首个普通列；触发器是 FOR EACH
            # STATEMENT，空谓词亦触发，不依赖行数据
            col = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "AND is_generated = 'NEVER' "
                "AND COALESCE(identity_generation, 'NO') = 'NO' "
                "ORDER BY (column_name = 'created_at') DESC, ordinal_position LIMIT 1",
                (t,),
            ).fetchone()
            if col is None:
                raise SystemExit(f"❌ 表 {t} 不存在或无普通列可作探针（触发器清单与 schema 脱节）")
            probes = (f"UPDATE {t} SET {col[0]} = {col[0]} WHERE false;", f"DELETE FROM {t} WHERE false;")
            for op_sql in probes:
                try:
                    conn.execute(op_sql)
                except psycopg.errors.RaiseException as e:
                    if "append-only" not in str(e):
                        raise SystemExit(f"❌ {t} 拒绝了但消息不符: {e}")
                    continue
                raise SystemExit(f"❌ {op_sql} 未被 append-only 触发器拦截")
    print(f"✅ {len(tables)} 张 append-only 表 × UPDATE/DELETE 全部被拒（含 D1 四核心账表）")

    for db in (go_db, py_db):
        psql(args.admin_dsn, f'DROP DATABASE IF EXISTS "{db}";')
    print("✅ T-W5-032 全链校验通过")


if __name__ == "__main__":
    main()
