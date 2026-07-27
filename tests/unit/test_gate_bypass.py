"""T-W2-011 门证书签发服务 + 直写 serving 区失败实证.

对照任务卡验收标准逐条覆盖：
1. `issue_certificate(...)` 仅在全部阻断项 pass 时生成合法 cert_id，并关联 GateRun。
2. `src/core/gate/certifier/serving_views.sql` 创建只读 serving 视图与角色，
   禁止 INSERT/UPDATE/DELETE。
3. `tests/unit/test_gate_bypass.py` 用低权限角色直写 serving 视图/底层表，
   断言抛出权限错误。
4. `make accept TASK=T-W2-011` 全绿；E2E-5 通过。

附加覆盖：
- 签发失败场景：阻断 fail / 阻断 review / 空 runs。
- serving 视图过滤：只暴露 published 且 retired_at 为空的版本；素材许可过期者排除。
- serving_reader 角色权限边界：只能 SELECT 视图，不能 INSERT/UPDATE/DELETE 底层表。

宪法 A5/X6：核心域零学科特判；D1：三本账只增不改；D2：门 DB 级强制。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.gate.certifier import CertificateIssuanceError, issue_certificate
from src.core.gate.validator import ValidatorResult


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixtures
# ────────────────────────────────────────────────────────────────────

# DB 隔离：每测试前 TRUNCATE 三表 + 关联表 + 插入 cert:none 占位行
# 为什么 cert:none 占位：编排器失败时 gate_run.certificate_id 用 'cert:none' 作 FK
# 目标；issue_certificate 路径在测试中可能与其他测试共享 DB 状态，TRUNCATE 清场
# 后必须重新插入占位行。
@pytest_asyncio.fixture(autouse=True)
async def _truncate_gate_tables(async_engine: AsyncEngine):
    """每测试前清空 gate 三表 + 内容版本表（item_version/material_version 等）+ 插入 cert:none 占位行.

    为什么 TRUNCATE 内容版本表：serving 视图测试需要从空表开始预插数据；其他
    测试文件（如 test_writer/test_triggers）可能留下 published 行，会让
    v_serving_* 视图返回非预期数据。本 fixture 清场后由具体测试 setup 控制。
    TRUNCATE CASCADE 自动级联到外键依赖（gate_run → gate_certificate 等）。

    W2a-integrate 修复：用独立连接真正提交 TRUNCATE，而非复用 async_session。
    - 原实现：TRUNCATE 在 async_session 的 savepoint 内执行，commit() 退化为
      RELEASE SAVEPOINT，TRUNCATE 持有的 ACCESS EXCLUSIVE 锁仍归属外层事务，
      直到测试结束外层事务回滚才释放。
    - 问题：serving_reader 角色测试用独立 engine 执行 INSERT/UPDATE/DELETE，
      这些操作要获取 ROW EXCLUSIVE 锁，与 TRUNCATE 的 ACCESS EXCLUSIVE 冲突。
      PostgreSQL 锁获取在权限检查之前，serving_reader 操作被阻塞 → asyncpg
      超时关闭连接 → 报 ConnectionDoesNotExistError 而非预期的 ProgrammingError。
    - 修复：用 async_engine 的独立连接执行 TRUNCATE + commit，锁在 fixture
      setup 完成后立即释放。测试用 async_session 在 savepoint 内操作，事务
      回滚隔离仍生效（savepoint 内写入不持久化，但 TRUNCATE 已持久化清场）。
    - 为什么不改用 DELETE：DELETE 用 ROW EXCLUSIVE 锁确实与 INSERT 兼容，但
      不会重置 IDENTITY 序列；保持 TRUNCATE RESTART IDENTITY 语义不变。
    """
    # 用独立连接执行 TRUNCATE + INSERT(cert:none) + 真正提交，释放 ACCESS EXCLUSIVE 锁
    async with async_engine.connect() as conn:
        tran = await conn.begin()
        try:
            # 顺序：先清子表（被 FK 引用），后清父表
            await conn.execute(
                text(
                    "TRUNCATE TABLE gate_verdict, gate_run, gate_certificate,"
                    " item_kp, publication, item_group,"
                    " corpus_version, corpus_asset,"
                    " material_version, material,"
                    " item_version, item,"
                    " item_template_version, item_template,"
                    " material_license"
                    " RESTART IDENTITY CASCADE"
                )
            )
            # cert:none 占位（编排器失败路径用；issue_certificate 不依赖此占位）
            await conn.execute(
                text(
                    "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
                    " policy_version, issued_by)"
                    " VALUES ('cert:none', 'placeholder-for-failed-run', 'publish',"
                    " 'no-policy', 'system')"
                )
            )
            await tran.commit()  # 真正提交，释放 ACCESS EXCLUSIVE 锁
        except Exception:
            await tran.rollback()
            raise
    yield


# ────────────────────────────────────────────────────────────────────
# ValidatorResult 工厂
# ────────────────────────────────────────────────────────────────────


def _make_result(
    vid: str,
    verdict: str,
    *,
    version: str = "test-1.0.0",
    evidence: dict[str, Any] | None = None,
    confidence: Decimal = Decimal("1.000"),
    cost_ms: int = 5,
    cost_tokens: int = 0,
) -> ValidatorResult:
    """构造 ValidatorResult（不依赖验证器实例）."""
    return ValidatorResult(
        verdict=verdict,
        evidence=evidence or {"vid": vid, "verdict": verdict},
        confidence=confidence,
        validator_id=vid,
        version=version,
        cost_ms=cost_ms,
        cost_tokens=cost_tokens,
    )


# ════════════════════════════════════════════════════════════════════
# §1 issue_certificate：全部阻断 pass → 签发证书 + 关联 GateRun
# ════════════════════════════════════════════════════════════════════


async def test_issue_certificate_pass_signs_and_persists(
    async_session: AsyncSession,
):
    """验收 #1：全部阻断 pass → 生成 cert_id + 三表 INSERT.

    - cert_id 形如 'cert_<ULID>'，非空。
    - gate_certificate 新增 1 行（cert:none + 新签发的 = 2 行）。
    - gate_run 新增 N 行（每个 validator 一行），certificate_id 都指向新 cert_id。
    - gate_verdict 新增 N 行（每 run 一行）。
    """
    runs = [
        (_make_result("v1", "pass"), True),    # 阻断 pass
        (_make_result("v2", "pass"), True),    # 阻断 pass
        (_make_result("v3", "pass"), False),   # 非阻断 pass
    ]

    cert_id = await issue_certificate(
        artifact_ref="sha256:item-v1",
        cert_type="publish",
        policy_version="test-policy-v1",
        issued_by="alice",
        runs=runs,
        db=async_session,
    )

    # cert_id 格式
    assert cert_id is not None
    assert cert_id.startswith("cert_")
    assert len(cert_id) > len("cert_")  # ULID 后缀非空

    # gate_certificate 行数：cert:none + 新签发 = 2
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 2

    # 新签发的证书行可回读
    new_cert = (
        await async_session.execute(
            text(
                "SELECT artifact_ref, cert_type, policy_version, issued_by"
                " FROM gate_certificate WHERE cert_id = :cid"
            ),
            {"cid": cert_id},
        )
    ).one()
    assert new_cert.artifact_ref == "sha256:item-v1"
    assert new_cert.cert_type == "publish"
    assert new_cert.policy_version == "test-policy-v1"
    assert new_cert.issued_by == "alice"

    # gate_run 行数：3 条（每 run 一行），均关联到新 cert_id
    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run WHERE certificate_id = :cid"),
        {"cid": cert_id},
    )
    assert run_count == 3

    # gate_verdict 行数：3 条
    verdict_count = await async_session.scalar(
        text(
            "SELECT count(*) FROM gate_verdict v"
            " JOIN gate_run r ON v.run_id = r.run_id"
            " WHERE r.certificate_id = :cid"
        ),
        {"cid": cert_id},
    )
    assert verdict_count == 3

    # 关联的 run 字段一致
    runs_in_db = (
        await async_session.execute(
            text(
                "SELECT validator_id, verdict, confidence"
                " FROM gate_run WHERE certificate_id = :cid"
                " ORDER BY validator_id"
            ),
            {"cid": cert_id},
        )
    ).all()
    assert {r.validator_id for r in runs_in_db} == {"v1", "v2", "v3"}
    for r in runs_in_db:
        assert r.verdict == "pass"
        assert Decimal(str(r.confidence)) == Decimal("1.000")


# ════════════════════════════════════════════════════════════════════
# §2 issue_certificate：阻断 fail → 拒绝签发
# ════════════════════════════════════════════════════════════════════


async def test_issue_certificate_fails_when_blocking_fails(
    async_session: AsyncSession,
):
    """验收 #1 失败路径：阻断 fail → 抛 CertificateIssuanceError，不签发证书."""
    runs = [
        (_make_result("v1", "pass"), True),
        (_make_result("v2_fail", "fail"), True),   # 阻断 fail
        (_make_result("v3", "pass"), False),
    ]

    with pytest.raises(CertificateIssuanceError, match="v2_fail"):
        await issue_certificate(
            artifact_ref="sha256:item-fail",
            cert_type="publish",
            policy_version="test-policy-v1",
            issued_by="alice",
            runs=runs,
            db=async_session,
        )

    # 没有新证书被签发（仅 cert:none 占位行）
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 1  # 只有 cert:none

    # 没有 gate_run 被落库
    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run")
    )
    assert run_count == 0


