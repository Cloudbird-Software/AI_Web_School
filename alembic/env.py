"""Alembic 运行环境（T-W0-005）。

连接串从环境变量读取，禁止在 alembic.ini 硬编码密码（X3）：
  POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_HOST / POSTGRES_PORT

为支持直接 `alembic upgrade head`（非经 make），env.py 会在未设置环境变量时
自动从项目根的 .env 文件读取——使用最小内联加载器，避免引入 python-dotenv 依赖。

一切 DDL 走迁移，禁止手工改库（X7）。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- 让 alembic 能 import 项目 src（W1 起若需 ORM 元数据即可复用）---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# --- 最小 .env 加载器（仅当 POSTGRES_USER 未在环境里时启用）---
def _load_dotenv_if_needed() -> None:
    """若 POSTGRES_USER 已在 os.environ 中，则什么都不做；
    否则从项目根的 .env 文件读取并注入 os.environ（不覆盖已有值）。
    不引入新依赖（X8）。
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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 不覆盖已有环境变量
        os.environ.setdefault(key, value)


_load_dotenv_if_needed()


# --- 由环境变量拼装 DSN（密码永不入文件）---
def _build_dsn() -> str:
    """从 POSTGRES_* 环境变量拼装 psycopg DSN；缺项给本地默认值。"""
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供（禁止写入 alembic.ini）。"
        )
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


config = context.config

# 将运行时 DSN 注入 alembic 配置（覆盖 alembic.ini 中的空 sqlalchemy.url）
config.set_main_option("sqlalchemy.url", _build_dsn())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# W0 仅占位表，无 ORM 元数据；W1 起 OnlineTargetMetadata 在此注入。
target_metadata = None


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL 脚本，不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
