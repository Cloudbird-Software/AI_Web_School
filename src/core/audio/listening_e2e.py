"""T-W4-026 英语听力端到端流水线：TTS → 音频门 → 组卷 overlay → 渲染（架构 v2 §4.6 / S5）.

完整链路（E2E-4 承载卡）：
1. TTS 产线合成音频（T-W4-022 produce_audio）。
2. 音频校验门（T-W4-023 AudioAgeCheck + AudioQuality）——未过门阻断组卷。
3. 听力组卷 overlay（T-W4-025 apply_listening_overlay）——占比 30–40%、置卷首。
4. 渲染产物（T-W4-024）：静态卷=二维码 SVG，在线卷=播放器 URL。

为什么 pipeline 不调 issue_certificate（DB 层签发）：E2E 测试需 hermetic（无 DB 依赖），
gate 阻断由验证器 verdict 决定（pass/fail），证书签发是 DB 层兜底（T-W4-023 已测）。
pipeline 聚焦「验证器链 → 阻断/放行 → 组卷 → 渲染」的逻辑闭环。

宪法 A5/X6：不 import 学科包/学段包——pipeline 是核心域通用流水线，
「英语」语义由调用方传入 texts/grade_band 决定，本模块不感知学科。
宪法 D2：未过门音频阻断组卷——gate fail → ListeningGateError，不静默放行。
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.audio.player_service import InMemoryPlayCountStore, PlayResult, play
from src.core.audio.point_read import PointReadResult, point_read
from src.core.audio.producer import AudioAsset, produce_audio
from src.core.audio.qr_generator import QRSignedUrl, generate_qr
from src.core.assembly.listening_overlay import (
    ListeningOverlay,
    ListeningOverlayResult,
    ListeningOverlaySpec,
    apply_listening_overlay,
)
from src.core.assembly.profile import AssemblyProfile, compile_profile
from src.core.gate.validator import (
    GateContext,
    ValidatorResult,
    register_validator,
)
from src.core.gate.validators import (
    AudioAgeCheckValidator,
    AudioQualityValidator,
)


# ════════════════════════════════════════════════════════════════════
# 异常
# ════════════════════════════════════════════════════════════════════


class ListeningGateError(Exception):
    """音频未过门，阻断组卷（D2：未过门产物不得入库/组卷）."""

    def __init__(
        self, audio_id: str, validator_id: str, verdict: str, evidence: dict[str, Any]
    ) -> None:
        self.audio_id = audio_id
        self.validator_id = validator_id
        self.verdict = verdict
        self.evidence = evidence
        super().__init__(
            f"音频 {audio_id!r} 未过门：{validator_id} verdict={verdict}"
        )


# ════════════════════════════════════════════════════════════════════
# 流水线数据模型
# ════════════════════════════════════════════════════════════════════


class ItemSpec(BaseModel):
    """听力题规格（pipeline 输入）.

    - item_version_id: 题版本 id（D3 内容寻址）。
    - text: 待合成文本（应已剥离 PII，D7）。
    - voice_profile: 音色名（None → 学段默认）。
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    text: str = Field(min_length=1)
    voice_profile: Optional[str] = None


class GateValidationResult(BaseModel):
    """单个音频过门结果."""

    model_config = ConfigDict(extra="forbid")

    audio_id: str
    age_check: ValidatorResult
    quality_check: ValidatorResult
    passed: bool


class ListeningPaperItem(BaseModel):
    """听力卷单题（含音频绑定 + 过门证据）."""

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    audio: AudioAsset
    gate: GateValidationResult


class RenderArtifact(BaseModel):
    """渲染产物：QR 码（静态卷）或播放器 URL（在线卷）."""

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    audio_id: str
    artifact_type: Literal["qr_code", "player_url"]
    qr_svg: Optional[str] = None
    signed_url: Optional[str] = None
    player_url: Optional[str] = None
    play_count: Optional[int] = None


