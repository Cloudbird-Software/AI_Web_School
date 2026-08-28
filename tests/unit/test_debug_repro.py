"""临时调试（修复后移除）：0030 spec_table 触发器 text->>unknown 复现.

T-W5-018 PR #103 CI 红的根因定位载体：原始 asyncpg 连接直插 spec_table，
打印异常的 message/detail/context——PG 对 plpgsql 触发器错误会带
「PL/pgSQL function <fn>() line <N>」context，可精确归因到函数与行号。
"""
from __future__ import annotations

import asyncio
import os

import pytest


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def test_debug_spec_table_trigger_context() -> None:
    """与 test_orm_can_persist_and_query_spec_table 同参直插，打印完整错误上下文."""

    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute(
                "INSERT INTO spec_table (spec_table_id, spec_table_version, "
                "gradeband, graph_release, cells, created_by) "
                "VALUES ($1, $2, $3, $4, $5::JSONB, $6)",
                "dbg-repro-1",
                "1.0.0",
                "M",
                "graph-math-2026q1",
                '[{"content_code": "math.nal.decimal.compare", '
                '"cognitive_level": "remember", "target_count": 3, '
                '"difficulty_min": 0.5, "difficulty_max": 0.8}]',
                "dbg",
            )
            print("=== DEBUG: insert succeeded（未复现，触发器语义需另查） ===")
        except Exception as e:  # noqa: BLE001 —— 调试探针原样透传
            print("=== DEBUG type ===", type(e).__module__, type(e).__name__)
            print("=== DEBUG message ===", e)
            print("=== DEBUG detail ===", getattr(e, "detail", None))
            print("=== DEBUG context ===", getattr(e, "context", None))
            print(
                "=== DEBUG query ===",
                getattr(getattr(e, "query", None), "query", None) or getattr(e, "query", None),
            )
            raise
        finally:
            await conn.close()

    asyncio.run(_run())
