"""0030 spec_table 结构触发器回归（T-W5-018 PR #103 事故的固化防线）.

事故：validate_spec_table_cells 的身份数组赋值曾裸写
``key := cell ->> 'a' || '/' || cell ->> 'b'``——PG 对 ->> 与 || 同优先级
左结合，链式场景被解析出 ``text ->> unknown``（ UndefinedFunctionError），
spec_table INSERT 全量被拒。修复 = 显式括号（双源同步）。

本测试保留为该表达式形状的回归防线：合法 cells 必须能过触发器入账；
非法 cells（Bloom 越界/缺键/空数组）必须被拒。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _insert(conn, spec_id: str, cells: list[dict]) -> None:
    conn.execute(
        "INSERT INTO spec_table (spec_table_id, spec_table_version, "
        "gradeband, graph_release, cells, created_by) "
        "VALUES ($1, $2, $3, $4, $5::JSONB, $6)",
        spec_id, "1.0.0", "M", "graph-math-2026q1", json.dumps(cells), "regression",
    )


def test_spec_table_cells_trigger_accepts_valid_cells() -> None:
    """合法 cells 过触发器入账（0030 括号修复的回归断言）."""

    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute("BEGIN")
            _insert(conn, "regression-cells-ok", [
                {"content_code": "math.nal.decimal.compare",
                 "cognitive_level": "remember",
                 "target_count": 3,
                 "difficulty_min": 0.5,
                 "difficulty_max": 0.8},
                {"content_code": "math.nal.fraction.add",
                 "cognitive_level": "apply",
                 "target_count": 2,
                 "difficulty_min": 0.3,
                 "difficulty_max": 0.6},
            ])
            await conn.execute("ROLLBACK")
        finally:
            await conn.close()

    asyncio.run(_run())


def test_spec_table_cells_trigger_rejects_invalid_bloom() -> None:
    """Bloom 越界单元格被触发器拒绝（fail-loud 保持）。"""

    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute("BEGIN")
            with pytest.raises(asyncpg.PostgresError):
                _insert(conn, "regression-cells-bad", [
                    {"content_code": "math.nal.decimal.compare",
                     "cognitive_level": "not-a-bloom-level",
                     "target_count": 3,
                     "difficulty_min": 0.5,
                     "difficulty_max": 0.8},
                ])
            await conn.execute("ROLLBACK")
        finally:
            await conn.close()

    asyncio.run(_run())
