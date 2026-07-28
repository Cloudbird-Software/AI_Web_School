#!/usr/bin/env python3
"""T-W4-040 备份完整性校验：表数量 / 记录数 / 关键表抽样.

验收（任务卡 T-W4-040 §验收 #3）：
    backup_verify.py 校验备份完整性（表数量/记录数/关键表抽样）。

连接至 POSTGRES_DB 指定的数据库（恢复演练时指向临时库），
校验：
1. 表数量：与期望表清单比对（核心域 + 知识图谱 + 卷追溯 + 曝光 + 评分 + 合规）。
2. 记录数：每张表的行数（空表合法，仅记录供对比）。
3. 关键表抽样：item / item_version / response_event / gate_certificate /
   kp_node / paper 的结构可查询（SELECT LIMIT 1 不报错）。

用法：
    POSTGRES_DB=muti_restore_drill_xxx python scripts/ops/backup_verify.py
    python scripts/ops/backup_verify.py  # 用 .env 默认 DB

密码纪律（验收 #5）：DSN 从环境变量拼装，不硬编码密码（X3）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 让脚本能 import 项目 src（复用 alembic env.py 的 .env 加载逻辑）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg  # noqa: E402

# ────────────────────────────────────────────────────────────────────
# 期望表清单（与 alembic 迁移 0001-0016 对齐；含分区表动态发现）
# ────────────────────────────────────────────────────────────────────

# 核心表（非分区、非 alembic_version）
EXPECTED_TABLES: list[str] = [
    # 内容模型核心
    "item", "item_version", "item_template", "item_template_version",
    "item_group", "item_kp",
    # 素材
    "material", "material_version", "material_license",
    # 语料库
    "corpus_asset", "corpus_version",
    # 知识图谱
    "kp_node", "kp_edge", "kp_closure", "relation_type", "graph_release",
    # 卷追溯
    "paper", "paper_item",
    # 曝光账本
    "paper_exposure", "student_exposure",
    # 校验域
    "gate_certificate", "gate_run", "gate_verdict",
    # 会话
    "practice_session",
    # 作答事件（主表，分区表动态发现）
    "response_event",
    # 数据域
    "item_param", "estimator_run",
    # 合规
    "parental_consent", "publication",
    # 复习
    "review_policy", "review_queue_entry",
]

# 关键表抽样（SELECT * LIMIT 1 验证结构可查）
KEY_TABLES_SAMPLE: list[str] = [
    "item",
    "item_version",
    "response_event",
    "gate_certificate",
    "kp_node",
    "paper",
]


# ────────────────────────────────────────────────────────────────────
# .env 加载（与 alembic/env.py 一致，避免引入新依赖）
# ────────────────────────────────────────────────────────────────────

def _load_dotenv_if_needed() -> None:
    """从项目根 .env 加载 POSTGRES_* 环境变量（不覆盖已有值）."""
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


def _build_dsn() -> str:
    """从环境变量拼装 psycopg DSN（密码不入代码）."""
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供（禁止硬编码）。"
        )
    return f"host={host} port={port} dbname={db} user={user} password={password}"


# ────────────────────────────────────────────────────────────────────
# 校验逻辑
# ────────────────────────────────────────────────────────────────────

def _fetch_actual_tables(cur: psycopg.Cursor) -> list[str]:
    """查询 public schema 下所有表（含分区子表）."""
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    return [r[0] for r in cur.fetchall()]


def _fetch_table_rowcount(cur: psycopg.Cursor, table: str) -> int:
    """查询表行数（使用 est 对大表也安全；此处用 COUNT 精确值）."""
    # 表名来自白名单/系统目录，无注入风险；仍用 quote_ident 兜底
    cur.execute(f'SELECT COUNT(*) FROM pg_class WHERE relname = %s AND relkind = %s',
                (table, 'p'))
    is_partitioned = cur.fetchone()[0] if cur.rowcount else False
    cur.execute(f'SELECT COUNT(*) FROM "{table}"')
    return cur.fetchone()[0]


def verify() -> int:
    """执行完整性校验，返回退出码（0=通过，1=失败）."""
    dsn = _build_dsn()
    db_name = os.environ.get("POSTGRES_DB", "muti_dev")
    print(f"== backup_verify: 校验数据库 {db_name} ==")
    print(f"   DSN: host={os.environ.get('POSTGRES_HOST','localhost')} "
          f"port={os.environ.get('POSTGRES_PORT','5432')} dbname={db_name}")
    print("")

    failures: list[str] = []

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # ── 1. 表数量校验 ──
            actual_tables = _fetch_actual_tables(cur)
            actual_set = set(actual_tables)
            print(f"[1/3] 表数量校验")
            print(f"   实际表数: {len(actual_tables)}（含分区子表与 alembic_version）")

            missing = [t for t in EXPECTED_TABLES if t not in actual_set]
            if missing:
                failures.append(f"缺少期望表: {missing}")
                print(f"   ❌ 缺少 {len(missing)} 张期望表: {missing}")
            else:
                print(f"   ✅ 全部 {len(EXPECTED_TABLES)} 张期望表均存在")

            # 分区子表（response_event_YYYYMM）
            partitions = [t for t in actual_tables if t.startswith("response_event_")]
            if partitions:
                print(f"   ℹ️  response_event 分区子表: {len(partitions)} 个")
            print("")

            # ── 2. 记录数统计 ──
            print(f"[2/3] 记录数统计")
            print(f"   {'表名':<30} {'行数':>10}")
            print(f"   {'-'*30} {'-'*10}")
            total_rows = 0
            for table in EXPECTED_TABLES:
                if table not in actual_set:
                    continue
                try:
                    cnt = _fetch_table_rowcount(cur, table)
                    total_rows += cnt
                    print(f"   {table:<30} {cnt:>10}")
                except Exception as e:
                    failures.append(f"统计 {table} 行数失败: {e}")
                    print(f"   {table:<30} ERROR: {e}")
            print(f"   {'-'*30} {'-'*10}")
            print(f"   {'合计':<30} {total_rows:>10}")
            print("")

            # ── 3. 关键表抽样 ──
            print(f"[3/3] 关键表抽样（SELECT * LIMIT 1）")
            for table in KEY_TABLES_SAMPLE:
                if table not in actual_set:
                    failures.append(f"关键表 {table} 不存在")
                    print(f"   ❌ {table}: 不存在")
                    continue
                try:
                    cur.execute(f'SELECT * FROM "{table}" LIMIT 1')
                    row = cur.fetchone()
                    cols = [d.name for d in cur.description]
                    print(f"   ✅ {table}: {len(cols)} 列，抽样 {'有数据' if row else '空表'}")
                except Exception as e:
                    failures.append(f"关键表 {table} 抽样失败: {e}")
                    print(f"   ❌ {table}: {e}")
            print("")

    # ── 汇总 ──
    if failures:
        print(f"❌ 校验失败（{len(failures)} 项）:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"✅ backup_verify 通过：{len(EXPECTED_TABLES)} 表齐全，关键表可查")
    return 0


if __name__ == "__main__":
    sys.exit(verify())