async def test_issue_certificate_fails_when_blocking_review(
    async_session: AsyncSession,
):
    """阻断 review → 拒绝签发证书（review 需人工裁决，未通过门强制）."""
    runs = [
        (_make_result("v1", "pass"), True),
        (_make_result("v2_review", "review"), True),   # 阻断 review
    ]

    with pytest.raises(CertificateIssuanceError, match="v2_review"):
        await issue_certificate(
            artifact_ref="sha256:item-review",
            cert_type="publish",
            policy_version="test-policy-v1",
            issued_by="alice",
            runs=runs,
            db=async_session,
        )

    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 1  # 仅 cert:none


async def test_issue_certificate_fails_when_no_runs(
    async_session: AsyncSession,
):
    """空 runs → 抛 CertificateIssuanceError（必须有验证器运行记录）."""
    with pytest.raises(CertificateIssuanceError, match="runs 为空"):
        await issue_certificate(
            artifact_ref="sha256:item-empty",
            cert_type="publish",
            policy_version="test-policy-v1",
            issued_by="alice",
            runs=[],
            db=async_session,
        )


async def test_issue_certificate_non_blocking_fail_passes(
    async_session: AsyncSession,
):
    """非阻断 fail 不影响签发：阻断项全部 pass 即可（fail 留痕但不阻断）."""
    runs = [
        (_make_result("v1", "pass"), True),       # 阻断 pass
        (_make_result("v2_advisory", "fail"), False),  # 非阻断 fail（建议性）
    ]

    cert_id = await issue_certificate(
        artifact_ref="sha256:item-advisory",
        cert_type="publish",
        policy_version="test-policy-v1",
        issued_by="alice",
        runs=runs,
        db=async_session,
    )

    assert cert_id is not None
    # gate_run 中 v2_advisory 的 verdict='fail'，但已落库（留痕）
    v2_verdict = await async_session.scalar(
        text(
            "SELECT verdict FROM gate_run"
            " WHERE certificate_id = :cid AND validator_id = 'v2_advisory'"
        ),
        {"cid": cert_id},
    )
    assert v2_verdict == "fail"