class ListeningPipelineResult(BaseModel):
    """听力端到端流水线结果（验收 #1）.

    - audio_assets: TTS 产出的全部音频素材。
    - gate_results: 每个音频的过门验证结果。
    - paper_items: 绑定音频的听力题（已过门）。
    - overlay: 听力组卷 overlay（占比/位置/testlet）。
    - render_artifacts: 渲染产物（QR/player）。
    - render_mode: 渲染模式（static/online）。
    - pipeline_digest: 端到端可复现指纹。
    """

    model_config = ConfigDict(extra="forbid")

    audio_assets: list[AudioAsset]
    gate_results: list[GateValidationResult]
    paper_items: list[ListeningPaperItem]
    overlay: ListeningOverlay
    render_artifacts: list[RenderArtifact]
    render_mode: Literal["static", "online"]
    pipeline_digest: str


# ════════════════════════════════════════════════════════════════════
# 内部：音频过门
# ════════════════════════════════════════════════════════════════════

# 确保音频验证器已注册（其他测试可能 reset_registry）
_age_validator = AudioAgeCheckValidator()
_quality_validator = AudioQualityValidator()
register_validator("platform", AudioAgeCheckValidator)
register_validator("platform", AudioQualityValidator)


async def _validate_audio_gate(
    asset: AudioAsset,
) -> GateValidationResult:
    """对单个音频素材执行过门校验（语速适龄 + 发音正确）.

    两个 blocking 验证器都 pass → passed=True。
    任一 fail → passed=False（调用方决定是否阻断）。
    """
    ctx = GateContext(
        artifact_type="audio",
        pack_id="platform",
        artifact_payload={
            "grade_band": asset.grade_band,
            "wpm": asset.tts_metadata.get("wpm"),
            "tts_metadata": asset.tts_metadata,
            "text": asset.text,
        },
    )
    age_result = await _age_validator.validate(asset.audio_id, ctx)
    quality_result = await _quality_validator.validate(asset.audio_id, ctx)
    passed = age_result.verdict == "pass" and quality_result.verdict == "pass"
    return GateValidationResult(
        audio_id=asset.audio_id,
        age_check=age_result,
        quality_check=quality_result,
        passed=passed,
    )


# ════════════════════════════════════════════════════════════════════
# 内部：渲染产物生成
# ════════════════════════════════════════════════════════════════════


def _generate_render_artifacts(
    paper_items: list[ListeningPaperItem],
    *,
    render_mode: Literal["static", "online"],
    qr_secret: str,
    paper_id: str,
    play_store: Optional[InMemoryPlayCountStore] = None,
) -> list[RenderArtifact]:
    """为每道听力题生成渲染产物.

    - static: QR 码 SVG（含签名 URL，24h 有效）。
    - online: 播放器 URL（首次 play 返回的 URL + play_count=1）。
    """
    artifacts: list[RenderArtifact] = []
    store = play_store if play_store is not None else InMemoryPlayCountStore()

    for item in paper_items:
        audio = item.audio
        if render_mode == "static":
            qr: QRSignedUrl = generate_qr(
                audio.audio_id, paper_id, secret=qr_secret
            )
            artifacts.append(
                RenderArtifact(
                    item_version_id=item.item_version_id,
                    audio_id=audio.audio_id,
                    artifact_type="qr_code",
                    qr_svg=qr.qr_svg,
                    signed_url=qr.signed_url,
                )
            )
        else:  # online
            # 首次播放获取 player URL（play_count=1）
            result: PlayResult = play(
                audio.audio_id,
                session_id=f"e2e-{paper_id}",
                audio_url=audio.url,
                store=store,
            )
            artifacts.append(
                RenderArtifact(
                    item_version_id=item.item_version_id,
                    audio_id=audio.audio_id,
                    artifact_type="player_url",
                    player_url=result.url,
                    play_count=result.play_count,
                )
            )
    return artifacts


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════


