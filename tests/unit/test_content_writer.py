"""T-W1-007 内容版本写入服务单元测试.

对照 T-W01-T03 验证卡 §T-W1-007 部分：
1. publish_item_version 函数签名
2. 正常路径：draft 版本
3. 门强制：published 无证书 → 失败
4. 门强制：draft/quarantined 无证书 → 成功
5. 有证书发布 → 成功，gate_certificate_id 在列字段
6. lineage 必填字段写入
7. 同一 item 多次发布：版本累积 + 指针更新
8. 旧版本永不删除
9. publish_material 骨架
10. publish_corpus_asset 骨架
"""
from __future__ import annotations

import inspect
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.content.writer import (
    GateEnforcementError,
    publish_corpus_asset,
    publish_item_version,
    publish_material,
)
from src.core.models.item import Item
from src.core.models.item_version import ItemVersion, Lineage
from src.core.models.material import Material
from src.core.models.material_version import MaterialVersion
from src.core.models.corpus_asset import CorpusAsset
from src.core.models.corpus_version import CorpusVersion


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture：每测试前 TRUNCATE 所有内容表（CASCADE）
# ────────────────────────────────────────────────────────────────────
# 为什么需要：conftest 不用事务回滚（要命中 PG 触发器/CHECK），数据会跨测试累积。
# 内容寻址（D3）使同 content 必产生同 item_version_id——多次 default-content 测试
# 会触发 PK 冲突。CASCADE 自动处理 FK 依赖（item_version→item 等）。
# 为什么用 TRUNCATE 而非 DELETE：D1 append-only 表（item_version 等）未来会有
# BEFORE UPDATE/DELETE 触发器；TRUNCATE 是 DDL 不触发行级触发器，安全。
@pytest_asyncio.fixture(autouse=True)
async def _truncate_content_tables(async_session: AsyncSession):
    await async_session.execute(
        text(
            "TRUNCATE TABLE "
            "item_version, item, item_kp, publication, item_group, "
            "material_version, material, corpus_version, corpus_asset, "
            "material_license "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


def _uid(prefix: str = "id") -> str:
    """生成短唯一 id."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _make_version_data(
    status: str = "draft",
    tier: str = "C",
    content: dict | None = None,
) -> dict:
    """构造一份最小可用的 version_data."""
    return {
        "pack_id": "subject-math",
        "tier": tier,
        "status": status,
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.test"}],
            "kp_set_mode": "single",
            "cognitive_level": "remember",
            "gradeband": "L",
            "graph_release": "2026.1",
        },
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "content": content or {"blocks": []},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {}},
        "error_bindings": [],
        "lineage": {
            "tier": tier,
            "pipeline": {"id": "test", "version": "1.0"},
            "signed_by": "tester",
            "signed_at": "2026-01-01T00:00:00Z",
        },
    }


def _make_cert_id() -> str:
    """生成一个假的 gate_certificate_id（T-W1-006 未落地，列字段无 FK）."""
    return f"gate-cert-{uuid.uuid4().hex[:12]}"


# ────────────────────────────────────────────────────────────────────
# §1 函数签名验证
# ────────────────────────────────────────────────────────────────────

def test_publish_item_version_signature():
    """验收标准 #1：函数含 item_id, version_data, gate_certificate_id 参数."""
    sig = inspect.signature(publish_item_version)
    params = list(sig.parameters.keys())
    assert "item_id" in params, "缺少 item_id 参数"
    assert "version_data" in params, "缺少 version_data 参数"
    assert "gate_certificate_id" in params, "缺少 gate_certificate_id 参数"
    # gate_certificate_id 默认值为 None
    assert sig.parameters["gate_certificate_id"].default is None


def test_no_subject_pack_imports_in_content():
    """宪法 A5/X6：src/core/content/ 不 import 任何学科包/学段包."""
    import os
    import re

    content_dir = os.path.join("src", "core", "content")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations = []
    for fname in os.listdir(content_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(content_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if pattern.findall(content):
            violations.append(fname)
    assert not violations, f"src/core/content/ 存在学科包 import：{violations}"


# ────────────────────────────────────────────────────────────────────
# §2 正常路径：draft 版本
# ────────────────────────────────────────────────────────────────────

async def test_publish_draft_version_creates_item_and_version(async_session):
    """正常路径：新建 item + draft 版本."""
    version_data = _make_version_data(status="draft")

    result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        db=async_session,
    )

    assert "item_id" in result
    assert "item_version_id" in result

    # 验证 item 和 version 在 DB 中存在
    item = await async_session.get(Item, result["item_id"])
    assert item is not None
    assert item.pack_id == "subject-math"
    assert item.tier == "C"

    version = await async_session.get(ItemVersion, result["item_version_id"])
    assert version is not None
    assert version.status == "draft"
    assert version.item_id == result["item_id"]

    # draft 版本不更新 current_version_id（触发器只在 published 时前移）
    assert item.current_version_id is None


# ────────────────────────────────────────────────────────────────────
# §3 门强制：published 无证书 → 失败
# ────────────────────────────────────────────────────────────────────

async def test_published_without_certificate_fails(async_session):
    """门强制：status=published 且 gate_certificate_id=None → 失败."""
    version_data = _make_version_data(status="published")

    with pytest.raises(GateEnforcementError):
        await publish_item_version(
            item_id=None,
            version_data=version_data,
            gate_certificate_id=None,
            db=async_session,
        )

    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §4 门强制：draft/quarantined 无证书 → 成功
# ────────────────────────────────────────────────────────────────────

async def test_draft_without_certificate_succeeds(async_session):
    """draft 状态不要求门证书."""
    version_data = _make_version_data(status="draft")

    result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        gate_certificate_id=None,
        db=async_session,
    )
    assert result["item_id"] is not None

    version = await async_session.get(ItemVersion, result["item_version_id"])
    assert version is not None
    assert version.gate_certificate_id is None
    assert version.published_at is None


async def test_quarantined_without_certificate_succeeds(async_session):
    """quarantined 状态不要求门证书（但需 rendered_snapshot，writer 自动补占位）."""
    version_data = _make_version_data(status="quarantined")

    result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        gate_certificate_id=None,
        db=async_session,
    )
    assert result["item_version_id"] is not None

    version = await async_session.get(ItemVersion, result["item_version_id"])
    assert version is not None
    assert version.status == "quarantined"
    assert version.rendered_snapshot is not None  # CHECK 兜底


# ────────────────────────────────────────────────────────────────────
# §5 有证书发布 → 成功且 gate_certificate_id 写入列字段
# ────────────────────────────────────────────────────────────────────

async def test_published_with_certificate_succeeds(async_session):
    """持 gate_certificate_id 发布 published 版本 → 成功，列字段正确写入."""
    cert_id = _make_cert_id()
    version_data = _make_version_data(status="published")

    result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        gate_certificate_id=cert_id,
        db=async_session,
    )

    version = await async_session.get(ItemVersion, result["item_version_id"])
    assert version is not None
    # gate_certificate_id 在列字段中（唯一真源）
    assert version.gate_certificate_id == cert_id
    # lineage 内不重复存储 gate_certificate_id
    assert "gate_certificate_id" not in version.lineage
    # published_at 非空
    assert version.published_at is not None

    # 触发器自动前移 current_version_id
    item = await async_session.get(Item, result["item_id"])
    assert item.current_version_id == result["item_version_id"]


