"""T-W2-011 serving 区只读视图 + serving_reader 角色.

按 specs/contracts/db/item-model.md#4 §4 规则 3 落地 authoring/serving 逻辑分区：
- 创建 v_serving_item_version / v_serving_material_version / v_serving_corpus_version
  三个 serving 视图，过滤 status='published' AND retired_at IS NULL（素材/语料
  额外过滤许可未过期）。
- 创建 serving_reader LOGIN 角色，仅授予三个视图的 SELECT 权限——
  无底层表 INSERT/UPDATE/DELETE 权限。
- 绕过写入服务直写 serving 表 → serving_reader 无权限 → DB 层失败
  （D2 物理强制的角色层兜底，与 CHECK 约束、append-only 触发器三层共同防护）。

SQL 源头：src/core/gate/certifier/serving_views.sql（契约冻结文本，人类逐行
审查批准）。本迁移把该文件按分号切分逐条执行（psycopg3 不支持单次 execute
多语句，必须拆分）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
downgrade 删除视图与角色。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# serving_views.sql 路径（契约冻结文本）
_SERVING_VIEWS_SQL_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "core" / "gate" / "certifier" / "serving_views.sql"
)


# ────────────────────────────────────────────────────────────────────
# SQL 切分（按分号，但跳过 $$...$$ 块内的分号）
# ────────────────────────────────────────────────────────────────────
# 为什么手写切分而非用 psycopg3 multi-statement：psycopg3 默认 ClientCursor
# 不支持 multi-statement execute；Alembic 的 op.execute(sa.text(...)) 也只跑首条。
# 必须把脚本拆为单语句列表逐条执行。
# 为什么正则而非 ast：PL/pgSQL $$...$$ 块内的分号不能切——正则识别
# (?:BEGIN|DECLARE|$$) 起止的 dollar-quoted 块即可，无需完整 PL/pgSQL 解析器。


def _split_sql_statements(sql_text: str) -> list[str]:
    """把多语句 SQL 切分为单语句列表，跳过 $$...$$ 与 '...' / "..." 内的分号.

    Returns:
        单语句列表（去注释、去空白后非空者）。
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql_text)
    # 状态机：默认 / 单引号字符串 / 双引号标识符 / dollar-quoted 块
    state = "default"
    dollar_tag = ""  # 当前 $$...$$ 的标签（如 $$ 或 $func$）
    while i < n:
        ch = sql_text[i]
        if state == "default":
            # 进入单引号字符串（PG 字符串字面量）
            if ch == "'":
                buf.append(ch)
                state = "single_quote"
                i += 1
                continue
            # 进入双引号标识符
            if ch == '"':
                buf.append(ch)
                state = "double_quote"
                i += 1
                continue
            # 进入 dollar-quoted 块：$tag$...$tag$
            if ch == "$":
                # 尝试匹配 $tag$ 形式（tag 为空或标识符）
                m = re.match(r"\$(\w*)\$", sql_text[i:])
                if m:
                    tag = m.group(1)
                    full_tag = f"${tag}$"
                    buf.append(full_tag)
                    i += len(full_tag)
                    state = "dollar_quote"
                    dollar_tag = full_tag
                    continue
                # 单独的 $（非 dollar-quote 起始）：当作普通字符
                buf.append(ch)
                i += 1
                continue
            # 行注释 --
            if ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
                # 跳到行尾
                while i < n and sql_text[i] != "\n":
                    i += 1
                continue
            # 块注释 /* */
            if ch == "/" and i + 1 < n and sql_text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            # 语句结束
            if ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if state == "single_quote":
            # 单引号字符串：'' 转义为字面单引号
            if ch == "'":
                if i + 1 < n and sql_text[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                buf.append(ch)
                state = "default"
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if state == "double_quote":
            if ch == '"':
                buf.append(ch)
                state = "default"
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if state == "dollar_quote":
            # 检查是否匹配到结束 tag
            if sql_text[i:].startswith(dollar_tag):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                state = "default"
                dollar_tag = ""
                continue
            buf.append(ch)
            i += 1
            continue
    # 末尾的 buffer（无分号结尾的语句）
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements


# ────────────────────────────────────────────────────────────────────
# downgrade 用的 DROP 语句（与 serving_views.sql 中的 CREATE 对称）
# ────────────────────────────────────────────────────────────────────
_DROP_VIEWS_SQL = [
    "DROP VIEW IF EXISTS v_serving_corpus_version",
    "DROP VIEW IF EXISTS v_serving_material_version",
    "DROP VIEW IF EXISTS v_serving_item_version",
    # 不 DROP ROLE serving_reader：role 可能被 GRANT 给其他对象；
    # 仅 REVOKE 视图权限即可。downgrade 不删角色避免级联错误。
    # 若需彻底清理角色，可手工执行 DROP ROLE IF EXISTS serving_reader。
]


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """应用 serving_views.sql：创建角色 + 视图 + GRANT."""
    sql_text = _SERVING_VIEWS_SQL_PATH.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql_text)
    binding = op.get_bind()
    for stmt in statements:
        binding.execute(sa.text(stmt))


def downgrade() -> None:
    """回滚：删三个 serving 视图（角色保留，避免 DROP ROLE 级联）."""
    binding = op.get_bind()
    for stmt in _DROP_VIEWS_SQL:
        binding.execute(sa.text(stmt))
    # REVOKE serving_reader 上的视图权限（视图已 DROP，权限自动失效，显式 REVOKE 兜底）
    binding.execute(sa.text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM serving_reader"))