async def test_issue_certificate_retire_type(
    async_session: AsyncSession,
):
    """cert_type='retire' 时签发的证书类型为 retire（退役签发与发布签发共用入口）."""
    runs = [(_make_result("v1", "pass"), True)]

    cert_id = await issue_certificate(
        artifact_ref="sha256:item-retire",
        cert_type="retire",
        policy_version="test-policy-v1",
        issued_by="bob",
        runs=runs,
        db=async_session,
    )

    cert_type = await async_session.scalar(
        text("SELECT cert_type FROM gate_certificate WHERE cert_id = :cid"),
        {"cid": cert_id},
    )
    assert cert_type == "retire"


# ════════════════════════════════════════════════════════════════════
# §3 serving_reader 角色直写底层表失败实证（验收 #2/#3）
# ════════════════════════════════════════════════════════════════════


def _serving_reader_dsn() -> str:
    """serving_reader 角色的 DSN（与 conftest 同 host/port/db，仅 user/pwd 不同）.

    为什么单独 DSN：用 serving_reader 独立连接数据库，实证「绕过写入服务直写 serving
    表在 DB 层失败」——这是 D2 物理强制的角色层兜底（验收 #2/#3）。
    """
    user = "serving_reader"
    pwd = "serving_reader_pwd"
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"


