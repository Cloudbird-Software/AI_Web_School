"""T-W2-019 测试隔离显式验证.

对照任务卡三条验收标准：
  1. async_session fixture 在测试结束后回滚事务
  2. 测试 A 写入数据，测试 B 在同一进程内看不到
  3. make test 在已存在数据的数据库上仍可全绿

为什么单独成文件：原 W1 conftest 注释曾说明「测试规模小，无共享状态依赖」，
故未引入事务回滚隔离；T-W2-019 引入隔离后必须有显式测试证明隔离生效，
避免「悄悄退化回 commit 模式」的反模式（X1）回归。

测试设计原则：
- 写入用稳定可识别的 ID（非 uuid4），便于跨测试断言存在性；
- 跨测试隔离用 pytest 测试函数顺序固定（同模块内按定义顺序执行）保证
  test_b 紧跟 test_a，避免依赖测试执行顺序的隐性假设。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# 写入测试用稳定 ID：方便跨测试断言「不存在」
_PROBE_ID_A = "iso-probe-test-a"
_PROBE_ID_B = "iso-probe-test-b"
_PROBE_ID_DIRTY = "iso-probe-dirty-outside-tx"


# ────────────────────────────────────────────────────────────────────
# 验收 #1：测试结束后事务回滚（写入在下一测试不可见）
# ────────────────────────────────────────────────────────────────────

async def test_a_writes_item(async_session: AsyncSession) -> None:
    """测试 A：写入一个 item，事务内可见。

    本测试只证明「写入确实发生了」；隔离效果由 test_b_does_not_see_a_writes 验证。
    """
    result = await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) "
            "VALUES (:id, 'subject-test', 'C') RETURNING item_id"
        ),
        {"id": _PROBE_ID_A},
    )
    row = result.fetchone()
    assert row is not None, "RETURNING 应返回写入的行"
    assert row[0] == _PROBE_ID_A

    # 同事务内可读到（SAVEPOINT 视图）
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": _PROBE_ID_A},
    )
    assert result.fetchone() is not None, "同事务内应可读到刚写入的行"


async def test_b_does_not_see_a_writes(async_session: AsyncSession) -> None:
    """测试 B：上一个测试的写入已回滚，本测试不应见到 _PROBE_ID_A。

    本测试与 test_a_writes_item 在同一模块内顺序执行；若 async_session fixture
    未做事务回滚，_PROBE_ID_A 会持久化到 DB，本断言会失败——这就是隔离生效的证明。
    """
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": _PROBE_ID_A},
    )
    assert result.fetchone() is None, (
        f"上一个测试写入的 {_PROBE_ID_A} 应已回滚，但仍被读到——隔离失败"
    )


# ────────────────────────────────────────────────────────────────────
# 验收 #2：commit() 也无法持久化（SAVEPOINT 释放 ≠ 真实提交）
# ────────────────────────────────────────────────────────────────────

async def test_commit_does_not_persist(async_session: AsyncSession) -> None:
    """session.commit() 在 SAVEPOINT 模式下退化为 RELEASE SAVEPOINT，不持久化。

    本测试显式调用 commit()，期望写入仅在外层事务内可见，不持久化到 DB。
    """
    await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) "
            "VALUES (:id, 'subject-test', 'C')"
        ),
        {"id": _PROBE_ID_B},
    )
    await async_session.commit()

    # 同事务内仍可读
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": _PROBE_ID_B},
    )
    assert result.fetchone() is not None, "commit 后同事务内应仍可读"


async def test_data_after_commit_does_not_persist(async_session: AsyncSession) -> None:
    """上一个测试 commit 的数据在下一个测试不可见——证明 commit 不持久化。"""
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": _PROBE_ID_B},
    )
    assert result.fetchone() is None, (
        f"上一个测试 commit 的 {_PROBE_ID_B} 应已随外层事务回滚，但仍被读到"
    )


# ────────────────────────────────────────────────────────────────────
# 验收 #3：已存在数据的 DB 上仍可全绿（脏数据不影响测试）
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
async def dirty_db_row(async_engine):
    """在 async_session 的事务之外注入一行脏数据，验证测试不受其影响。

    为什么用 committed_session：committed_session 走独立连接 + 真实 commit，
    数据持久化到 DB（在 async_session 的外层事务之外）。
    async_session 的外层事务是 READ COMMITTED 隔离级别，能读到 committed_session
    提交的脏数据——这正是「在已存在数据的 DB 上跑测试」的真实场景模拟。

    teardown 显式 DELETE 脏数据，避免污染后续测试。
    """
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO item (item_id, pack_id, tier) "
                "VALUES (:id, 'subject-test', 'C')"
            ),
            {"id": _PROBE_ID_DIRTY},
        )
        await s.commit()
    yield _PROBE_ID_DIRTY
    # teardown：清理脏数据
    async with factory() as s:
        await s.execute(
            text("DELETE FROM item WHERE item_id = :id"),
            {"id": _PROBE_ID_DIRTY},
        )
        await s.commit()


async def test_existing_data_visible_during_test(
    async_session: AsyncSession, dirty_db_row: str
) -> None:
    """脏数据在测试期间可见（READ COMMITTED），但不影响测试逻辑。"""
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": dirty_db_row},
    )
    assert result.fetchone() is not None, "脏数据应可见（READ COMMITTED）"


async def test_test_writes_still_isolated_with_dirty_db(
    async_session: AsyncSession, dirty_db_row: str
) -> None:
    """在脏数据存在的 DB 上，本测试的写入仍走 SAVEPOINT 隔离。

    本测试写入一个新 ID，然后断言：
    - 脏数据 + 新写入同时在当前事务可见
    - 新写入不会持久化（由下一个测试验证）
    """
    new_id = "iso-probe-with-dirty-db"
    await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) "
            "VALUES (:id, 'subject-test', 'C')"
        ),
        {"id": new_id},
    )

    # 脏数据 + 新写入均可见
    result = await async_session.execute(
        text(
            "SELECT item_id FROM item "
            "WHERE item_id IN (:dirty, :new) ORDER BY item_id"
        ),
        {"dirty": dirty_db_row, "new": new_id},
    )
    rows = [r[0] for r in result.fetchall()]
    assert dirty_db_row in rows, "脏数据应仍可见"
    assert new_id in rows, "新写入应在本事务可见"


async def test_dirty_db_test_writes_did_not_persist(async_session: AsyncSession) -> None:
    """上一个测试的新写入已回滚（脏数据由 dirty_db_row fixture teardown 清理）。"""
    result = await async_session.execute(
        text("SELECT item_id FROM item WHERE item_id = :id"),
        {"id": "iso-probe-with-dirty-db"},
    )
    assert result.fetchone() is None, "上一个测试的新写入应已回滚"


# ────────────────────────────────────────────────────────────────────
# 边界场景：CHECK 约束在 SAVEPOINT 内立即生效（与原 W1 行为兼容）
# ────────────────────────────────────────────────────────────────────

async def test_check_constraint_fires_within_savepoint(async_session: AsyncSession) -> None:
    """CHECK 约束在 SAVEPOINT 内立即抛错，无需 commit 即可命中。

    这是 W1 注释中担心的「需要真实提交才能命中 PG 端强制逻辑」的反例证明：
    PostgreSQL 的 CHECK 约束在 INSERT 语句执行时即校验，与事务隔离模式无关。
    """
    # item_group 的 ck_ig_max_six_items 限制 ≤6 题，7 题应被拒绝
    with pytest.raises(Exception) as exc_info:
        await async_session.execute(
            text(
                "INSERT INTO item_group (item_group_id, item_version_ids, created_at) "
                "VALUES (:gid, :ids, now())"
            ),
            {"gid": "iso-ig-overflow", "ids": [f"iv-{i}" for i in range(7)]},
        )
    err_msg = str(exc_info.value).lower()
    assert "ck_ig_max_six_items" in err_msg or (
        "check" in err_msg and "violation" in err_msg
    ), f"应被 CHECK 拒绝，实际：{exc_info.value}"


# ────────────────────────────────────────────────────────────────────
# 边界场景：session.rollback() 在 SAVEPOINT 模式下退化为 ROLLBACK TO SAVEPOINT
# ────────────────────────────────────────────────────────────────────

async def test_rollback_within_savepoint_does_not_break_session(
    async_session: AsyncSession,
) -> None:
    """session.rollback() 后会话仍可用（SAVEPOINT 模式下退化为 ROLLBACK TO SAVEPOINT）。

    为什么需要这个测试：W1 多个测试在 pytest.raises 内执行失败 SQL 后调用
    session.rollback()——必须证明新 fixture 下这些测试的 rollback 模式仍兼容。
    """
    # 失败的 INSERT：tier 是 enum(A/B/C/D)，'INVALID_TIER' 触发 enum 违反
    with pytest.raises(Exception):
        await async_session.execute(
            text(
                "INSERT INTO item (item_id, pack_id, tier) "
                "VALUES ('iso-rollback-probe', 'subject-test', 'INVALID_TIER')"
            ),
        )
        # 此处不会执行——上一行抛错
        await async_session.commit()

    # rollback（SAVEPOINT 模式下：ROLLBACK TO SAVEPOINT）
    await async_session.rollback()

    # 会话仍可用：执行一个查询
    result = await async_session.execute(text("SELECT 1"))
    assert result.scalar_one() == 1, "rollback 后会话应仍可用"
