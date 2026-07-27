"""T-W2-043 签发闭环单元测试.

覆盖任务卡验收标准 §1-§4：
1. 签发页展示待审 item_version 与门状态摘要.
2. 点击签发调用门编排；全部通过后生成 GateCertificate 并更新 item_version.status=published.
3. 失败时门状态停留在 quarantined/draft，展示 fail 证据.
4. 单元测试覆盖签发成功与签发失败.

宪法 D1：内容六大块不改；状态机字段前移（draft→published）合法.
宪法 A5/X6：测试不 import 学科包；用 mock 验证器隔离真实 license/schema 依赖.
宪法 D2：published_at 非空必伴随 gate_certificate_id（DB CHECK 强制）.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.core.content.publication import (
    IssueError,
    get_publication_by_version,
    issue_item_version,
)
from src.core.content.writer import publish_item_version
from src.core.gate.orchestrator.orchestrator import run_gate
from src.core.gate.policy.loader import (
    ChainEntry,
    GatePolicy,
    ValidatorStep,
    _ensure_generic_validator_stubs,
    load_default_policy,
)
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    list_validators,
    register_validator,
    reset_registry,
)
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)
from src.core.models.item import Item
from src.core.models.item_version import ItemVersion
from src.workbench.auth import SESSION_COOKIE_NAME, get_workbench_token
from src.workbench.main import create_app


# ────────────────────────────────────────────────────────────────────
# 测试数据：合法 item_version（C 级 / draft）
# ────────────────────────────────────────────────────────────────────


def _valid_version_data(pack_id: str = "subject-math") -> dict:
    """构造一份合法的 draft item_version 数据."""
    return {
        "pack_id": pack_id,
        "tier": "C",
        "status": "draft",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": "M",
            "graph_release": "graph-v1",
        },
        "interaction_ref": {
            "interaction_id": "single_choice",
            "interaction_params": {"option_count": 4},
        },
        "content": {
            "blocks": [
                {"kind": "stem", "template": "比较 {a} 与 {b}", "rendered": "比较 0.3 与 0.4"}
            ]
        },
        "scoring_ref": {
            "scorer_id": "exact_match",
            "scorer_params": {"answer": "B"},
        },
        "error_bindings": [
            {"option_value": "A", "label": "0.3 > 0.4", "error_type_id": "et_comp_flaw",
             "collision": False, "corpus_ref": None}
        ],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "test-pipeline", "version": "1.0"},
            "signed_by": "test-author",
            "signed_at": "2026-07-27T00:00:00Z",
        },
    }


# ────────────────────────────────────────────────────────────────────
# Mock 验证器工厂（与 test_gate_orchestrator.py 同模式）
# ────────────────────────────────────────────────────────────────────
# 为什么自造 mock 而非用 generic 真实实现：签发测试要确定性地触发 pass/fail
# 路径，真实 LicenseValidator 在无 license_id 时必 fail，难以同时覆盖成功与失败
# 两种场景。mock 让两条路径都可控。


def _make_mock_validator(
    vid: str,
    verdict: str = "pass",
    blocking: bool = True,
    evidence: dict[str, Any] | None = None,
) -> type[Validator]:
    """工厂：构造一个 mock Validator 子类."""

    class _MockValidator(Validator):
        validator_id = vid
        version = f"test-1.0.0+{vid}"

        async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
            return self._timed_result(
                verdict=verdict,
                evidence=evidence or {"mock_id": vid, "verdict": verdict},
                confidence=Decimal("1.000"),
                elapsed_ms=5,
                cost_tokens=0,
            )

    _MockValidator.blocking = blocking
    _MockValidator.cost_tier = "cheap"
    return _MockValidator


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _baseline_registry(monkeypatch):
    """每测试前重置验证器注册表 + 注册 mock + 覆写 load_default_policy.

    为什么覆写 load_default_policy：默认策略含 license 验证器（对 item 必 fail），
    测试需用自定义策略控制 pass/fail 路径。monkeypatch 在 yield 后自动还原。
    """
    reset_registry()
    # 注册 platform 通用验证器（_ensure_generic_validator_stubs 需要）
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    _ensure_generic_validator_stubs()

    yield

    reset_registry()
    register_validator("platform", SchemaValidator)
    _ensure_generic_validator_stubs()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    """每测试前清空 gate 三表 + 内容表 + 插入 cert:none 占位行.

    锁竞争说明（W2b-api 调试）：
    - async_session 用 savepoint 模式，TRUNCATE 的 ACCESS EXCLUSIVE 锁持有到
      外层事务回滚（测试结束）。
    - test_gate_bypass.py 的 _truncate_gate_tables fixture 用 async_engine.connect()
      独立连接提交 TRUNCATE，与本 fixture 的 savepoint 内 TRUNCATE 在重叠表
      上曾触发 deadlock detected。
    - 缓解：本 fixture 在 INSERT cert:none 后立即 commit（RELEASE SAVEPOINT），
      并依赖 async_session teardown 的外层事务回滚释放锁。fixture 顺序上
      test_issue_flow 在 test_gate_bypass 之前执行（字母序），无反向持锁。
    - 若仍偶发 deadlock，可参考 test_gate_bypass.py 改用 async_engine.connect()
      独立连接提交（但需注意 pool 容量与 hang 风险，W2b 实证 hang）。
    """
    await async_session.execute(
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
    # cert:none 占位（编排器失败路径的 FK 目标）
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES ('cert:none', 'placeholder-for-failed-run', 'publish',"
            " 'no-policy', 'system')"
        )
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 工作台 client fixture（带 session cookie）
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def workbench_client(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """构造工作台 ASGI client，DB 走 async_session fixture，并预登录带 cookie."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = get_workbench_token()
        login_resp = await client.post(
            "/login",
            data={"token": token, "next": "/items"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303, f"预登录失败：{login_resp.text}"
        yield client

    app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────
# 辅助：构造 draft item_version
# ────────────────────────────────────────────────────────────────────


async def _publish_draft(async_session: AsyncSession, pack_id: str = "subject-math") -> dict:
    """写入一条 draft item_version，返回 {item_id, item_version_id}."""
    return await publish_item_version(
        item_id=None,
        version_data=_valid_version_data(pack_id=pack_id),
        gate_certificate_id=None,
        db=async_session,
    )


def _build_test_policy(
    validators: list[tuple[str, bool | None]],
    pack_id: str = "subject-math",
) -> GatePolicy:
    """构造测试用 GatePolicy（仅 item 链）.

    Args:
        validators: [(validator_id, blocking), ...]
        blocking=None 表示让编排器取验证器类属性。

    Returns:
        GatePolicy 对象。
    """
    return GatePolicy(
        policy_version="test-policy-v1",
        status="frozen-candidate",
        description="测试策略",
        chains=[
            ChainEntry(
                pack_id=pack_id,
                artifact_type="item",
                validators=[
                    ValidatorStep(validator_id=vid, blocking=blk)
                    for vid, blk in validators
                ],
            )
        ],
    )


def _patch_policy(monkeypatch, policy: GatePolicy) -> None:
    """覆写 load_default_policy 返回测试策略."""
    monkeypatch.setattr(
        "src.workbench.pages.issue.load_default_policy",
        lambda: policy,
    )


# ════════════════════════════════════════════════════════════════════
# §1 签发页：GET /issue/{item_version_id}
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_issue_page_draft_shows_sign_button(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /issue/{id} 对 draft → 200，展示 item_version 信息 + 确认签发按钮."""
    info = await _publish_draft(async_session)
    resp = await workbench_client.get(f"/issue/{info['item_version_id']}")
    assert resp.status_code == 200, resp.text
    assert "签发" in resp.text
    assert "确认签发" in resp.text
    assert info["item_version_id"] in resp.text
    assert "draft" in resp.text


@pytest.mark.asyncio
async def test_issue_page_404(workbench_client: AsyncClient) -> None:
    """GET /issue/nonexistent → 404 错误页."""
    resp = await workbench_client.get("/issue/nonexistent-id")
    assert resp.status_code == 404
    assert "不存在" in resp.text


@pytest.mark.asyncio
async def test_issue_page_already_published_no_button(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /issue/{id} 对 published 版本 → 200，但不展示签发按钮（状态机无重签）."""
    # 先创建 draft，再用 publication 服务直接前移到 published
    info = await _publish_draft(async_session)
    cert_id = "cert_test_published_001"
    # 直接 INSERT 一张门证书（绕过 run_gate，专注测试 UI 行为）
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES (:cid, :aref, 'publish', 'test', 'tester')"
        ),
        {"cid": cert_id, "aref": info["item_version_id"]},
    )
    await async_session.commit()
    await issue_item_version(
        item_version_id=info["item_version_id"],
        gate_certificate_id=cert_id,
        published_by="tester",
        db=async_session,
    )

    resp = await workbench_client.get(f"/issue/{info['item_version_id']}")
    assert resp.status_code == 200, resp.text
    # 已 published 不展示签发按钮
    assert "确认签发" not in resp.text
    assert "不可签发" in resp.text or "已" in resp.text


# ════════════════════════════════════════════════════════════════════
# §2 签发成功：POST /issue/{item_version_id}
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_issue_submit_success_publishes(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} 全部 pass → 状态前移到 published + publication 记录落库.

    验收 §2：点击签发调用门编排；全部通过后生成 GateCertificate 并更新
              item_version.status=published。
    """
    info = await _publish_draft(async_session)

    # 注册全 pass 的 mock 验证器
    register_validator("subject-math", _make_mock_validator("test_pass_v1", "pass", True))
    register_validator("subject-math", _make_mock_validator("test_pass_v2", "pass", False))

    policy = _build_test_policy([
        ("test_pass_v1", True),
        ("test_pass_v2", False),
    ])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 成功页关键文案
    assert "签发成功" in resp.text
    assert "published" in resp.text
    assert "publication_id" in resp.text or "pub_" in resp.text

    # 数据库验证：item_version.status='published'
    version = await async_session.get(ItemVersion, info["item_version_id"])
    assert version is not None
    assert version.status == "published"
    assert version.gate_certificate_id is not None
    assert version.gate_certificate_id.startswith("cert_")
    assert version.published_at is not None

    # publication 表落记录
    pub = await get_publication_by_version(info["item_version_id"], async_session)
    assert pub is not None
    assert pub["item_id"] == info["item_id"]
    assert pub["item_version_id"] == info["item_version_id"]
    assert pub["gate_certificate_id"] == version.gate_certificate_id
    assert pub["published_by"] is not None  # W2 单用户 = token

    # item.current_version_id 自动前移（触发器）
    item = await async_session.get(Item, info["item_id"])
    assert item.current_version_id == info["item_version_id"]


@pytest.mark.asyncio
async def test_issue_submit_success_gate_certificate_persisted(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} 成功 → gate_certificate 表 INSERT 一行（合法证书来源）."""
    info = await _publish_draft(async_session)

    register_validator("subject-math", _make_mock_validator("test_pass_v1", "pass", True))
    policy = _build_test_policy([("test_pass_v1", True)])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    # gate_certificate 表新增一行（除 cert:none 外）
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate WHERE cert_id != 'cert:none'")
    )
    assert cert_count == 1
    # 该证书的 artifact_ref 指向被签发的 item_version_id
    cert_ref = await async_session.scalar(
        text(
            "SELECT artifact_ref FROM gate_certificate"
            " WHERE cert_id != 'cert:none' LIMIT 1"
        )
    )
    assert cert_ref == info["item_version_id"]