@pytest_asyncio.fixture
async def serving_reader_engine() -> AsyncEngine:
    """serving_reader 角色的独立 AsyncEngine.

    为什么不复用 async_engine fixture：async_engine 用 muti 用户（特权）；
    serving_reader 是低权限角色，必须独立连接才能真实反映「绕过写入服务」的视图。
    """
    engine = create_async_engine(_serving_reader_dsn(), echo=False, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_serving_reader_cannot_insert_item_version(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3：serving_reader 直写 item_version 底层表 → permission denied."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text(
                    "INSERT INTO item_version"
                    " (item_version_id, item_id, status, objective,"
                    " interaction_ref, content, scoring_ref, error_bindings,"
                    " lineage, rendered_snapshot)"
                    " VALUES ('sha256:bypass', 'item_x', 'draft',"
                    " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,"
                    " '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb)"
                )
            )
            await session.commit()


async def test_serving_reader_cannot_update_item_version(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3：serving_reader 直 UPDATE item_version → permission denied."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text("UPDATE item_version SET status = 'retired' WHERE 1=0")
            )
            await session.commit()


async def test_serving_reader_cannot_delete_item_version(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3：serving_reader 直 DELETE item_version → permission denied."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text("DELETE FROM item_version WHERE 1=0")
            )
            await session.commit()


async def test_serving_reader_cannot_insert_material_version(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3 边界：serving_reader 直写 material_version → permission denied."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text(
                    "INSERT INTO material_version"
                    " (material_version_id, material_id, content_ref,"
                    " license_id, status, lineage)"
                    " VALUES ('sha256:bypass-m', 'mat_x', 'ref',"
                    " 'lic_x', 'draft', '{}'::jsonb)"
                )
            )
            await session.commit()


async def test_serving_reader_cannot_insert_corpus_version(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3 边界：serving_reader 直写 corpus_version → permission denied."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text(
                    "INSERT INTO corpus_version"
                    " (version_id, asset_id, content_ref, license_id,"
                    " status, lineage)"
                    " VALUES ('sha256:bypass-c', 'asset_x', 'ref',"
                    " 'lic_x', 'draft', '{}'::jsonb)"
                )
            )
            await session.commit()


async def test_serving_reader_cannot_insert_gate_certificate(
    serving_reader_engine: AsyncEngine,
):
    """验收 #3 边界：serving_reader 直写 gate_certificate（伪造证书）→ permission denied.

    关键防护：serving_reader 不仅不能写 serving 表，也不能伪造门证书
    （伪造证书 + 直写 item_version 是绕过门强制的最危险路径）。
    """
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ProgrammingError, match="(?i)permission denied"):
            await session.execute(
                text(
                    "INSERT INTO gate_certificate"
                    " (cert_id, artifact_ref, cert_type, policy_version, issued_by)"
                    " VALUES ('cert_fake', 'sha256:fake', 'publish', 'p', 'mallory')"
                )
            )
            await session.commit()


# ════════════════════════════════════════════════════════════════════
# §4 serving_reader 角色可 SELECT 视图（正常路径）
# ════════════════════════════════════════════════════════════════════


async def test_serving_reader_can_select_view(
    serving_reader_engine: AsyncEngine,
):
    """验收 #2 边界：serving_reader 可 SELECT v_serving_item_version（GRANT 已生效）."""
    factory = async_sessionmaker(serving_reader_engine, expire_on_commit=False)
    async with factory() as session:
        # 简单 SELECT，无数据时返回空集（不应报权限错误）
        result = await session.execute(
            text("SELECT count(*) FROM v_serving_item_version")
        )
        count = result.scalar()
        # 视图可读，count 为 int（空表时为 0）
        assert isinstance(count, int)
        assert count >= 0


# ════════════════════════════════════════════════════════════════════
# §5 serving 视图过滤：只暴露 published + 未退役 + 许可未过期
# ════════════════════════════════════════════════════════════════════
# 这些测试用特权 session 预插数据，再用 serving_reader 验证视图过滤行为。
# 为什么不用 async_session 直接 SELECT 视图：特权用户可读所有表，但视图过滤逻辑
# 由视图 WHERE 子句承载，特权用户 SELECT 视图同样受过滤——直接用 async_session
# 即可验证视图 WHERE 正确性。serving_reader 仅用于 §3 权限边界测试。


