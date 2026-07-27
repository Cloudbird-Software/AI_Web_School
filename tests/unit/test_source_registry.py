"""T-W2-015 内容来源登记表 + CI 拦截 单元测试.

对照 T-W2-015 任务卡验收标准：
1. content/sources/registry.yaml 包含 ≥5 条来源记录，字段完整。
2. SourceRegistry 提供 get_license / is_approved / all_approved。
3. CI 脚本 tools/ci/check_sources.py 发现未登记或过期来源时非零退出。
4. 单元测试覆盖 approved/rejected/expired/缺失 四种情况。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.core.content.source_registry import (
    DEFAULT_REGISTRY_PATH,
    SourceRecord,
    SourceRegistry,
)


# ────────────────────────────────────────────────────────────────────
# §1 文件结构验收（验收 #1）
# ────────────────────────────────────────────────────────────────────

def test_registry_file_exists_at_default_path():
    """验收 #1：默认路径存在 registry.yaml."""
    assert DEFAULT_REGISTRY_PATH.is_file(), f"registry.yaml 不存在: {DEFAULT_REGISTRY_PATH}"


def test_registry_has_at_least_5_records():
    """验收 #1：≥5 条来源记录."""
    reg = SourceRegistry.from_yaml()
    assert len(reg) >= 5, f"记录数 {len(reg)} < 5"


def test_registry_records_have_complete_fields():
    """验收 #1：字段完整（license_id/source/rights_holder/scope/expires_at/decision）."""
    reg = SourceRegistry.from_yaml()
    for rec in reg.all_records():
        assert rec.license_id, f"license_id 为空: {rec}"
        assert rec.source, f"source 为空: {rec}"
        # rights_holder/scope/expires_at 允许 null，但必须存在该属性
        assert hasattr(rec, "rights_holder")
        assert hasattr(rec, "scope")
        assert hasattr(rec, "expires_at")
        assert rec.decision in ("approved", "rejected", "expired"), \
            f"decision 非法: {rec.decision}"


def test_registry_no_duplicate_license_ids():
    """YAML 内无重复 license_id."""
    raw = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    ids = [r["license_id"] for r in raw["records"]]
    duplicates = {lid for lid in ids if ids.count(lid) > 1}
    assert not duplicates, f"重复 license_id: {duplicates}"


def test_registry_covers_all_decision_types():
    """覆盖性：approved/rejected/expired 三种 decision 都有样本."""
    reg = SourceRegistry.from_yaml()
    decisions = {r.decision for r in reg.all_records()}
    assert "approved" in decisions, "缺少 approved 样本"
    assert "rejected" in decisions, "缺少 rejected 样本"
    # expired 可能是 decision=expired 或 decision=approved+expires_at 过期
    # 验收 #4 要求 expired 情况——任一形式即可
    assert "expired" in decisions or any(
        r.decision == "approved" and r.expires_at is not None
        and r.expires_at.year < 2026
        for r in reg.all_records()
    ), "缺少 expired 样本"


# ────────────────────────────────────────────────────────────────────
# §2 SourceRegistry 查询接口（验收 #2）
# ────────────────────────────────────────────────────────────────────

def test_get_license_returns_record_for_known_id():
    """get_license 命中已知 license_id."""
    reg = SourceRegistry.from_yaml()
    # 取第一条记录的 license_id
    first = next(iter(reg.all_records()))
    rec = reg.get_license(first.license_id)
    assert rec is not None
    assert rec.license_id == first.license_id


def test_get_license_returns_none_for_unknown_id():
    """get_license 未登记返回 None."""
    reg = SourceRegistry.from_yaml()
    assert reg.get_license("lic-does-not-exist-xyz") is None


def test_is_approved_true_for_approved_non_expired():
    """验收 #4 approved 情况：decision=approved 且未过期 → True."""
    reg = SourceRegistry.from_yaml()
    # 找一条 approved 且 expires_at=null 的记录
    approved_record = next(
        r for r in reg.all_records()
        if r.decision == "approved" and r.expires_at is None
    )
    assert reg.is_approved(approved_record.license_id) is True


def test_is_approved_false_for_rejected():
    """验收 #4 rejected 情况：decision=rejected → False."""
    reg = SourceRegistry.from_yaml()
    rejected_record = next(r for r in reg.all_records() if r.decision == "rejected")
    assert reg.is_approved(rejected_record.license_id) is False