# ════════════════════════════════════════════════════════════════════
# §3 签发失败：POST /issue/{item_version_id}
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_issue_submit_fail_stays_draft(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} 阻断 fail → 状态停留在 draft + 展示 fail 证据.

    验收 §3：失败时门状态停留在 quarantined/draft，展示 fail 证据。
    """
    info = await _publish_draft(async_session)

    # 注册 fail 验证器（阻断）
    register_validator(
        "subject-math",
        _make_mock_validator(
            "test_fail_v1",
            "fail",
            True,
            evidence={"reason": "测试失败：缺关键字段", "missing": ["foo"]},
        ),
    )
    register_validator("subject-math", _make_mock_validator("test_pass_v2", "pass", True))

    policy = _build_test_policy([
        ("test_fail_v1", True),
        ("test_pass_v2", True),
    ])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 失败页关键文案
    assert "签发未通过" in resp.text
    assert "fail" in resp.text
    # 证据展示
    assert "测试失败" in resp.text or "test_fail_v1" in resp.text

    # 数据库验证：item_version.status 仍为 draft（未前移）
    version = await async_session.get(ItemVersion, info["item_version_id"])
    assert version is not None
    assert version.status == "draft"
    assert version.gate_certificate_id is None
    assert version.published_at is None

    # 无 publication 记录
    pub = await get_publication_by_version(info["item_version_id"], async_session)
    assert pub is None

    # item.current_version_id 未前移
    item = await async_session.get(Item, info["item_id"])
    assert item.current_version_id is None


@pytest.mark.asyncio
async def test_issue_submit_fail_no_certificate_issued(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} fail → 不签发新 GateCertificate（仅 cert:none 占位）."""
    info = await _publish_draft(async_session)

    register_validator(
        "subject-math",
        _make_mock_validator("test_fail_v1", "fail", True),
    )
    policy = _build_test_policy([("test_fail_v1", True)])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    # 不签发新证书（仅 cert:none 占位）
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate WHERE cert_id != 'cert:none'")
    )
    assert cert_count == 0