# ────────────────────────────────────────────────────────────────────
# §6 lineage 必填字段写入
# ────────────────────────────────────────────────────────────────────

async def test_lineage_required_fields_written(async_session):
    """lineage 中 tier/pipeline/signed_by/signed_at 正确写入."""
    version_data = _make_version_data(status="draft", tier="D")
    version_data["lineage"] = {
        "tier": "D",
        "pipeline": {"id": "manual", "version": "1.0"},
        "signed_by": "author_x",
        "signed_at": "2026-07-26T12:00:00Z",
    }

    result = await publish_item_version(
        item_id=None,
        version_data=version_data,
        db=async_session,
    )

    v = await async_session.get(ItemVersion, result["item_version_id"])
    lin = Lineage(**v.lineage)
    assert lin.tier == "D"
    assert lin.pipeline.id == "manual"
    assert lin.signed_by == "author_x"
    assert lin.signed_at == "2026-07-26T12:00:00Z"


# ────────────────────────────────────────────────────────────────────
# §7/§8 同一 item 多次发布：版本累积 + 指针更新 + 旧版不删
# ────────────────────────────────────────────────────────────────────

async def test_multiple_publishes_version_accumulation(async_session):
    """同一 item 两次 published → 两个 version 均存在、current 指向最新."""
    cert_id1 = _make_cert_id()
    cert_id2 = _make_cert_id()

    # 第一次发布
    vdata1 = _make_version_data(status="published", content={"blocks": [{"type": "text", "value": "v1"}]})
    r1 = await publish_item_version(
        item_id=None,
        version_data=vdata1,
        gate_certificate_id=cert_id1,
        db=async_session,
    )
    item_id = r1["item_id"]
    v1_id = r1["item_version_id"]

    # 第二次发布（修改 content → 不同 item_version_id）
    vdata2 = _make_version_data(status="published", content={"blocks": [{"type": "text", "value": "v2"}]})
    r2 = await publish_item_version(
        item_id=item_id,
        version_data=vdata2,
        gate_certificate_id=cert_id2,
        db=async_session,
    )
    v2_id = r2["item_version_id"]

    # 两个 version 都存在
    v1 = await async_session.get(ItemVersion, v1_id)
    v2 = await async_session.get(ItemVersion, v2_id)
    assert v1 is not None, "旧版本 v1 不应被删除"
    assert v2 is not None
    assert v1_id != v2_id

    # current_version_id 指向最新 published
    item = await async_session.get(Item, item_id)
    assert item.current_version_id == v2_id

    # 统计 version 数量应为 2
    count = await async_session.scalar(
        select(func.count()).select_from(ItemVersion).where(
            ItemVersion.item_id == item_id
        )
    )
    assert count == 2, f"应有 2 个版本，实际 {count}"


