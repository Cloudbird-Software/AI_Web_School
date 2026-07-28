"""T-W4-023 音频校验门单元测试.

验收对照：
  #1 音频质量验证器输出 pass/fail + 语速实测值/目标值/偏差百分比
  #2 语速适龄：L 120±12 / M 140±14 / H 160±16 wpm；超出范围 fail
  #3 发音正确性文本匹配占位（对比原始文本与元数据标记），可扩展 ASR 回译
  #4 make accept 全绿；含"未过门音频入库被 DB 层拒绝"断言
  #5 不 import 学科包/学段包

DB 层拒绝（验收 #4）走 certifier.issue_certificate：阻断 fail → CertificateIssuanceError
+ DB 断言无新证书（D2 门强制：未过门音频拿不到 publish 证书，不得入库）。
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.gate.certifier import CertificateIssuanceError, issue_certificate
from src.core.gate.validator import (
    GateContext,
    ValidatorResult,
    get_validator,
    list_validators,
    register_validator,
)
from src.core.gate.validators import (
    AudioAgeCheckValidator,
    AudioQualityValidator,
    GRADE_BAND_TARGET_WPM,
    TOLERANCE_PCT,
)
from src.core.gate.validators.audio_age_check import AudioAgeCheckValidator as _Age
from src.core.gate.validators.audio_quality import AudioQualityValidator as _Qual

# 触发音频验证器注册（import 即注册到 platform 桶）
import src.core.gate.validators  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_registered() -> None:
    """每个测试前重注册音频验证器.

    为什么需要：test_gate_validator_base.py 等测试的 reset_registry() 会清空整个
    注册表，导致本测试的 list_validators('platform') 不含 audio_age_check /
    audio_quality（模块已加载不会重跑模块级 register_validator）。与
    test_math_validator.py 的 _ensure_subject_math_validators_registered 同模式。
    """
    register_validator("platform", AudioAgeCheckValidator)
    register_validator("platform", AudioQualityValidator)
    yield


# ── 验收 #1：语速适龄验证器输出 pass/fail + 实测/目标/偏差 ────────


async def test_age_check_pass_returns_wpm_evidence() -> None:
    """pass 时 evidence 含 wpm_actual/wpm_target/deviation_pct."""
    v = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio",
        pack_id="platform",
        artifact_payload={"grade_band": "M", "wpm": 140},
    )
    r: ValidatorResult = await v.validate("audio:asset-1", ctx)
    assert r.verdict == "pass"
    ev = r.evidence
    assert ev["wpm_actual"] == 140
    assert ev["wpm_target"] == 140
    assert ev["deviation_pct"] == 0.0
    assert ev["grade_band"] == "M"
    assert r.validator_id == "audio_age_check"


async def test_age_check_fail_returns_deviation() -> None:
    """fail 时 evidence 含偏差百分比与边界."""
    v = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio",
        pack_id="platform",
        artifact_payload={"grade_band": "L", "wpm": 200},
    )
    r = await v.validate("audio:asset-2", ctx)
    assert r.verdict == "fail"
    ev = r.evidence
    assert ev["wpm_actual"] == 200
    assert ev["wpm_target"] == 120
    # 偏差 = (200-120)/120*100 ≈ 66.67
    assert ev["deviation_pct"] > 60
    assert ev["lower_bound"] == 108.0  # 120 * 0.9
    assert ev["upper_bound"] == 132.0  # 120 * 1.1


# ── 验收 #2：学段语速范围 ─────────────────────────────────────────


async def test_age_check_L_range() -> None:
    """L(1-2) 120±12wpm：108/120/132 pass，107/133 fail."""
    v = AudioAgeCheckValidator()
    for wpm, expected in [(108, "pass"), (120, "pass"), (132, "pass"),
                          (107, "fail"), (133, "fail")]:
        ctx = GateContext(
            artifact_type="audio", pack_id="platform",
            artifact_payload={"grade_band": "L", "wpm": wpm},
        )
        r = await v.validate("a", ctx)
        assert r.verdict == expected, f"L wpm={wpm} 期望 {expected} 实际 {r.verdict}"


async def test_age_check_M_range() -> None:
    """M(3-4) 140±14wpm：126/140/154 pass，125/155 fail."""
    v = AudioAgeCheckValidator()
    for wpm, expected in [(126, "pass"), (140, "pass"), (154, "pass"),
                          (125, "fail"), (155, "fail")]:
        ctx = GateContext(
            artifact_type="audio", pack_id="platform",
            artifact_payload={"grade_band": "M", "wpm": wpm},
        )
        r = await v.validate("a", ctx)
        assert r.verdict == expected, f"M wpm={wpm} 期望 {expected} 实际 {r.verdict}"


async def test_age_check_H_range() -> None:
    """H(5-6) 160±16wpm：144/160/176 pass，143/177 fail."""
    v = AudioAgeCheckValidator()
    for wpm, expected in [(144, "pass"), (160, "pass"), (176, "pass"),
                          (143, "fail"), (177, "fail")]:
        ctx = GateContext(
            artifact_type="audio", pack_id="platform",
            artifact_payload={"grade_band": "H", "wpm": wpm},
        )
        r = await v.validate("a", ctx)
        assert r.verdict == expected, f"H wpm={wpm} 期望 {expected} 实际 {r.verdict}"


async def test_age_check_wpm_from_tts_metadata() -> None:
    """未提供 payload.wpm 时回退 tts_metadata.wpm."""
    v = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={"grade_band": "M", "tts_metadata": {"wpm": 140}},
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "pass"
    assert r.evidence["wpm_actual"] == 140


async def test_age_check_unknown_grade_band_fails() -> None:
    """未知学段 → fail（无法判定目标语速）."""
    v = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={"grade_band": "X", "wpm": 100},
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "fail"


async def test_age_check_missing_wpm_fails() -> None:
    """wpm 缺失 → fail."""
    v = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={"grade_band": "M"},
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "fail"
    assert "wpm" in r.evidence["reason"]


def test_grade_band_target_wpm_constants() -> None:
    """学段目标语速常量与 voice_profiles.yaml 一致."""
    assert GRADE_BAND_TARGET_WPM == {"L": 120, "M": 140, "H": 160}
    assert TOLERANCE_PCT == 0.10


# ── 验收 #3：发音正确性（文本匹配占位 + ASR 扩展点）──────────────


async def test_quality_placeholder_pass() -> None:
    """占位模式：文本与 tts_metadata.text_length 一致 → pass."""
    v = AudioQualityValidator()
    text = "一段听力素材文本"
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={
            "text": text,
            "tts_metadata": {"text_length": len(text)},
        },
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "pass"
    assert r.evidence["method"] == "text_length_placeholder"
    assert r.evidence["normalized_match"] is True


async def test_quality_placeholder_text_length_mismatch_fails() -> None:
    """占位模式：text_length 不一致 → fail（文本与合成参数不匹配）."""
    v = AudioQualityValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={
            "text": "abc",
            "tts_metadata": {"text_length": 99},  # 不一致
        },
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "fail"
    assert r.evidence["method"] == "text_length_placeholder"


async def test_quality_empty_text_fails() -> None:
    """原始文本缺失/空 → fail."""
    v = AudioQualityValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={"text": "", "tts_metadata": {"text_length": 0}},
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "fail"


async def test_quality_asr_transcript_match_passes() -> None:
    """ASR 扩展点：回译与原文规范化后一致 → pass."""
    v = AudioQualityValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={
            "text": "Hello, World!",
            "asr_transcript": "hello world",  # 去标点空白后一致
        },
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "pass"
    assert r.evidence["method"] == "asr_transcript"
    assert r.evidence["normalized_match"] is True


async def test_quality_asr_transcript_mismatch_fails() -> None:
    """ASR 扩展点：回译与原文不一致 → fail（发音可能错误）."""
    v = AudioQualityValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={
            "text": "The cat is on the mat",
            "asr_transcript": "the dog is under the table",
        },
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "fail"
    assert r.evidence["method"] == "asr_transcript"


async def test_quality_asr_takes_precedence_over_placeholder() -> None:
    """提供 asr_transcript 时走 ASR 路径（即使 text_length 不一致也以 ASR 为准）."""
    v = AudioQualityValidator()
    ctx = GateContext(
        artifact_type="audio", pack_id="platform",
        artifact_payload={
            "text": "same text",
            "tts_metadata": {"text_length": 999},  # 不一致
            "asr_transcript": "same text",  # ASR 一致
        },
    )
    r = await v.validate("a", ctx)
    assert r.verdict == "pass"
    assert r.evidence["method"] == "asr_transcript"


# ── 注册表 ────────────────────────────────────────────────────────


def test_audio_validators_registered() -> None:
    """两个音频验证器注册到 platform 桶."""
    registered = set(list_validators("platform"))
    assert "audio_age_check" in registered
    assert "audio_quality" in registered


def test_get_validator_returns_instances() -> None:
    """get_validator 可取到音频验证器实例."""
    assert isinstance(get_validator("platform", "audio_age_check"), AudioAgeCheckValidator)
    assert isinstance(get_validator("platform", "audio_quality"), AudioQualityValidator)


# ── 验收 #5：不 import 学科包/学段包 ──────────────────────────────


def test_no_subject_pack_imports_in_validators() -> None:
    """src/core/gate/validators/ 禁止 import 学科包/学段包（A5/X6）."""
    vdir = (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "core" / "gate" / "validators"
    )
    assert vdir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(vdir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(vdir)))
    assert not violations, f"gate/validators 存在学科包 import（违反 A5）：{violations}"


# ════════════════════════════════════════════════════════════════════
# 验收 #4：未过门音频入库被 DB 层拒绝（D2 门强制）
# ════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def _clean_gate_db(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """DB 测试前清空 gate 三表 + 插入 cert:none 占位行（与 test_gate_bypass 同模式）.

    为什么独立连接真正提交 TRUNCATE：释放 ACCESS EXCLUSIVE 锁，避免与后续
    issue_certificate 的写入冲突（详见 test_gate_bypass._truncate_gate_tables 注释）。
    """
    async with async_engine.connect() as conn:
        tran = await conn.begin()
        try:
            await conn.execute(
                text(
                    "TRUNCATE TABLE gate_verdict, gate_run, gate_certificate"
                    " RESTART IDENTITY CASCADE"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
                    " policy_version, issued_by)"
                    " VALUES ('cert:none', 'placeholder', 'publish', 'no-policy', 'system')"
                )
            )
            await tran.commit()
        except Exception:
            await tran.rollback()
            raise
    yield


async def test_ungated_audio_rejected_at_db_layer(
    async_session: AsyncSession, _clean_gate_db: None
) -> None:
    """验收 #4：未过门音频（语速超范围）→ issue_certificate 拒绝签发 + DB 无新证书.

    场景：低段音频 wpm=200（远超 [108,132]），AudioAgeCheckValidator fail。
    调用 issue_certificate 签发 publish 证书 → CertificateIssuanceError，
    且 DB 中无该音频的 gate_certificate / gate_run（入库被拒）。
    """
    age_validator = AudioAgeCheckValidator()
    ctx = GateContext(
        artifact_type="audio",
        pack_id="platform",
        artifact_payload={"grade_band": "L", "wpm": 200},  # 超出低段范围
    )
    fail_result: ValidatorResult = await age_validator.validate(
        "audio:ungated-asset", ctx
    )
    assert fail_result.verdict == "fail", "前置：wpm=200 低段应 fail"

    # 签发 publish 证书 → 阻断 fail 必须拒绝
    with pytest.raises(CertificateIssuanceError, match="audio_age_check"):
        await issue_certificate(
            artifact_ref="audio:ungated-asset",
            cert_type="publish",
            policy_version="audio-gate-v1",
            issued_by="audio-producer",
            runs=[(fail_result, True)],  # blocking=True
            db=async_session,
        )

    # DB 层断言：未签发任何新证书（仅 cert:none 占位），无 gate_run 落库
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 1, "未过门音频不应有 publish 证书"

    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run")
    )
    assert run_count == 0, "未过门音频不应落 gate_run"

    # 该音频 artifact_ref 无证书
    asset_cert = await async_session.scalar(
        text(
            "SELECT count(*) FROM gate_certificate"
            " WHERE artifact_ref = 'audio:ungated-asset'"
        )
    )
    assert asset_cert == 0


async def test_gated_audio_passes_db_layer(
    async_session: AsyncSession, _clean_gate_db: None
) -> None:
    """对照：过门音频（语速合规 + 发音占位通过）→ 证书签发成功，DB 有记录."""
    age_v = AudioAgeCheckValidator()
    qual_v = AudioQualityValidator()
    text_str = "合规听力文本"
    payload = {
        "grade_band": "M",
        "wpm": 140,
        "text": text_str,
        "tts_metadata": {"text_length": len(text_str), "wpm": 140},
    }
    ctx = GateContext(
        artifact_type="audio", pack_id="platform", artifact_payload=payload
    )
    age_r = await age_v.validate("audio:gated-asset", ctx)
    qual_r = await qual_v.validate("audio:gated-asset", ctx)
    assert age_r.verdict == "pass"
    assert qual_r.verdict == "pass"

    cert_id = await issue_certificate(
        artifact_ref="audio:gated-asset",
        cert_type="publish",
        policy_version="audio-gate-v1",
        issued_by="audio-producer",
        runs=[(age_r, True), (qual_r, True)],
        db=async_session,
    )
    assert cert_id.startswith("cert_")

    # DB 有新证书 + 2 条 gate_run
    asset_cert = await async_session.scalar(
        text(
            "SELECT count(*) FROM gate_certificate"
            " WHERE artifact_ref = 'audio:gated-asset'"
        )
    )
    assert asset_cert == 1
    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run WHERE certificate_id = :cid"),
        {"cid": cert_id},
    )
    assert run_count == 2
