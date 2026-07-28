"""T-W4-023 音频语速适龄校验器（架构 v2 §4.3 / §4.6）.

音频素材专用校验门之一：语速适龄性。按学段配置目标 wpm，实测 wpm 偏差需在
±10% 内（低段 120±12 / 中段 140±14 / 高段 160±16），超出范围 fail。
未过门音频不得入库（D2：由 certifier 在签发 publish 证书时强制）。

为什么独立于 AudioQualityValidator：语速适龄与发音正确是两个独立维度，分两个
验证器便于策略矩阵按需组合（如低段强制两者、高段仅语速）。两者均 blocking=True。

宪法 A5/X6：学段参数以常量表形式内置（L/M/H wpm），不 import 学段包；
学段包若要覆盖目标 wpm，可经 ctx 注入 target_wpm（扩展点，当前用默认表）。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

# ── 学段目标语速（与 voice_profiles.yaml 一致；本地常量避免 import TTS 配置）──
# 为什么本地常量而非读 voice_profiles.yaml：校验域不应依赖音频产线配置文件
# （解耦）；学段 wpm 是稳定契约，两处保持一致即可，漂移由测试守护。
GRADE_BAND_TARGET_WPM: dict[str, int] = {"L": 120, "M": 140, "H": 160}

# 偏差容忍度：±10%（低段 120±12 / 中段 140±14 / 高段 160±16）
TOLERANCE_PCT = 0.10


class AudioAgeCheckValidator(Validator):
    """音频语速适龄验证器（验收 #1/#2）.

    ctx.artifact_payload 期望字段：
    - grade_band: 'L'/'M'/'H'——决定目标 wpm。
    - wpm: int——实测语速（优先取此；缺省时回退 tts_metadata['wpm']）。
    - tts_metadata: dict（可选，回退取 wpm）。

    verdict 规则：
    - fail：grade_band 缺失/未知；wpm 缺失；偏差超出 ±10%。
    - pass：偏差在 ±10% 内。

    evidence：{grade_band, wpm_actual, wpm_target, deviation_pct,
              tolerance_pct, lower_bound, upper_bound}
    """

    validator_id = "audio_age_check"
    version = "1.0.0+audio"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        payload = ctx.artifact_payload or {}
        grade_band = payload.get("grade_band")
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        # 未知学段 → 无法判定目标语速，fail
        if grade_band not in GRADE_BAND_TARGET_WPM:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": f"未知 grade_band={grade_band!r}，无法判定目标语速",
                    "grade_band": grade_band,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        target = GRADE_BAND_TARGET_WPM[grade_band]
        # 实测 wpm：优先 payload['wpm']，回退 tts_metadata['wpm']
        wpm_actual: Any = payload.get("wpm")
        if wpm_actual is None:
            md = payload.get("tts_metadata") or {}
            wpm_actual = md.get("wpm")
        if wpm_actual is None:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "未提供实测 wpm（payload.wpm 或 tts_metadata.wpm 均缺失）",
                    "grade_band": grade_band,
                    "wpm_target": target,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        wpm_actual = int(wpm_actual)
        lower = target * (1 - TOLERANCE_PCT)
        upper = target * (1 + TOLERANCE_PCT)
        deviation_pct = (wpm_actual - target) / target * 100.0
        passed = lower <= wpm_actual <= upper

        return self._timed_result(
            verdict="pass" if passed else "fail",
            evidence={
                "grade_band": grade_band,
                "wpm_actual": wpm_actual,
                "wpm_target": target,
                "deviation_pct": round(deviation_pct, 2),
                "tolerance_pct": TOLERANCE_PCT * 100,
                "lower_bound": lower,
                "upper_bound": upper,
                "reason": (
                    None
                    if passed
                    else f"语速 {wpm_actual}wpm 超出 [{lower:.0f}, {upper:.0f}]（学段{grade_band}）"
                ),
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册（pack_id='platform'，与 SchemaValidator/LicenseValidator 同桶）
register_validator("platform", AudioAgeCheckValidator)


__all__ = ["AudioAgeCheckValidator", "GRADE_BAND_TARGET_WPM", "TOLERANCE_PCT"]
