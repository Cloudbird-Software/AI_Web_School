"""T-W1-001 全局 pytest fixture。

提供 PostgreSQL 异步会话 fixture（基于 docker-compose 中的 db 服务），
后续 W1+ 任务（ORM/写入服务等）的单元测试均复用本 fixture。

为什么不用 SQLite：宪法 D2 的「DB 触发器强制」「JSONB」「DEFERRABLE 外键」
等关键约束依赖 PostgreSQL 特性，测试环境必须与生产同构。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# --- 让 tests 能 import 项目 src（同 alembic/env.py 的处理）---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# --- 最小 .env 加载器（与 alembic/env.py 一致，避免重复实现污染）---
def _load_dotenv_if_needed() -> None:
    """若 POSTGRES_USER 已在 os.environ 中，则什么都不做；
    否则从项目根 .env 读取并注入 os.environ（不覆盖已有值）。
    """
    if os.environ.get("POSTGRES_USER"):
        return
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv_if_needed()


def _build_async_dsn() -> str:
    """拼装 asyncpg DSN：postgresql+asyncpg://user:pwd@host:port/db。

    为什么与 alembic 用不同驱动：alembic 走同步 psycopg（迁移工具不需 async），
    测试与运行时走 asyncpg（FastAPI / SQLAlchemy[asyncio] 标配）。
    """
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供。"
        )
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


@pytest_asyncio.fixture(scope="session")
async def async_engine() -> AsyncIterator[AsyncEngine]:
    """会话级 AsyncEngine。

    为什么 session 级：建连接池代价高，单个测试会话复用一个引擎；
    测试间用事务回滚隔离（见 async_session fixture）。
    """
    engine = create_async_engine(_build_async_dsn(), echo=False, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def async_session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """单测试用 AsyncSession。

    为什么不用事务回滚隔离：W1 测试包含 DDL 触发器 / CHECK 约束验证，
    需要真实提交才能命中 PG 端强制逻辑；改用每测试后 TRUNCATE 清理代价
    较高且会破坏只增不改表的语义。当前测试规模小，测试间无共享状态依赖。
    后续测试规模扩大时引入 transaction-rollback 隔离层。
    """
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
