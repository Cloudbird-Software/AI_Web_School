#!/usr/bin/env python3
"""W0 出口最小链路演示（OPC §6.1）：创建一条题目记录 → 过门（占位验证器）→ 查询。

基于 T-W0-005 的 0001 占位 item 表（id BIGINT IDENTITY PK + created_at），
不依赖 src/ 业务代码。占位验证器规则：记录存在且 id 为正整数 → PASS。

用法（需 db 容器运行、.env 含 POSTGRES_*）：
    python scripts/demo-w0-min-link.py
退出码 0 = 链路全通；非 0 = 失败。
"""
import os
import sys

import psycopg


def load_env(path=".env"):
    """最小 .env 读取（避免引入 python-dotenv 新依赖，X8）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def main() -> int:
    load_env()
    dsn = (
        f"host=localhost port=5432 dbname={os.environ.get('POSTGRES_DB', 'muti_dev')} "
        f"user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # ① 创建一条题目记录（W1 0002 后的 item 表：item_id/pack_id/tier 必填）
        import ulid
        item_id = str(ulid.new())
        cur.execute(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (%s, 'platform', 'C') RETURNING item_id;",
            (item_id,),
        )
        item_id = cur.fetchone()[0]
        print(f"1. 创建题目记录: item.item_id = {item_id}")

        # ② 过门（占位验证器：记录存在且 id 非空）
        cur.execute("SELECT item_id FROM item WHERE item_id = %s;", (item_id,))
        row = cur.fetchone()
        assert row and row[0], "占位验证器 FAIL：记录不存在或 id 非法"
        print(f"2. 过门（占位验证器）: PASS（gate=placeholder-validator）")

        # ③ 查询（链路回读）
        cur.execute("SELECT item_id, created_at FROM item WHERE item_id = %s;", (item_id,))
        got = cur.fetchone()
        assert got and got[0] == item_id, "查询回读 FAIL"
        print(f"3. 查询回读: item_id={got[0]}, created_at={got[1]}")

    print("MIN-LINK PASS：创建 → 过门 → 查询 链路全通")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 —— 演示脚本：任何失败即链路不通
        print(f"MIN-LINK FAIL: {e}", file=sys.stderr)
        sys.exit(1)