@pytest.mark.asyncio
async def test_issue_submit_fail_shows_short_circuit(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} 第一个 fail → 短路，后续验证器未调用，证据展示 short_circuit_at."""
    info = await _publish_draft(async_session)

    register_validator(
        "subject-math",
        _make_mock_validator("test_fail_first", "fail", True),
    )
    # 第二个验证器应被短路（不调用）
    register_validator("subject-math", _make_mock_validator("test_should_skip", "pass", True))

    policy = _build_test_policy([
        ("test_fail_first", True),
        ("test_should_skip", True),
    ])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 失败页展示短路位置
    assert "test_fail_first" in resp.text
    # short_circuited 验证器在表格中展示
    assert "test_should_skip" in resp.text


@pytest.mark.asyncio
async def test_issue_submit_review_no_publish(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /issue/{id} review（非阻断）→ 不签证书、不前移状态、展示 review 证据."""
    info = await _publish_draft(async_session)

    # 非阻断 review（duplicate_placeholder 的语义）
    register_validator(
        "subject-math",
        _make_mock_validator(
            "test_review_v1",
            "review",
            False,
            evidence={"reason": "需人工复核", "tag": "可疑内容"},
        ),
    )
    register_validator("subject-math", _make_mock_validator("test_pass_v1", "pass", True))

    policy = _build_test_policy([
        ("test_review_v1", False),
        ("test_pass_v1", True),
    ])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert "签发未通过" in resp.text
    assert "review" in resp.text

    # review 不签发证书
    version = await async_session.get(ItemVersion, info["item_version_id"])
    assert version.status == "draft"
    assert version.gate_certificate_id is None


