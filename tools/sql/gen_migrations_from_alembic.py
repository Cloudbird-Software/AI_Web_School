#!/usr/bin/env python
"""从 alembic 在线运行捕获真实 DDL，生成 golang-migrate 纯 SQL 迁移（T-W5-032）。

为什么用"进程内事件捕获"而不是手写或离线 --sql：
1. 语义零偏差硬约束——捕获的就是 alembic 对真库执行的原句（含 checkfirst
   动态决策：如枚举已存在则跳过 CREATE TYPE，与逐版本重放的顺序完全一致）；
2. 离线 --sql 在 checkfirst 探测（op.get_bind().execute().scalar()）处崩溃，
   且 bind param 渲染为 NULL，产物不可信；
3. 唯一被排除的语句是 alembic_version 账本维护与 SELECT 探测——golang-migrate
   用自己的 schema_migrations 账本，语义由"同样的 DDL 作用于同样的空库序列"保证。

实现：monkeypatch sqlalchemy.engine_from_config（alembic env.py 以
`from sqlalchemy import engine_from_config` 引用，加载时取的是本模块已替换的
命名空间属性），在引擎上挂 before_cursor_execute 事件，按执行顺序记录语句。

用法（本地生成，产物入库，CI 只校验不重生成）：
    python tools/sql/gen_migrations_from_alembic.py --out db/migrations

依赖：pip install pgserver（开发机工具，不进 requirements）。
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 只保留 DDL；排除 alembic 自身账本与 checkfirst 探测
DDL_PREFIXES = ("CREATE", "DROP", "ALTER", "DO", "COMMENT", "GRANT", "REVOKE")
EXCLUDE_SUBSTR = ("alembic_version",)

N_STEPS = 22

_CAPTURED: list[tuple[str, tuple | dict]] = []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="db/migrations")
    args = ap.parse_args()
    out_dir = PROJECT_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    import pgserver

    server = pgserver.get_server(str(PROJECT_ROOT / ".pytest_tmp/pgserver_gen"), cleanup_mode=None)
    uri = server.get_uri()  # postgresql://postgres:@/postgres?host=<sock>
    socket_dir = uri.split("host=")[1].split("&")[0]
    port = uri.split("port=")[1].split("&")[0] if "port=" in uri else "5432"
    # psycopg：host 以 / 开头 = socket 目录。SQLAlchemy URL 语法要求把路径里的
    # "/" 编码为 %2F，解析后再传给 psycopg 即还原为 socket 目录。
    host = socket_dir.replace("/", "%2F")

    # 安装捕获（必须在 alembic/env.py 被加载之前）
    import sqlalchemy

    _orig = sqlalchemy.engine_from_config

    def tracking_engine_from_config(*a, **k):
        from sqlalchemy import event

        eng = _orig(*a, **k)

        @event.listens_for(eng, "before_cursor_execute")
        def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
            _CAPTURED.append((statement, parameters))

        return eng

    sqlalchemy.engine_from_config = tracking_engine_from_config

    slugs: dict[str, str] = {}
    for f in sorted((PROJECT_ROOT / "alembic/versions").glob("*.py")):
        num, _, slug = f.stem.partition("_")
        slugs[num] = slug

    from alembic import command
    from alembic.config import Config

    # socket 连接：env.py 拼出的 URL 语法装不下路径型 host。这里把
    # ...@%2Fsock%2Fdir:5432/db 重写为 SQLAlchemy 支持的 query 形式
    # ...@/db?host=/sock/dir&port=5432（query 参数原样传给 psycopg，
    # psycopg 的 host 以 / 开头即 socket 目录）。仅影响本进程。
    _orig_set_main = Config.set_main_option

    def _safe_set_main(self, name, value):  # type: ignore[no-untyped-def]
        if name == "sqlalchemy.url" and isinstance(value, str) and "%2F" in value:
            m = re.match(r"^(.*)@%2F([^:]*):(\d+)/(.*)$", value)
            if m:
                value = f"{m.group(1)}@/{m.group(4)}?host=/{m.group(2)}&port={m.group(3)}"
            # configparser 插值把 % 当特殊字符（URL 归一化后 host 仍带 %2F）
            value = value.replace("%", "%%")
        return _orig_set_main(self, name, value)

    Config.set_main_option = _safe_set_main  # type: ignore[method-assign]

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))

    def alembic(db: str, op: str, rev: str) -> list[str]:
        os.environ.update(
            {
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "pgserver-trust-auth",  # trust 认证占位，永不入库
                "POSTGRES_DB": db,
                "POSTGRES_HOST": host,  # %2F 编码的 socket 目录
                "POSTGRES_PORT": port,
            }
        )
        before = len(_CAPTURED)
        if op == "upgrade":
            command.upgrade(cfg, rev)
        else:
            command.downgrade(cfg, rev)
        out: list[str] = []
        for stmt, _p in _CAPTURED[before:]:
            s = stmt.rstrip()
            if s.endswith(";"):
                s = s[:-1]
            if not s.lstrip().upper().startswith(DDL_PREFIXES):
                continue
            if any(x in s for x in EXCLUDE_SUBSTR):
                continue
            out.append(s + ";")
        return out

    for n in range(1, N_STEPS + 1):
        rev = f"{n:04d}"
        prev = "base" if n == 1 else f"{n-1:04d}"
        db = f"gen_{n}"
        server.psql(f"DROP DATABASE IF EXISTS {db};")
        server.psql(f"CREATE DATABASE {db};")
        try:
            # UP：先升到 prev 打底（golang-migrate 逐步重放，本文件只需增量）
            alembic(db, "upgrade", prev)
            ups = alembic(db, "upgrade", rev)
            # DOWN：n→prev（同库已到 n，直接降）
            downs = alembic(db, "downgrade", prev)
        finally:
            server.psql(f"DROP DATABASE IF EXISTS {db};")

        slug = slugs.get(rev, "step")
        header = (
            f"-- T-W5-032: 由 alembic {rev}（{slug}.py）在线捕获生成（语义零变更，"
            f"tools/sql/gen_migrations_from_alembic.py）；禁止手改。\n"
        )
        (out_dir / f"{rev}_{slug}.up.sql").write_text(header + "\n".join(ups) + "\n", encoding="utf-8")
        (out_dir / f"{rev}_{slug}.down.sql").write_text(header + "\n".join(downs) + "\n", encoding="utf-8")
        print(f"{rev}_{slug}: up={len(ups)} stmts, down={len(downs)} stmts")

    print(f"✅ 生成 {N_STEPS} 对迁移 → {out_dir}")


if __name__ == "__main__":
    main()