# ────────────────────────────────────────────────────────────────────
# §9 publish_material 骨架
# ────────────────────────────────────────────────────────────────────

async def test_publish_material_creates_two_stage(async_session):
    """material + material_version 两段式写入."""
    # 先创建 material_license（FK 依赖）
    license_id = _uid("license")
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, decision) "
            "VALUES (:lid, 'approved')"
        ),
        {"lid": license_id},
    )
    await async_session.flush()

    mat_data = {
        "kind": "passage",
        "pack_id": "platform",
        "content_ref": f"minio:materials/sha256:{uuid.uuid4().hex[:8]}",
        "license_id": license_id,
        "status": "draft",
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "upload", "version": "1.0"},
            "signed_by": "test",
            "signed_at": "2026-01-01T00:00:00Z",
        },
    }

    result = await publish_material(material_data=mat_data, db=async_session)
    assert "material_id" in result
    assert "material_version_id" in result

    m = await async_session.get(Material, result["material_id"])
    mv = await async_session.get(MaterialVersion, result["material_version_id"])
    assert m is not None
    assert mv is not None
    assert mv.material_id == m.material_id
    assert mv.status == "draft"


# ────────────────────────────────────────────────────────────────────
# §10 publish_corpus_asset 骨架
# ────────────────────────────────────────────────────────────────────

async def test_publish_corpus_creates_two_stage(async_session):
    """corpus_asset + corpus_version 两段式写入."""
    # 先创建 material_license（FK 依赖）
    license_id = _uid("license")
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, decision) "
            "VALUES (:lid, 'approved')"
        ),
        {"lid": license_id},
    )
    await async_session.flush()

    corpus_data = {
        "kind": "wordlist",
        "pack_id": "platform",
        "content_ref": f"minio:corpus/sha256:{uuid.uuid4().hex[:8]}",
        "license_id": license_id,
        "status": "draft",
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "upload", "version": "1.0"},
            "signed_by": "test",
            "signed_at": "2026-01-01T00:00:00Z",
        },
    }

    result = await publish_corpus_asset(corpus_data=corpus_data, db=async_session)
    assert "asset_id" in result
    assert "version_id" in result

    a = await async_session.get(CorpusAsset, result["asset_id"])
    cv = await async_session.get(CorpusVersion, result["version_id"])
    assert a is not None
    assert cv is not None
    assert cv.asset_id == a.asset_id
    assert cv.status == "draft"


# ────────────────────────────────────────────────────────────────────
# §11 T-W2-018 publish_corpus_asset 门字段对齐断言
# ────────────────────────────────────────────────────────────────────
# 修复 W1 遗留：published 状态必须写入 gate_certificate_id + published_at；
# retired 状态写入 retired_at；draft 状态保持 NULL（与 material_version 对齐）。

async def _seed_license(async_session) -> str:
    """创建 approved 状态的 material_license 行并返回 license_id."""
    license_id = _uid("license")
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, decision) "
            "VALUES (:lid, 'approved')"
        ),
        {"lid": license_id},
    )
    await async_session.flush()
    return license_id


def _make_corpus_data(license_id: str, status: str = "draft") -> dict:
    """构造一份最小可用的 corpus_data."""
    return {
        "kind": "wordlist",
        "pack_id": "platform",
        "content_ref": f"minio:corpus/sha256:{uuid.uuid4().hex[:8]}",
        "license_id": license_id,
        "status": status,
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "upload", "version": "1.0"},
            "signed_by": "test",
            "signed_at": "2026-01-01T00:00:00Z",
        },
    }


