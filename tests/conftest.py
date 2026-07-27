"""T-W1-001 全局 pytest fixture；T-W2-019 引入事务回滚隔离。

提供 PostgreSQL 异步会话 fixture（基于 docker-compose 中的 db 服务），
后续 W1+ 任务（ORM/写入服务等）的单元测试均复用本 fixture。

为什么不用 SQLite：宪法 D2 的「DB 触发器强制」「JSONB」「DEFERRABLE 外键」
等关键约束依赖 PostgreSQL 特性，测试环境必须与生产同构。

T-W2-019 测试隔离：async_session 通过「外层事务 + SAVEPOINT」实现测试间隔离。
- 测试 A 的写入在 SAVEPOINT 内；session.commit() 退化为 RELEASE SAVEPOINT
  （数据对当前外层事务可见，但不持久化到 DB）
- 测试结束后 ROLLBACK 外层事务，所有写入丢弃
- 测试可重复运行，不污染开发/测试数据库，可并发
- PostgreSQL 的 DDL 触发器、CHECK 约束在 SAVEPOINT 内同样生效（与原 W1 测试兼容）

W2a-integrate 调整：
- async_engine 改为 function 级（原 session 级在 sync/async 混合测试时连接会被
  event loop 切换关闭）。
- async_session teardown rollback 容错：某些测试使用独立 engine 执行会被 DB
  拒绝的 SQL，连接关闭会导致外层事务 rollback 抛 InterfaceError，捕获避免掩盖
  测试结果。
- 移除僵尸连接清理（原 subprocess 调 docker compose exec 在 Windows 下可能卡住）：
  test_gate_bypass.py 的 TRUNCATE 已改用独立连接真正提交，不再持锁跨测试；
  PostgreSQL 在连接关闭时自动回滚事务释放锁，无需外部清理。
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
    """从 worktree 根 .env 加载配置，覆盖系统环境变量。

    为什么覆盖而不是 setdefault：
    - 多 worktree 并行开发时，系统环境变量可能被其他 worktree 的 `make` export
      污染（POSTGRES_DB=muti_dev 等），导致本 worktree 测试连到错误数据库。
    - 每个 worktree 应有独立 .env，指定独立测试数据库（POSTGRES_DB 互斥）。
    - .env 已在 .gitignore 中，是 worktree 本地配置，应优先于系统环境变量。
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # 覆盖系统环境变量：worktree .env 优先
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


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


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    """函数级 AsyncEngine。

    为什么 function 级（W2a-integrate 调整）：
    - 原 session 级在 sync 测试与 async 测试混合运行时，event loop 跨测试切换
      会导致 asyncpg 连接在中途被关闭（ConnectionDoesNotExistError）。
    - function 级每个测试自建自毁引擎，连接池不跨测试复用，规避连接陈旧问题；
      建引擎本身代价低（懒建连接），整体测试耗时影响可接受。
    - 测试间隔离仍由 async_session 的事务回滚保证（见下）。

    W2a-integrate 移除僵尸连接清理：
    - 原实现用 subprocess.run 调用 docker compose exec psql 终止残留连接，
      但在 Windows + Docker Desktop 环境下 subprocess 可能卡住（Docker 未响应
      时 timeout 不生效），导致整个 pytest 卡死。
    - 实际上僵尸连接清理已不必要：
      (1) test_gate_bypass.py 的 _truncate_gate_tables 已改用独立连接真正提交
          TRUNCATE，不再在 savepoint 内持有 ACCESS EXCLUSIVE 锁跨测试。
      (2) 其他测试文件的 TRUNCATE 在 savepoint 内执行，async_session teardown
          时外层事务回滚释放锁。
      (3) 即使 rollback 失败（连接关闭），PostgreSQL 在连接关闭时自动回滚
          事务释放所有锁，不会留下持锁的僵尸事务。
    """
    engine = create_async_engine(_build_async_dsn(), echo=False, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def async_session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """单测试用 AsyncSession，事务回滚隔离（T-W2-019）。

    实现：在连接上 BEGIN 外层事务，AsyncSession 通过
    ``join_transaction_mode="create_savepoint"`` 加入外层事务——
    - session.commit() 退化为 RELEASE SAVEPOINT（写入仅对外层事务可见，
      不持久化到 DB）
    - session.rollback() 退化为 ROLLBACK TO SAVEPOINT
    测试结束后 ROLLBACK 外层事务，所有写入丢弃，下一个测试从干净状态开始。

    为什么 PostgreSQL 的 DDL 触发器与 CHECK 约束在此模式下仍然生效：
    - AFTER/BEFORE INSERT 触发器在 INSERT 语句执行时同步触发（非 commit 时），
      SAVEPOINT 内 INSERT 同样触发，验证逻辑不受影响。
    - CHECK 约束在 PostgreSQL 中不可 DEFERRABLE，INSERT 语句执行时即校验，
      SAVEPOINT 内违反约束同样立即抛 IntegrityError。
    - DEFERRABLE INITIALLY DEFERRED 外键在 SAVEPOINT RELEASE 时校验，
      原 W1 循环外键测试（互引插入）在 SAVEPOINT 内仍可通过。

    为什么改用事务回滚（取代 W1 注释中「每测试后 TRUNCATE」方案）：
    TRUNCATE 破坏只增不改表的语义、代价高且不可与并发测试共用 DB；
    事务回滚是 SQLAlchemy 官方推荐的测试隔离模式，且天然支持并发。

    W2a-integrate：rollback 容错。某些测试（如 test_gate_bypass）使用独立的
    serving_reader_engine 执行会被 DB 拒绝的 SQL，DB 拒绝可能触发 asyncpg
    关闭同一进程内的其他连接（推测是 asyncpg 连接池的级联失效）。此时外层
    事务的 rollback 会抛 InterfaceError（connection is closed）。本 fixture
    捕获该异常，避免 teardown 报错掩盖测试本身的结果。
    """
    async with async_engine.connect() as connection:
        # 外层事务：所有测试写入都封装在此事务内，结束时整体回滚
        transaction = await connection.begin()
        try:
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session
        finally:
            try:
                await transaction.rollback()
            except Exception as exc:
                # 连接已被关闭（级联失效或僵尸清理），rollback 失败可接受——
                # 事务本身会随连接关闭而自动回滚，无需再显式 rollback
                print(f"[async_session teardown] rollback failed (acceptable): {exc}", flush=True)


@pytest_asyncio.fixture
async def committed_session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """需要真实持久化提交的测试用 AsyncSession（不隔离）。

    何时使用：极少数测试需要验证跨连接/跨事务的可见性（如测试隔离元测试
    自身需要往 DB 注入「脏数据」并保留）——常规业务测试禁止使用本 fixture，
    一律走 async_session 的事务回滚隔离。

    为什么不与 async_session 共享连接：committed_session 用独立连接 + 独立
    事务，commit 后数据真实持久化到 DB；调用方需自行在 teardown 清理。
    """
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
