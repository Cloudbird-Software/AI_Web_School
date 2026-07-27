"""API 层依赖：异步 DB 会话工厂.

复用 tests/conftest.py 的 .env 加载与 DSN 拼装逻辑（避免重复实现污染），
但走独立 engine——API 进程与测试进程的连接池互不干扰。

宪法 D1：本模块仅暴露只读会话；写入路径在 src/core/content/writer.py 等服务层。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# --- .env 加载（与 tests/conftest.py 同语义；worktree 本地 .env 优先）---
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv_if_needed() -> None:
    """从 worktree 根 .env 加载 POSTGRES_* 配置（覆盖系统环境变量）.

    为什么覆盖而非 setdefault：多 worktree 并行时系统 env 可能被其他 worktree
    污染（POSTGRES_DB=muti_dev 等），导致 API 连错库。worktree .env 优先。
    """
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_dotenv_if_needed()


def _build_async_dsn() -> str:
    """拼装 asyncpg DSN：postgresql+asyncpg://user:pwd@host:port/db."""
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


# 进程级单例 engine（FastAPI 进程内复用连接池；测试通过 dependency_overrides 替换）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取进程级单例 AsyncEngine（懒建）."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            _build_async_dsn(),
            echo=False,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取进程级单例 async_sessionmaker."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一 AsyncSession.

    用法（路由）：
        async def handler(session: AsyncSession = Depends(get_async_session)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


__all__ = ["get_async_session", "get_engine", "get_session_factory"]