def _item_version_kwargs(
    *,
    item_version_id: str,
    item_id: str,
    status: str,
    with_publish_fields: bool = True,
    retired_at: datetime | None = None,
) -> dict:
    """构造 item_version INSERT 用 kwargs（六大块用空 jsonb 占位）."""
    kw: dict[str, Any] = {
        "item_version_id": item_version_id,
        "item_id": item_id,
        "status": status,
        "objective": "{}",
        "interaction_ref": "{}",
        "content": "{}",
        "scoring_ref": "{}",
        "error_bindings": "[]",
        "lineage": "{}",
        "rendered_snapshot": "{}",
    }
    if with_publish_fields and status in ("published", "retired"):
        kw["gate_certificate_id"] = "cert:test-fixture"
        kw["published_at"] = datetime.now(timezone.utc)
    if retired_at is not None:
        kw["retired_at"] = retired_at
    return kw


async def _insert_item(async_session: AsyncSession, item_id: str) -> None:
    """插入 item 行（status='published' 触发器需要 item 先存在）."""
    await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, :pid, :t)"
        ),
        {"iid": item_id, "pid": "platform", "t": "C"},
    )


async def test_serving_item_view_filters_only_published(
    async_session: AsyncSession,
):
    """serving 视图只暴露 status='published' 的版本：draft/quarantined 不出现."""
    # 1 个 published + 1 个 draft + 1 个 quarantined
    await _insert_item(async_session, "item_a")
    await _insert_item(async_session, "item_b")
    await _insert_item(async_session, "item_c")

    for iid, status in [
        ("sha256:iv_pub", "published"),
        ("sha256:iv_draft", "draft"),
        ("sha256:iv_quar", "quarantined"),
    ]:
        kwargs = _item_version_kwargs(
            item_version_id=iid,
            item_id={"sha256:iv_pub": "item_a",
                     "sha256:iv_draft": "item_b",
                     "sha256:iv_quar": "item_c"}[iid],
            status=status,
            with_publish_fields=(status == "published"),
        )
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(f":{k}" for k in kwargs.keys())
        await async_session.execute(
            text(f"INSERT INTO item_version ({cols}) VALUES ({placeholders})"),
            kwargs,
        )
    await async_session.commit()

    rows = (
        await async_session.execute(
            text("SELECT item_version_id FROM v_serving_item_version")
        )
    ).all()
    visible = {r[0] for r in rows}
    assert visible == {"sha256:iv_pub"}


async def test_serving_item_view_excludes_retired_status(
    async_session: AsyncSession,
):
    """status='retired' 不出现在 serving 视图（即便有 published_at）."""
    await _insert_item(async_session, "item_r1")
    await _insert_item(async_session, "item_r2")

    # published 一行
    pub_kwargs = _item_version_kwargs(
        item_version_id="sha256:iv_pub2",
        item_id="item_r1",
        status="published",
    )
    cols = ", ".join(pub_kwargs.keys())
    placeholders = ", ".join(f":{k}" for k in pub_kwargs.keys())
    await async_session.execute(
        text(f"INSERT INTO item_version ({cols}) VALUES ({placeholders})"),
        pub_kwargs,
    )

    # retired 一行（retired_at 设置；status='retired'）
    ret_kwargs = _item_version_kwargs(
        item_version_id="sha256:iv_retired",
        item_id="item_r2",
        status="retired",
        retired_at=datetime.now(timezone.utc),
    )
    cols = ", ".join(ret_kwargs.keys())
    placeholders = ", ".join(f":{k}" for k in ret_kwargs.keys())
    await async_session.execute(
        text(f"INSERT INTO item_version ({cols}) VALUES ({placeholders})"),
        ret_kwargs,
    )
    await async_session.commit()

    rows = (
        await async_session.execute(
            text("SELECT item_version_id FROM v_serving_item_version")
        )
    ).all()
    visible = {r[0] for r in rows}
    assert "sha256:iv_pub2" in visible
    assert "sha256:iv_retired" not in visible


