"""T-W1-001 数据库连接烟测。

验收标准 #4：tests/conftest.py 提供 async_session fixture，可连接 PostgreSQL
并执行 SELECT 1。
"""
from __future__ import annotations

from sqlalchemy import text


async def test_async_session_can_connect(async_session):
    """async_session fixture 可执行 SELECT 1。"""
    result = await async_session.execute(text("SELECT 1"))
    row = result.scalar_one()
    assert row == 1


async def test_db_is_postgresql(async_session):
    """确认连接的是 PostgreSQL（非 SQLite）。

    为什么必须 PG：D2 的 DB 触发器强制、JSONB、DEFERRABLE 外键均依赖 PG 特性。
    """
    result = await async_session.execute(text("SELECT current_database(), version()"))
    row = result.one()
    assert "postgresql" in row[1].lower(), f"非 PostgreSQL：{row[1]}"


async def test_db_supports_jsonb(async_session):
    """JSONB 是契约 §2.2 六大块字段的承载类型，必须可用。"""
    result = await async_session.execute(
        text("SELECT '{}'::jsonb, '{\"a\": 1}'::jsonb->'a'")
    )
    row = result.one()
    assert row[0] == {}
    assert row[1] == 1