# ════════════════════════════════════════════════════════════════════
# §4 单元测试覆盖签发成功与签发失败（直接调服务层）
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_issue_item_version_invalid_state_raises(
    async_session: AsyncSession,
) -> None:
    """issue_item_version 对已 published 的版本 → 抛 IssueError."""
    info = await _publish_draft(async_session)
    cert_id = "cert_test_state_001"
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES (:cid, :aref, 'publish', 'test', 'tester')"
        ),
        {"cid": cert_id, "aref": info["item_version_id"]},
    )
    await async_session.commit()

    # 第一次签发：成功
    result = await issue_item_version(
        item_version_id=info["item_version_id"],
        gate_certificate_id=cert_id,
        published_by="tester",
        db=async_session,
    )
    assert result["publication_id"].startswith("pub_")

    # 第二次签发：抛 IssueError（状态机无重签）
    with pytest.raises(IssueError, match="已是 published"):
        await issue_item_version(
            item_version_id=info["item_version_id"],
            gate_certificate_id=cert_id,
            published_by="tester",
            db=async_session,
        )


@pytest.mark.asyncio
async def test_issue_item_version_nonexistent_raises(
    async_session: AsyncSession,
) -> None:
    """issue_item_version 对不存在的 item_version_id → 抛 IssueError."""
    with pytest.raises(IssueError, match="不存在"):
        await issue_item_version(
            item_version_id="nonexistent-id-xyz",
            gate_certificate_id="cert_xxx",
            published_by="tester",
            db=async_session,
        )