def test_is_approved_false_for_expired_decision():
    """验收 #4 expired 情况：decision=expired → False."""
    reg = SourceRegistry.from_yaml()
    expired_records = [r for r in reg.all_records() if r.decision == "expired"]
    if expired_records:
        rec = expired_records[0]
        assert reg.is_approved(rec.license_id) is False
    else:
        # 退化：找 decision=approved 但 expires_at 已过期的
        past = datetime.now(timezone.utc) - timedelta(days=1)
        rec = next(
            r for r in reg.all_records()
            if r.decision == "approved" and r.expires_at is not None
            and r.expires_at < past
        )
        assert reg.is_approved(rec.license_id) is False


def test_is_approved_false_for_missing_license():
    """验收 #4 缺失情况：未登记 → False."""
    reg = SourceRegistry.from_yaml()
    assert reg.is_approved("lic-not-registered-aaa") is False


def test_all_approved_returns_only_approved_records():
    """all_approved 仅返回 is_approved=True 的记录."""
    reg = SourceRegistry.from_yaml()
    approved = reg.all_approved()
    assert len(approved) >= 1
    for rec in approved:
        assert reg.is_approved(rec.license_id) is True


def test_contains_operator_works():
    """__contains__ 支持 `lid in reg`."""
    reg = SourceRegistry.from_yaml()
    first = next(iter(reg.all_records()))
    assert first.license_id in reg
    assert "lic-not-registered-zzz" not in reg


# ────────────────────────────────────────────────────────────────────
# §3 自定义 registry 测试（不依赖默认 YAML）
# ────────────────────────────────────────────────────────────────────

def _make_registry_yaml(tmp_path: Path, records: list[dict]) -> Path:
    """生成临时 registry.yaml."""
    p = tmp_path / "registry.yaml"
    p.write_text(
        yaml.safe_dump({"version": "1.0", "records": records}, allow_unicode=True),
        encoding="utf-8",
    )
    return p


def test_is_approved_false_when_expires_at_in_past(tmp_path):
    """decision=approved 但 expires_at 在过去 → False."""
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    p = _make_registry_yaml(tmp_path, [
        {"license_id": "lic-past", "source": "S", "decision": "approved",
         "expires_at": past},
    ])
    reg = SourceRegistry.from_yaml(p)
    assert reg.is_approved("lic-past") is False


def test_is_approved_true_when_expires_at_in_future(tmp_path):
    """decision=approved 且 expires_at 在未来 → True."""
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    p = _make_registry_yaml(tmp_path, [
        {"license_id": "lic-future", "source": "S", "decision": "approved",
         "expires_at": future},
    ])
    reg = SourceRegistry.from_yaml(p)
    assert reg.is_approved("lic-future") is True


def test_load_raises_on_duplicate_license_id(tmp_path):
    """重复 license_id 抛 ValueError."""
    p = _make_registry_yaml(tmp_path, [
        {"license_id": "lic-dup", "source": "S1", "decision": "approved"},
        {"license_id": "lic-dup", "source": "S2", "decision": "approved"},
    ])
    with pytest.raises(ValueError, match="重复"):
        SourceRegistry.from_yaml(p)


def test_load_raises_on_missing_file(tmp_path):
    """文件不存在抛 FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        SourceRegistry.from_yaml(tmp_path / "no-such.yaml")


def test_load_raises_on_invalid_decision(tmp_path):
    """decision 非法值抛 ValidationError."""
    p = _make_registry_yaml(tmp_path, [
        {"license_id": "lic-x", "source": "S", "decision": "invalid"},
    ])
    with pytest.raises(Exception):  # pydantic.ValidationError
        SourceRegistry.from_yaml(p)


def test_load_raises_on_empty_license_id(tmp_path):
    """license_id 空字符串抛 ValidationError."""
    p = _make_registry_yaml(tmp_path, [
        {"license_id": "", "source": "S", "decision": "approved"},
    ])
    with pytest.raises(Exception):
        SourceRegistry.from_yaml(p)


# ────────────────────────────────────────────────────────────────────
# §4 CI 拦截脚本验收（验收 #3）
# ────────────────────────────────────────────────────────────────────

def _load_check_module():
    """动态加载 tools/ci/check_sources.py 作为模块（tools/ 不在 src/）.

    为什么先注册到 sys.modules：Python 3.12 的 @dataclass 装饰器内部会
    调用 sys.modules.get(cls.__module__).__dict__；若模块未注册则 NoneType 报错。
    """
    script = Path(__file__).resolve().parents[2] / "tools" / "ci" / "check_sources.py"
    spec = importlib.util.spec_from_file_location("check_sources", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_sources"] = mod  # 必须先注册，否则 @dataclass 报错
    spec.loader.exec_module(mod)
    return mod


def test_check_sources_returns_0_when_all_approved(tmp_path, monkeypatch):
    """验收 #3：所有 license_id 已登记且 approved → 退出 0."""
    mod = _load_check_module()

    # 构造一个干净的 content_dir
    content = tmp_path / "content"
    content.mkdir()
    # registry.yaml
    (content / "sources").mkdir()
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {"license_id": "lic-good", "source": "S", "decision": "approved"},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    # 一个引用 license_id 的内容文件
    (content / "data.yaml").write_text(
        "kind: wordlist\nlicense_id: lic-good\n",
        encoding="utf-8",
    )

    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 0, f"应退出 0，实际 {rc}"