async def run_listening_pipeline(
    item_specs: list[ItemSpec],
    *,
    grade_band: Literal["L", "M", "H"],
    total_items: int,
    render_mode: Literal["static", "online"] = "static",
    qr_secret: str = "e2e-default-secret",
    paper_id: str = "paper-e2e",
    audio_context_ref: Optional[str] = None,
) -> ListeningPipelineResult:
    """英语听力端到端流水线（验收 #1/#2/#3）.

    流程：
    1. TTS 合成：对每个 item_spec 产出 AudioAsset（produce_audio）。
    2. 音频过门：AudioAgeCheck + AudioQuality（blocking）。
       未过门 → 抛 ListeningGateError（阻断组卷，D2）。
    3. 组卷 overlay：apply_listening_overlay（占比 30–40%、置卷首）。
       不可行 → 抛 ValueError（overlay 冲突原因）。
    4. 渲染产物：static=QR 码 SVG，online=播放器 URL。
    5. 返回 ListeningPipelineResult（含音频/过门/题/overlay/渲染产物/digest）。

    为什么是 async：验证器 validate 是 async（DB 验证器需 AsyncSession；
    音频验证器虽同步但保持契约一致）。

    Args:
        item_specs: 听力题规格列表（item_version_id + text + voice_profile）。
        grade_band: 学段 L/M/H（决定语速与默认音色）。
        total_items: 卷面总题量（含非听力题；用于计算听力占比）。
        render_mode: 渲染模式（'static'=QR 码 / 'online'=播放器 URL）。
        qr_secret: QR 签名密钥（静态卷用）。
        paper_id: 卷 id（QR 签名绑定 + player session）。
        audio_context_ref: 共享音频上下文引用（None → 用 paper_id）。

    Returns:
        ListeningPipelineResult：完整卷对象。

    Raises:
        ListeningGateError: 音频未过门（阻断组卷）。
        ValueError: overlay 不可行（听力素材不足或占比冲突）。
    """
    if not item_specs:
        raise ValueError("item_specs 不能为空")

    ctx_ref = audio_context_ref or f"audio-bundle:{paper_id}"

    # ── 1. TTS 合成 ──
    audio_assets: list[AudioAsset] = []
    for spec in item_specs:
        asset = produce_audio(
            text=spec.text,
            voice_profile=spec.voice_profile,
            grade_band=grade_band,
        )
        audio_assets.append(asset)

    # ── 2. 音频过门 ──
    gate_results: list[GateValidationResult] = []
    for asset in audio_assets:
        gate = await _validate_audio_gate(asset)
        if not gate.passed:
            # 未过门阻断（D2）：找出失败的验证器
            failed = gate.age_check if gate.age_check.verdict != "pass" else gate.quality_check
            raise ListeningGateError(
                audio_id=asset.audio_id,
                validator_id=failed.validator_id,
                verdict=failed.verdict,
                evidence=failed.evidence,
            )
        gate_results.append(gate)

    # ── 3. 组卷 overlay ──
    # 通过 base overlay 传入总题量（compile_profile 从 base.item_count_range 读取）
    paper_spec = compile_profile(
        profile_id=paper_id,
        profile_version="1.0.0",
        purpose="practice",
        gradeband=grade_band,
        kp_codes=["eng.listen"],
        base={"item_count_range": [total_items, total_items]},
    )
    overlay_spec = ListeningOverlaySpec(audio_context_ref=ctx_ref)
    overlay_result: ListeningOverlayResult = apply_listening_overlay(
        paper_spec,
        available_listening_items=len(audio_assets),
        spec=overlay_spec,
    )
    if not overlay_result.feasible:
        conflict_details = "; ".join(c.detail for c in overlay_result.conflicts)
        raise ValueError(f"听力 overlay 不可行：{conflict_details}")

    assert overlay_result.overlay is not None  # feasible=True 时非 None

    # ── 4. 绑定音频到题目 ──
    paper_items: list[ListeningPaperItem] = []
    for spec, asset, gate in zip(item_specs, audio_assets, gate_results):
        paper_items.append(
            ListeningPaperItem(
                item_version_id=spec.item_version_id,
                audio=asset,
                gate=gate,
            )
        )

    # ── 5. 渲染产物 ──
    render_artifacts = _generate_render_artifacts(
        paper_items,
        render_mode=render_mode,
        qr_secret=qr_secret,
        paper_id=paper_id,
    )

    # ── 6. 端到端 digest ──
    digest_payload = "|".join(
        f"{item.item_version_id}:{item.audio.audio_id}:{item.gate.passed}"
        for item in paper_items
    )
    pipeline_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()

    return ListeningPipelineResult(
        audio_assets=audio_assets,
        gate_results=gate_results,
        paper_items=paper_items,
        overlay=overlay_result.overlay,
        render_artifacts=render_artifacts,
        render_mode=render_mode,
        pipeline_digest=pipeline_digest,
    )


__all__ = [
    "ListeningGateError",
    "ItemSpec",
    "GateValidationResult",
    "ListeningPaperItem",
    "RenderArtifact",
    "ListeningPipelineResult",
    "run_listening_pipeline",
]