@pytest.mark.asyncio
async def test_issue_item_version_missing_args_raises(
    async_session: AsyncSession,
) -> None:
    """issue_item_version 缺必填参数 → 抛 ValueError."""
    with pytest.raises(ValueError, match="item_version_id 必填"):
        await issue_item_version(
            item_version_id="",
            gate_certificate_id="cert_xxx",
            published_by="tester",
            db=async_session,
        )

    with pytest.raises(ValueError, match="gate_certificate_id 必填"):
        await issue_item_version(
            item_version_id="some-id",
            gate_certificate_id="",
            published_by="tester",
            db=async_session,
        )


@pytest.mark.asyncio
async def test_issue_item_version_writes_publication_record(
    async_session: AsyncSession,
) -> None:
    """issue_item_version 成功 → publication 表写入一行（签发账留痕）."""
    info = await _publish_draft(async_session)
    cert_id = "cert_test_pub_001"
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES (:cid, :aref, 'publish', 'test', 'tester')"
        ),
        {"cid": cert_id, "aref": info["item_version_id"]},
    )
    await async_session.commit()

    result = await issue_item_version(
        item_version_id=info["item_version_id"],
        gate_certificate_id=cert_id,
        published_by="test-operator",
        db=async_session,
    )

    # publication 表查询
    pub_count = await async_session.scalar(
        text("SELECT count(*) FROM publication")
    )
    assert pub_count == 1
    # 字段一致
    row = (
        await async_session.execute(
            text(
                "SELECT publication_id, item_id, item_version_id,"
                " gate_certificate_id, published_by"
                " FROM publication LIMIT 1"
            )
        )
    ).one()
    assert row[0] == result["publication_id"]
    assert row[1] == info["item_id"]
    assert row[2] == info["item_version_id"]
    assert row[3] == cert_id
    assert row[4] == "test-operator"


# ════════════════════════════════════════════════════════════════════
# 宪法 D2 验证：published_at 非空必伴随 gate_certificate_id 非空
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_published_state_has_both_gate_cert_and_published_at(
    workbench_client: AsyncClient,
    async_session: AsyncSession,
    monkeypatch,
) -> None:
    """签发成功后 item_version.published_at 与 gate_certificate_id 同时非空（D2 强制）."""
    info = await _publish_draft(async_session)
    register_validator("subject-math", _make_mock_validator("test_pass_v1", "pass", True))
    policy = _build_test_policy([("test_pass_v1", True)])
    _patch_policy(monkeypatch, policy)

    resp = await workbench_client.post(
        f"/issue/{info['item_version_id']}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    version = await async_session.get(ItemVersion, info["item_version_id"])
    assert version.status == "published"
    # D2：published_at 非空必伴随 gate_certificate_id 非空
    assert version.published_at is not None
    assert version.gate_certificate_id is not None
