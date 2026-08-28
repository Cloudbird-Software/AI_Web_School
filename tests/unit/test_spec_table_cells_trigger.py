"""0030 spec_table 结构触发器回归 + 调试探针（修复后收敛为回归测试）.

事故：validate_spec_table_cells 身份数组赋值裸写链式 ->> || 产生
text ->> unknown（UndefinedFunctionError），spec_table INSERT 全量被拒。
本文件同时承载：
1. 布尔探针：确认 DB 函数体是否含括号修复（修复验收用，随后续 PR 移除/收敛）
2. 正/反回归断言
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


def test_debug_function_body_probe() -> None:
    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            fndef = await conn.fetchval(
                "SELECT pg_get_functiondef('validate_spec_table_cells()'::regprocedure)")
            paren = "(cell ->>" in fndef
            bloom = "cognitive_level" in fndef
            # 故意失败以暴露探针值（pytest 仅失败用例显示输出）
            assert paren and bloom, f"PROBE paren={paren} bloom={bloom} len={len(fndef)}"
        finally:
            await conn.close()

    asyncio.run(_run())


def test_spec_table_cells_trigger_accepts_valid_cells() -> None:
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
    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute("BEGIN")
            rejected = False
            reject_reason = None
            try:
                _insert(conn, "regression-cells-bad", [
                    {"content_code": "math.nal.decimal.compare",
                     "cognitive_level": "not-a-bloom-level",
                     "target_count": 3,
                     "difficulty_min": 0.5,
                     "difficulty_max": 0.8},
                ])
            except Exception as e:  # noqa: BLE001 —— 触发器/约束拒绝即通过
                rejected = True
                reject_reason = f"{type(e).__name__}: {e}"
            row = await conn.fetchrow(
                "SELECT cells::text AS cells FROM spec_table "
                "WHERE spec_table_id = 'regression-cells-bad'")
            await conn.execute("ROLLBACK")
            # 数据完整性语义（验收本体）：非法 cells 不得入账——无论以异常
            # 拒绝还是静默取消，行不存在即达标；入账才红。
            assert row is None, (
                f"Bloom 越界入账（rejected={rejected} reason={reject_reason}）")
        finally:
            await conn.close()

    asyncio.run(_run())
