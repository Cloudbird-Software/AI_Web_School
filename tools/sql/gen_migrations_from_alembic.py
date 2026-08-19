#!/usr/bin/env python
"""从 alembic 在线运行捕获真实 DDL，生成 golang-migrate 纯 SQL 迁移（T-W5-032）。

为什么用"进程内事件捕获"而不是手写或离线 --sql：
1. 语义零偏差硬约束——捕获的就是 alembic 对真库执行的原句（含 checkfirst
   动态决策：如枚举已存在则跳过 CREATE TYPE，与逐版本重放的顺序完全一致）；
2. 离线 --sql 在 checkfirst 探测（op.get_bind().execute().scalar()）处崩溃，
   且 bind param 渲染为 NULL，产物不可信；
3. 唯一被排除的语句是 alembic_version 账本维护与 checkfirst SELECT 探测——
   golang-migrate 用自己的 schema_migrations 账本，语义由"同样的 DDL 作用于
   同样的空库序列"保证。

捕获文本两次归一化（还原为服务器实际收到的语句）：
- SQLAlchemy 对 psycopg（pyformat 参数风格）会把语句中的字面 `%` 转义成
  `%%`（psycopg 执行时再还原）；捕获点在转义之后、psycopg 还原之前，因此
  落盘前必须 `%%`→`%` 反转义，否则 golang-migrate 原样回放会改变语义
  （如 0003 分区 DO 块的 format('%I') 会被当成字面量）。
- 带 bind param 的语句（如 0012 review_policy 种子 INSERT）用 literal_binds
  重新编译成字面量 SQL。

实现：monkeypatch sqlalchemy.engine_from_config（alembic env.py 以
`from sqlalchemy import engine_from_config` 引用，加载时取的是本模块已替换的
命名空间属性），在引擎上挂 before_cursor_execute 事件，按执行顺序记录语句。

用法（本地生成，产物入库，CI 只校验不重生成）：
    python tools/sql/gen_migrations_from_alembic.py \
        --admin-dsn "postgresql://postgres:pass@localhost:5432/postgres" \
        --out db/migrations
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 只保留 DDL + 迁移内数据语句（0012 review_policy 种子 INSERT）；排除 alembic
# 自身账本与 checkfirst 探测
KEEP_PREFIXES = ("CREATE", "DROP", "ALTER", "DO", "COMMENT", "GRANT", "REVOKE", "INSERT")
EXCLUDE_SUBSTR = ("alembic_version",)
# 已知无害的会话/探测语句：静默丢弃；其余被丢弃的语句打印告警，防止语义
# 静默丢失（教训：0012 的种子 INSERT 曾被 DDL 前缀过滤默默吞掉）
BENIGN_PREFIXES = ("SELECT", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET", "SHOW", "DEALLOCATE")

N_STEPS = 22

_CAPTURED: list[tuple[str, tuple | dict]] = []


def _render_literal(statement: str, params: tuple | dict) -> str:
    """把带 bind param 的语句重编译为字面量 SQL（如 0012 的种子 INSERT）。

    捕获到的语句是 psycopg pyformat 形态（`%(name)s`），参数在独立 dict 里。
    不能走 SQLAlchemy text() 重编译：占位符后跟 `::VARCHAR` 强转时 text()
    的参数解析会跳过该占位符（负向断言把 `::` 当 cast），导致 bindparams
    报"未定义参数"。改用 psycopg.sql.Literal 逐值做字面量替换，引用/转义
    由 psycopg 自己保证。位置参数（`%s` + tuple）当前 22 个迁移中不存在，
    告警放弃渲染。
    """
    if not params:
        return statement
    if not isinstance(params, dict):
        print(f"⚠️ 位置参数语句未做字面量渲染，请人工核验：{statement[:80]}", file=sys.stderr)
        return statement
    import psycopg.sql

    return re.sub(
        r"%\((\w+)\)s",
        lambda m: psycopg.sql.Literal(params[m.group(1)]).as_string(None),
        statement,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="db/migrations")
    ap.add_argument("--admin-dsn", required=True, help="管理员库 DSN（postgres 库，用于建删 scratch 库）")
    args = ap.parse_args()
    out_dir = PROJECT_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    import psycopg

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

    import os

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))

    def drop_create(db: str) -> None:
        with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}"')
            conn.execute(f'CREATE DATABASE "{db}"')

    def drop(db: str) -> None:
        with psycopg.connect(args.admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}"')

    def alembic(db: str, op: str, rev: str) -> list[str]:
        base = re.match(r"^postgresql://([^@]+)@([^/:]+):(\d+)/", args.admin_dsn)
        if not base:
            raise SystemExit(f"❌ --admin-dsn 须为 TCP 形态 postgresql://user:pass@host:port/db：{args.admin_dsn}")
        user, password = base.group(1).split(":", 1)
        os.environ.update(
            {
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": db,
                "POSTGRES_HOST": base.group(2),
                "POSTGRES_PORT": base.group(3),
            }
        )
        before = len(_CAPTURED)
        if op == "upgrade":
            command.upgrade(cfg, rev)
        else:
            command.downgrade(cfg, rev)
        out: list[str] = []
        for stmt, params in _CAPTURED[before:]:
            s = stmt.rstrip()
            if s.endswith(";"):
                s = s[:-1]
            if any(x in s for x in EXCLUDE_SUBSTR):
                continue
            if not s.lstrip().upper().startswith(KEEP_PREFIXES):
                if not s.lstrip().upper().startswith(BENIGN_PREFIXES):
                    print(f"⚠️ 语句被过滤，请人工核验语义是否丢失：{s[:120]}", file=sys.stderr)
                continue
            s = _render_literal(s, params)
            # SQLAlchemy→psycopg 的字面 % 转义在捕获文本里是 %%，落盘前还原
            s = s.replace("%%", "%")
            out.append(s + ";")
        return out

    for n in range(1, N_STEPS + 1):
        rev = f"{n:04d}"
        prev = "base" if n == 1 else f"{n-1:04d}"
        db = f"gen_{n}"
        drop_create(db)
        try:
            # UP：先升到 prev 打底（golang-migrate 逐步重放，本文件只需增量）
            alembic(db, "upgrade", prev)
            ups = alembic(db, "upgrade", rev)
            # DOWN：n→prev（同库已到 n，直接降）
            downs = alembic(db, "downgrade", prev)
        finally:
            drop(db)

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