async def test_serving_material_view_filters_expired_license(
    async_session: AsyncSession,
):
    """serving 视图过滤素材许可已过期（expires_at < now 或 decision='expired'）."""
    # 准备三个 license：未过期 / 已过期 / decision=expired
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, decision)"
            " VALUES ('lic_active', 'approved'),"
            " ('lic_expired', 'approved'),"
            " ('lic_rejected', 'expired')"
        )
    )
    # 已过期的 license 设置 expires_at
    await async_session.execute(
        text(
            "UPDATE material_license SET expires_at = :past WHERE license_id = 'lic_expired'"
        ),
        {"past": datetime.now(timezone.utc) - timedelta(days=1)},
    )
    # 还有一个未来过期的 license（边界）
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, decision, expires_at)"
            " VALUES ('lic_future', 'approved', :future)"
        ),
        {"future": datetime.now(timezone.utc) + timedelta(days=1)},
    )

    # 三个 material + 三个 material_version（status='published'）
    for mid, lid in [
        ("mat_active", "lic_active"),
        ("mat_expired", "lic_expired"),
        ("mat_future", "lic_future"),
        ("mat_rejected", "lic_rejected"),
    ]:
        await async_session.execute(
            text(
                "INSERT INTO material (material_id, kind, pack_id)"
                " VALUES (:mid, 'passage', 'platform')"
            ),
            {"mid": mid},
        )
        await async_session.execute(
            text(
                "INSERT INTO material_version"
                " (material_version_id, material_id, content_ref,"
                " license_id, status, lineage, gate_certificate_id, published_at)"
                " VALUES (:mvid, :mid, 'ref', :lid, 'published',"
                " '{}'::jsonb, 'cert:test-fixture', now())"
            ),
            {"mvid": f"sha256:mv_{mid}", "mid": mid, "lid": lid},
        )
    await async_session.commit()

    rows = (
        await async_session.execute(
            text(
                "SELECT material_version_id, license_decision"
                " FROM v_serving_material_version"
            )
        )
    ).all()
    visible = {r[0] for r in rows}
    # active + future 通过；expired + rejected 排除
    assert "sha256:mv_mat_active" in visible
    assert "sha256:mv_mat_future" in visible
    assert "sha256:mv_mat_expired" not in visible
    assert "sha256:mv_mat_rejected" not in visible


# ════════════════════════════════════════════════════════════════════
# §6 serving_views.sql 契约一致性（防迁移 0006 与 SQL 文本漂移）
# ════════════════════════════════════════════════════════════════════


def test_serving_views_sql_is_frozen_contract():
    """serving_views.sql 是契约冻结文本：视图与角色必须存在.

    防护：迁移 0006 漂移或有人手改 serving_views.sql 导致 DB 与契约不一致。
    本测试只检查关键字符串存在（视图名 + 角色名），不全文比对（允许注释修订）。
    """
    sql_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "core" / "gate" / "certifier" / "serving_views.sql"
    )
    assert sql_path.is_file(), "serving_views.sql 必须存在"
    content = sql_path.read_text(encoding="utf-8")
    # 关键契约字符串
    assert "CREATE ROLE serving_reader" in content
    assert "CREATE OR REPLACE VIEW v_serving_item_version" in content
    assert "CREATE OR REPLACE VIEW v_serving_material_version" in content
    assert "CREATE OR REPLACE VIEW v_serving_corpus_version" in content
    assert "GRANT SELECT ON v_serving_item_version TO serving_reader" in content
    assert "GRANT SELECT ON v_serving_material_version TO serving_reader" in content
    assert "GRANT SELECT ON v_serving_corpus_version TO serving_reader" in content
    # 关键过滤条件（published + retired_at IS NULL）
    assert "iv.status = 'published'" in content
    assert "iv.retired_at IS NULL" in content


# ════════════════════════════════════════════════════════════════════
# §7 核心域不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


def test_no_subject_pack_imports_in_certifier():
    """宪法 A5/X6：src/core/gate/certifier/ 不 import 任何学科包/学段包."""
    cert_dir = os.path.join("src", "core", "gate", "certifier")
    pattern = __import__("re").compile(
        r"^\s*(?:from\s+(?:packs|subject_|gradeband)|import\s+(?:packs|subject_|gradeband))",
        __import__("re").MULTILINE,
    )
    violations: list[tuple[str, list[str]]] = []
    for fname in os.listdir(cert_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(cert_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        matches = pattern.findall(content)
        if matches:
            violations.append((fname, matches))
    assert not violations, f"src/core/gate/certifier/ 存在学科包 import：{violations}"