async def test_publish_corpus_draft_has_no_gate_fields(async_session):
    """T-W2-018 验收 #1：draft 状态不写 gate_certificate_id 与 published_at."""
    license_id = await _seed_license(async_session)
    corpus_data = _make_corpus_data(license_id, status="draft")

    result = await publish_corpus_asset(corpus_data=corpus_data, db=async_session)
    cv = await async_session.get(CorpusVersion, result["version_id"])

    assert cv.status == "draft"
    assert cv.gate_certificate_id is None
    assert cv.published_at is None
    assert cv.retired_at is None


async def test_publish_corpus_published_without_gate_fails(async_session):
    """T-W2-018 验收 #2：published 状态无 gate_certificate_id → GateEnforcementError."""
    license_id = await _seed_license(async_session)
    corpus_data = _make_corpus_data(license_id, status="published")

    with pytest.raises(GateEnforcementError):
        await publish_corpus_asset(
            corpus_data=corpus_data,
            gate_certificate_id=None,
            db=async_session,
        )

    await async_session.rollback()


async def test_publish_corpus_published_with_gate_writes_fields(async_session):
    """T-W2-018 验收 #1：published + gate_cert → gate_certificate_id 与 published_at 写入."""
    license_id = await _seed_license(async_session)
    corpus_data = _make_corpus_data(license_id, status="published")
    cert_id = _make_cert_id()

    result = await publish_corpus_asset(
        corpus_data=corpus_data,
        gate_certificate_id=cert_id,
        db=async_session,
    )
    cv = await async_session.get(CorpusVersion, result["version_id"])

    assert cv.status == "published"
    # 关键门字段对齐（原 W1 骨架漏写这里）
    assert cv.gate_certificate_id == cert_id, "gate_certificate_id 未写入"
    assert cv.published_at is not None, "published_at 未写入"
    assert cv.retired_at is None


async def test_publish_corpus_retired_writes_retired_at(async_session):
    """T-W2-018 验收 #1：retired 状态写入 retired_at（与 material_version 对齐）."""
    license_id = await _seed_license(async_session)
    corpus_data = _make_corpus_data(license_id, status="retired")

    result = await publish_corpus_asset(corpus_data=corpus_data, db=async_session)
    cv = await async_session.get(CorpusVersion, result["version_id"])

    assert cv.status == "retired"
    assert cv.retired_at is not None, "retired_at 未写入"
    # retired 状态不需要 gate_certificate_id（语义：从已发布退役，门证书已在 published 版本中）
    assert cv.gate_certificate_id is None
    assert cv.published_at is None


async def test_publish_corpus_gate_enforcement_error_is_raised_before_db_write(async_session):
    """T-W2-018 验收 #2：门强制失败时 DB 不应被污染（无 corpus_asset / corpus_version 行）."""
    from sqlalchemy import func, select

    license_id = await _seed_license(async_session)
    corpus_data = _make_corpus_data(license_id, status="published")

    with pytest.raises(GateEnforcementError):
        await publish_corpus_asset(
            corpus_data=corpus_data,
            gate_certificate_id=None,
            db=async_session,
        )
    await async_session.rollback()

    asset_count = await async_session.scalar(
        select(func.count()).select_from(CorpusAsset)
    )
    version_count = await async_session.scalar(
        select(func.count()).select_from(CorpusVersion)
    )
    assert asset_count == 0, "GateEnforcementError 前不应创建 corpus_asset"
    assert version_count == 0, "GateEnforcementError 前不应创建 corpus_version"


async def test_publish_corpus_status_field_alignment_with_material(async_session):
    """T-W2-018 验收 #1：corpus_version.status 接受四态（draft/quarantined/published/retired）.

    与 material_version / item_version 共用 item_version_status_enum；
    任何 enum 外的值应被数据库拒绝（CHECK 约束）。
    """
    license_id = await _seed_license(async_session)

    # quarantined 状态也应能写入（不要求 gate_certificate_id）
    corpus_data = _make_corpus_data(license_id, status="quarantined")
    result = await publish_corpus_asset(corpus_data=corpus_data, db=async_session)
    cv = await async_session.get(CorpusVersion, result["version_id"])
    assert cv.status == "quarantined"
    # quarantined 同样不写 published_at（语义：未签发）
    assert cv.published_at is None
    assert cv.gate_certificate_id is None
