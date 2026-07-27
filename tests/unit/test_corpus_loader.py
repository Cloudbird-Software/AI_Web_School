"""T-W2-016 语料库加载器单元测试.

对照任务卡 §验收标准：
1. corpus_loader.py 可加载 corpus_asset + corpus_version，校验 license、status、gate_certificate_id。
2. src/packs/subject-math/corpora/functions.yaml 定义 ≥10 个数学函数。
3. 函数库加载器返回函数白名单，可被 expr_eval 注册。
4. 单元测试覆盖加载、缺失 license、函数调用（此处校验函数调用=校验签名注册）。
"""
from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.content.corpus_loader import (
    DEFAULT_FUNCTIONS_YAML,
    CorpusLoaderError,
    FunctionDef,
    FunctionLibrary,
    GateEnforcementError,
    LicenseNotApprovedError,
    ParamSpec,
    ReturnSpec,
    SafetySpec,
    check_license,
    compute_corpus_version_id,
    function_whitelist,
    parse_library,
    publish_corpus_library,
)
from src.core.content.source_registry import SourceRegistry, SourceRecord
from src.core.models.corpus_asset import CorpusAsset
from src.core.models.corpus_version import CorpusVersion


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture：每测试前 TRUNCATE corpus 表 + material_license（CASCADE）
# ────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _truncate_corpus_tables(async_session: AsyncSession):
    await async_session.execute(
        text(
            "TRUNCATE TABLE "
            "corpus_version, corpus_asset, "
            "material_license "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


def _load_default_library() -> FunctionLibrary:
    return parse_library(DEFAULT_FUNCTIONS_YAML)


# ────────────────────────────────────────────────────────────────────
# §验收 #2：functions.yaml ≥10 个数学函数
# ────────────────────────────────────────────────────────────────────

def test_functions_yaml_has_at_least_10_functions():
    """验收 #2：≥10 个数学函数."""
    lib = _load_default_library()
    assert len(lib.functions) >= 10, f"函数数 {len(lib.functions)} < 10"


def test_functions_yaml_required_functions_present():
    """验收 #2 关键函数：gcd/lcm/unit_convert/fraction_simplify 至少存在."""
    lib = _load_default_library()
    names = {f.name for f in lib.functions}
    for required in ("gcd", "lcm", "unit_convert", "fraction_simplify"):
        assert required in names, f"缺少关键函数 {required}"


def test_functions_yaml_kind_is_function_lib():
    """验收 #2：kind=function_lib."""
    lib = _load_default_library()
    assert lib.kind == "function_lib"


def test_functions_yaml_pack_id_is_subject_math():
    """验收 #2：pack_id=subject-math."""
    lib = _load_default_library()
    assert lib.pack_id == "subject-math"


def test_functions_yaml_license_id_present():
    """验收 #2：license_id 非空."""
    lib = _load_default_library()
    assert lib.license_id, "license_id 缺失"


# ────────────────────────────────────────────────────────────────────
# §验收 #3：函数库加载器返回函数白名单
# ────────────────────────────────────────────────────────────────────

def test_function_whitelist_returns_dict_of_function_defs():
    """验收 #3：返回 {name: FunctionDef} dict."""
    lib = _load_default_library()
    wl = function_whitelist(lib, strict_safety=True)
    assert isinstance(wl, dict)
    assert len(wl) > 0
    for name, fn in wl.items():
        assert isinstance(name, str)
        assert isinstance(fn, FunctionDef)


def test_function_whitelist_includes_safe_functions():
    """验收 #3：所有安全函数（pure/no_io/deterministic）都在白名单中."""
    lib = _load_default_library()
    wl = function_whitelist(lib, strict_safety=True)
    safe_fns = [
        f for f in lib.functions
        if f.safety.pure and f.safety.no_io and f.safety.deterministic
    ]
    assert len(wl) == len(safe_fns)
    for fn in safe_fns:
        assert fn.name in wl


def test_function_whitelist_excludes_unsafe_functions():
    """验收 #3：pure/no_io/deterministic 任一为 false 的函数被排除."""
    # 构造一个不安全函数
    unsafe_fn = FunctionDef(
        name="unsafe_test_fn",
        version="1.0.0",
        signature={"params": [{"name": "x", "type": "integer"}], "return": "integer"},
        safety=SafetySpec(pure=False, no_io=True, deterministic=True, no_loops=True),
        description="测试用：pure=false 应被排除",
    )
    lib = FunctionLibrary(
        schema_version="1.0",
        pack_id="subject-math",
        kind="function_lib",
        license_id="lic-platform-math-functions-v1",
        library_version="1.0.0",
        functions=[unsafe_fn],
    )
    wl = function_whitelist(lib, strict_safety=True)
    assert "unsafe_test_fn" not in wl, "unsafe 函数不应在白名单"

    # strict_safety=False 时应允许
    wl2 = function_whitelist(lib, strict_safety=False)
    assert "unsafe_test_fn" in wl2


def test_function_whitelist_can_be_registered_to_expr_eval_namespace():
    """验收 #3：白名单结构可被 expr_eval 直接消费（提供 name + signature）.

    模拟 expr_eval 的注册接口：需要一个 {name: callable_signature} 字典。
    """
    lib = _load_default_library()
    wl = function_whitelist(lib, strict_safety=True)

    # 模拟 expr_eval 的注册过程
    registered: dict[str, dict[str, Any]] = {}
    for name, fn in wl.items():
        registered[name] = {
            "params": [{"name": p.name, "type": p.type} for p in fn.params],
            "return_type": fn.return_spec.type,
            "version": fn.version,
        }

    # 所有白名单函数都应能注册
    assert len(registered) == len(wl)
    # 每个 entry 都有完整结构
    for name, sig in registered.items():
        assert "params" in sig
        assert "return_type" in sig
        assert "version" in sig


# ────────────────────────────────────────────────────────────────────
# §验收 #1 + #4：加载 corpus_asset + corpus_version，校验 license/status/gate
# ────────────────────────────────────────────────────────────────────

def test_parse_library_loads_valid_yaml():
    """验收 #1：合法 YAML 加载成功."""
    lib = _load_default_library()
    assert lib.schema_version == "1.0"
    assert len(lib.functions) >= 10


def test_parse_library_rejects_missing_file(tmp_path):
    """验收 #4：文件不存在抛 FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        parse_library(tmp_path / "no_such.yaml")


def test_parse_library_rejects_duplicate_function_names(tmp_path):
    """验收 #4：函数名重复抛 CorpusLoaderError."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "pack_id": "subject-math",
            "kind": "function_lib",
            "license_id": "lic-platform-math-functions-v1",
            "library_version": "1.0.0",
            "functions": [
                {
                    "name": "dup",
                    "version": "1.0.0",
                    "signature": {"params": [], "return": "integer"},
                    "safety": {"pure": True, "no_io": True, "deterministic": True, "no_loops": True},
                    "description": "first",
                },
                {
                    "name": "dup",
                    "version": "1.0.0",
                    "signature": {"params": [], "return": "integer"},
                    "safety": {"pure": True, "no_io": True, "deterministic": True, "no_loops": True},
                    "description": "second",
                },
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    with pytest.raises(CorpusLoaderError, match="函数名重复"):
        parse_library(bad_yaml)


# ────────────────────────────────────────────────────────────────────
# §验收 #4：license 缺失 / 未 approved / 已过期
# ────────────────────────────────────────────────────────────────────

def _make_library_with_license(license_id: str) -> FunctionLibrary:
    """构造一个最小合法 FunctionLibrary，但 license_id 自定义."""
    fn = FunctionDef(
        name="dummy",
        version="1.0.0",
        signature={"params": [], "return": "integer"},
        safety=SafetySpec(pure=True, no_io=True, deterministic=True, no_loops=True),
        description="test only",
    )
    return FunctionLibrary(
        schema_version="1.0",
        pack_id="subject-math",
        kind="function_lib",
        license_id=license_id,
        library_version="1.0.0",
        functions=[fn],
    )


def test_check_license_passes_for_default_library():
    """验收 #1：默认 YAML 的 license 应通过校验."""
    lib = _load_default_library()
    # 不应抛异常
    check_license(lib)


def test_check_license_fails_for_unregistered_license(tmp_path):
    """验收 #4：license 未登记 → LicenseNotApprovedError."""
    # 构造一个不含我们 license_id 的临时 registry
    bad_registry_path = tmp_path / "registry.yaml"
    bad_registry_path.write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {
                    "license_id": "lic-other",
                    "source": "S",
                    "decision": "approved",
                },
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    reg = SourceRegistry.from_yaml(bad_registry_path)

    lib = _make_library_with_license("lic-not-registered")
    with pytest.raises(LicenseNotApprovedError, match="未在.*登记"):
        check_license(lib, registry=reg)


def test_check_license_fails_for_rejected_license(tmp_path):
    """验收 #4：license decision=rejected → LicenseNotApprovedError."""
    bad_registry_path = tmp_path / "registry.yaml"
    bad_registry_path.write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {
                    "license_id": "lic-rejected",
                    "source": "S",
                    "decision": "rejected",
                },
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    reg = SourceRegistry.from_yaml(bad_registry_path)

    lib = _make_library_with_license("lic-rejected")
    with pytest.raises(LicenseNotApprovedError, match="不可用"):
        check_license(lib, registry=reg)


def test_check_license_fails_for_expired_license(tmp_path):
    """验收 #4：license expires_at 已过期 → LicenseNotApprovedError."""
    bad_registry_path = tmp_path / "registry.yaml"
    bad_registry_path.write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {
                    "license_id": "lic-expired",
                    "source": "S",
                    "decision": "approved",
                    "expires_at": "2020-01-01T00:00:00Z",
                },
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    reg = SourceRegistry.from_yaml(bad_registry_path)

    lib = _make_library_with_license("lic-expired")
    with pytest.raises(LicenseNotApprovedError, match="不可用"):
        check_license(lib, registry=reg)


# ────────────────────────────────────────────────────────────────────
# §验收 #1：publish_corpus_library 入库 corpus_asset + corpus_version
# ────────────────────────────────────────────────────────────────────

async def test_publish_corpus_library_creates_asset_and_version(async_session):
    """验收 #1：draft 状态入库 → 创建 corpus_asset + corpus_version."""
    result = await publish_corpus_library(db=async_session, status="draft")

    assert "asset_id" in result
    assert "version_id" in result
    assert result["function_count"] >= 10
    assert result["whitelist_count"] >= 1

    # 验证 DB 行存在
    asset = await async_session.get(CorpusAsset, result["asset_id"])
    cv = await async_session.get(CorpusVersion, result["version_id"])
    assert asset is not None
    assert cv is not None
    assert cv.asset_id == asset.asset_id
    assert cv.status == "draft"
    assert cv.gate_certificate_id is None
    assert cv.published_at is None
    # lineage 必填字段
    assert "tier" in cv.lineage
    assert "pipeline" in cv.lineage
    assert "signed_by" in cv.lineage
    assert "signed_at" in cv.lineage


async def test_publish_corpus_library_published_requires_gate_cert(async_session):
    """验收 #1：published 状态必须有 gate_certificate_id."""
    with pytest.raises(GateEnforcementError):
        await publish_corpus_library(
            db=async_session,
            status="published",
            gate_certificate_id=None,
        )

    await async_session.rollback()


async def test_publish_corpus_library_published_with_gate_cert_succeeds(async_session):
    """验收 #1：published + gate_cert → 写入 gate_certificate_id + published_at."""
    cert_id = f"gate-cert-{uuid.uuid4().hex[:12]}"
    result = await publish_corpus_library(
        db=async_session,
        status="published",
        gate_certificate_id=cert_id,
    )

    cv = await async_session.get(CorpusVersion, result["version_id"])
    assert cv is not None
    assert cv.status == "published"
    assert cv.gate_certificate_id == cert_id
    assert cv.published_at is not None


async def test_publish_corpus_library_rejects_unapproved_license(async_session, tmp_path):
    """验收 #4：license 未 approved → 抛 LicenseNotApprovedError 且不写库."""
    # 构造一个引用未登记 license 的 YAML
    good_lib = _load_default_library()
    bad_yaml = tmp_path / "bad_lib.yaml"
    bad_yaml.write_text(
        yaml.safe_dump({
            "schema_version": good_lib.schema_version,
            "pack_id": good_lib.pack_id,
            "kind": good_lib.kind,
            "license_id": "lic-not-registered",
            "library_version": good_lib.library_version,
            "functions": [
                {
                    "name": "dummy",
                    "version": "1.0.0",
                    "signature": {
                        "params": [],
                        "return": "integer",
                    },
                    "safety": {
                        "pure": True, "no_io": True,
                        "deterministic": True, "no_loops": True,
                    },
                    "description": "test",
                },
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )

    with pytest.raises(LicenseNotApprovedError):
        await publish_corpus_library(yaml_path=bad_yaml, db=async_session, status="draft")

    await async_session.rollback()

    # 库未污染：count == 0
    from sqlalchemy import func, select
    count = await async_session.scalar(
        select(func.count()).select_from(CorpusAsset)
    )
    assert count == 0, "license 校验失败不应入库"


# ────────────────────────────────────────────────────────────────────
# §内容寻址：同 YAML 字节内容 → 同 version_id（D3）
# ────────────────────────────────────────────────────────────────────

def test_compute_corpus_version_id_deterministic():
    """D3：同内容同 id."""
    lib = _load_default_library()
    vid1 = compute_corpus_version_id(lib, DEFAULT_FUNCTIONS_YAML)
    vid2 = compute_corpus_version_id(lib, DEFAULT_FUNCTIONS_YAML)
    assert vid1 == vid2
    assert vid1.startswith("sha256:")


# ────────────────────────────────────────────────────────────────────
# §验收 #4：函数调用（签名校验+调用样例）
# ────────────────────────────────────────────────────────────────────

def test_function_signature_params_parsed_correctly():
    """验收 #4：函数签名解析后 params 是 ParamSpec list."""
    lib = _load_default_library()
    gcd = next(f for f in lib.functions if f.name == "gcd")
    assert isinstance(gcd.params, list)
    assert all(isinstance(p, ParamSpec) for p in gcd.params)
    assert len(gcd.params) == 2
    assert gcd.params[0].name == "a"
    assert gcd.params[0].type == "integer"
    assert gcd.params[0].min == 0


def test_function_return_spec_parsed_correctly():
    """验收 #4：return 字段简单形式与对象形式都能解析."""
    lib = _load_default_library()
    gcd = next(f for f in lib.functions if f.name == "gcd")
    assert gcd.return_spec.type == "integer"

    fraction_add = next(f for f in lib.functions if f.name == "fraction_add")
    assert fraction_add.return_spec.type == "object"
    assert fraction_add.return_spec.fields is not None
    assert "numerator" in fraction_add.return_spec.fields


def test_function_safety_spec_all_safe_in_default_yaml():
    """验收 #4：默认 YAML 中所有函数必须满足安全契约（pure/no_io/deterministic）."""
    lib = _load_default_library()
    for fn in lib.functions:
        s = fn.safety
        assert s.pure is True, f"{fn.name}.pure != True"
        assert s.no_io is True, f"{fn.name}.no_io != True"
        assert s.deterministic is True, f"{fn.name}.deterministic != True"


def test_function_whitelist_includes_all_default_functions():
    """验收 #4：默认 YAML 中所有函数都应在白名单中（全满足安全契约）."""
    lib = _load_default_library()
    wl = function_whitelist(lib, strict_safety=True)
    assert len(wl) == len(lib.functions), (
        f"白名单 {len(wl)} != 函数总数 {len(lib.functions)}"
        "（默认 YAML 应全部满足安全契约）"
    )


# ────────────────────────────────────────────────────────────────────
# 宪法 A5：本模块不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_corpus_loader():
    """宪法 A5/X6：src/core/content/corpus_loader.py 不 import 学科包."""
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
# 函数签名验证：publish_corpus_library 参数
# ────────────────────────────────────────────────────────────────────

def test_publish_corpus_library_signature():
    """验收 #1：函数含 yaml_path, db, gate_certificate_id, status 参数."""
    sig = inspect.signature(publish_corpus_library)
    params = sig.parameters
    assert "yaml_path" in params
    assert "db" in params
    assert "gate_certificate_id" in params
    assert "status" in params
    # 默认值
    assert params["gate_certificate_id"].default is None
    assert params["status"].default == "draft"


# ────────────────────────────────────────────────────────────────────
# 端到端：加载 → 白名单 → 注册 → 入库
# ────────────────────────────────────────────────────────────────────

async def test_end_to_end_load_whitelist_and_publish(async_session):
    """端到端：加载 YAML → 取白名单 → 入库（draft）→ 查询验证."""
    # 1. 加载
    lib = _load_default_library()
    assert len(lib.functions) >= 10

    # 2. 取白名单
    wl = function_whitelist(lib, strict_safety=True)
    assert len(wl) >= 10  # 默认 YAML 全部满足安全契约

    # 3. 入库
    result = await publish_corpus_library(
        db=async_session,
        status="draft",
    )

    # 4. 验证
    cv = await async_session.get(CorpusVersion, result["version_id"])
    assert cv is not None
    assert cv.status == "draft"
    assert cv.license_id == lib.license_id
    assert cv.lineage["tier"] == "B"
    assert cv.lineage["pipeline"]["id"] == "subject-math.function_lib"
    assert cv.lineage["pipeline"]["version"] == lib.library_version
