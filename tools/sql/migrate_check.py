#!/usr/bin/env python
"""T-W5-032 全链校验：SQL 迁移与 alembic 语义等价 + 可逆性 + append-only 行为。

三段验收（对应任务卡验收标准 #2/#3/#4）：
1. parity：alembic upgrade head（库 A） vs go migrate up（库 B）→
   pg_dump --schema-only 逐语句 diff 为空（排除双方自带账本表）。
2. cycle：库 B down 全量→0 → up→head（E2E-9 的 Go 侧事实源）。
3. append-only：四张账表 UPDATE/DELETE 必须抛 'append-only' 异常
   （触发器为 FOR EACH STATEMENT，空谓词亦触发，无需种子数据）。

用法：
    python tools/sql/migrate_check.py --admin-dsn "postgresql://user:pass@localhost:5432/postgres"
    # 本地无 docker：--admin-dsn "postgresql://postgres@/postgres?host=/socket/dir"
    #           --pg-dump /path/to/pg_dump（pgserver 场景）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_TABLES = ("response_event", "gate_certificate", "gate_run", "gate_verdict")
N_STEPS = 22


def sh(cmd: list[str], **kw: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, **kw)  # type: ignore[arg-type]


def must(cmd: list[str], what: str) -> None:
    r = sh(cmd)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"❌ {what} 失败")


def psql(admin_dsn: str, sql: str) -> None:
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql)


def db_dsn(admin_dsn: str, dbname: str) -> str:
    base, _, query = admin_dsn.partition("?")
    base = base.rstrip("/")
    sep = "&" if query else ""
    return f"{base}/{dbname}?{query}{sep}" if query else f"{base}/{dbname}"


def alembic_up(dbname: str, admin_dsn: str) -> None:
    env = dict(os.environ)
    m = re.match(r".*?@([^:/?]*)(?::(\d+))?/?\??(?:host=([^&]*))?", admin_dsn)
    host = (m.group(3) or m.group(1) or "localhost") if m else "localhost"
    port = (m.group(2) or "5432") if m else "5432"
    env.update(
        {
            "POSTGRES_USER": "postgres" if "postgres@" in admin_dsn else env.get("POSTGRES_USER", "postgres"),
            "POSTGRES_PASSWORD": env.get("POSTGRES_PASSWORD", "migrate-check"),
            "POSTGRES_DB": dbname,
            "POSTGRES_HOST": host,
            "POSTGRES_PORT": port,
        }
    )
    # socket 场景 host 是路径：env.py 拼不出 socket URL，直接注入 query 形式 URL
    if host.startswith("/"):
        env["ALEMBIC_DATABASE_URL"] = db_dsn(admin_dsn, dbname)
    r = sh([str(PROJECT_ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=env)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"❌ alembic upgrade head 失败（{dbname}）")


def go_migrate(dsn: str, *args: str) -> str:
    r = sh(["go", "run", "./tools/migrate", "-dsn", dsn, *args])
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"❌ go migrate {' '.join(args)} 失败")
    return r.stdout


def dump_schema(dsn: str, pg_dump: str, dbname: str) -> list[str]:
    host = ""
    port = "5432"
    m = re.match(r".*?@([^:/?]*)(?::(\d+))?/?\??(?:host=([^&]*))?", dsn)
    if m:
        host = m.group(3) or m.group(1) or "localhost"
        port = m.group(2) or "5432"
    cmd = [pg_dump, "--schema-only", "--no-owner", "--no-privileges", "-h", host, "-p", port, dbname]
    r = sh(cmd)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(f"❌ pg_dump {dbname} 失败")
    stmts = [s.strip() for s in r.stdout.split(";") if s.strip()]
    return [
        s
        for s in stmts
        if "alembic_version" not in s and "schema_migrations" not in s and not re.match(r"^(SET|\\connect)", s)
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-dsn", required=True, help="管理员库 DSN（postgres 库，用于建删 scratch 库）")
    ap.add_argument("--pg-dump", default="pg_dump")
    args = ap.parse_args()

    go_db, py_db = "mig_check_go", "mig_check_py"
    for db in (go_db, py_db):
        psql(args.admin_dsn, f'DROP DATABASE IF EXISTS "{db}";')
        psql(args.admin_dsn, f'CREATE DATABASE "{db}";')
    go_dsn = db_dsn(args.admin_dsn, go_db)

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

    print("== 2/3 cycle：down 全量 → up 全量 ==")
    out = go_migrate(go_dsn, "down", str(N_STEPS))
    assert "version: 0" in out or "全部回滚" in out, out
    out = go_migrate(go_dsn, "up")
    print(f"✅ {out.strip()}")

    print("== 3/3 append-only：四账表 UPDATE/DELETE 必须被拒 ==")
    import psycopg

    with psycopg.connect(go_dsn, autocommit=True) as conn:
        for t in LEDGER_TABLES:
            for op_sql in (f"UPDATE {t} SET created_at = created_at WHERE false;", f"DELETE FROM {t} WHERE false;"):
                try:
                    conn.execute(op_sql)
                except psycopg.errors.RaiseException as e:
                    assert "append-only" in str(e), f"{t} 拒绝了但消息不符: {e}"
                    continue
                raise SystemExit(f"❌ {op_sql} 未被 append-only 触发器拦截")
    print("✅ 4 表 × UPDATE/DELETE 全部被拒")

    for db in (go_db, py_db):
        psql(args.admin_dsn, f'DROP DATABASE IF EXISTS "{db}";')
    print("✅ T-W5-032 全链校验通过")


if __name__ == "__main__":
    main()