def test_check_sources_returns_1_on_unregistered_license(tmp_path):
    """验收 #3：未登记 license_id → 退出 1."""
    mod = _load_check_module()

    content = tmp_path / "content"
    content.mkdir()
    (content / "sources").mkdir()
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {"license_id": "lic-good", "source": "S", "decision": "approved"},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    # 引用未登记的 license_id
    (content / "bad.yaml").write_text(
        "kind: wordlist\nlicense_id: lic-unknown\n",
        encoding="utf-8",
    )

    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 1, f"应退出 1，实际 {rc}"


def test_check_sources_returns_1_on_rejected_license(tmp_path):
    """验收 #3：decision!=approved → 退出 1."""
    mod = _load_check_module()

    content = tmp_path / "content"
    content.mkdir()
    (content / "sources").mkdir()
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {"license_id": "lic-bad", "source": "S", "decision": "rejected"},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    (content / "bad.yaml").write_text(
        "kind: wordlist\nlicense_id: lic-bad\n",
        encoding="utf-8",
    )

    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 1, f"应退出 1，实际 {rc}"


def test_check_sources_returns_1_on_expired_license(tmp_path):
    """验收 #3：已过期（decision=approved+expires_at 过期） → 退出 1."""
    mod = _load_check_module()
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    content = tmp_path / "content"
    content.mkdir()
    (content / "sources").mkdir()
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {"license_id": "lic-exp", "source": "S", "decision": "approved",
                 "expires_at": past},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    (content / "data.yaml").write_text(
        "kind: wordlist\nlicense_id: lic-exp\n",
        encoding="utf-8",
    )

    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 1, f"应退出 1，实际 {rc}"


def test_check_sources_returns_2_on_invalid_registry(tmp_path):
    """验收 #3：registry schema 校验失败 → 退出 2."""
    mod = _load_check_module()

    content = tmp_path / "content"
    content.mkdir()
    (content / "sources").mkdir()
    # registry 缺 version 字段
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({"records": []}, allow_unicode=True),
        encoding="utf-8",
    )

    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 2, f"应退出 2，实际 {rc}"


def test_check_sources_skips_registry_yaml_itself(tmp_path):
    """扫描时跳过 registry.yaml 自身（避免自指）."""
    mod = _load_check_module()

    content = tmp_path / "content"
    content.mkdir()
    (content / "sources").mkdir()
    (content / "sources" / "registry.yaml").write_text(
        yaml.safe_dump({
            "version": "1.0",
            "records": [
                {"license_id": "lic-good", "source": "S", "decision": "approved"},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )
    # 无其他内容文件
    rc = mod.main(["--content-dir", str(content), "--registry", str(content / "sources" / "registry.yaml")])
    assert rc == 0, f"无引用时应退出 0，实际 {rc}"


def test_check_sources_default_args_use_project_paths():
    """默认参数扫描项目 content/ + 默认 registry.yaml."""
    mod = _load_check_module()
    # 项目 content/ 下不应有未登记 license_id（math_kp_3-4.yaml 不含 license_id）
    # 直接调用 main() 不传参数
    rc = mod.main([])
    # 项目当前 content/ 内只有 math_kp_3-4.yaml + sources/registry.yaml
    # math_kp_3-4.yaml 不含 license_id 字段 → 应退出 0
    assert rc == 0, f"项目当前 content/ 应通过 CI，实际 {rc}"
