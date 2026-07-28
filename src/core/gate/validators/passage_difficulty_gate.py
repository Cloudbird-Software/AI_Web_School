"""T-W4-014 语篇难度一致性验证器.

架构 v2 §4.3 / §4.1：语篇校验门第三道——难度一致性。
分析值（oov_rate/句长）与目标难度区间偏差须 < 阈值，否则 fail/review。

verdict 规则：
- fail：oov_rate 高于目标上限（生词率超目标，语篇过难）。
- review：oov_rate 低于目标下限（语篇过易，需教研确认是否调整目标）；
  或无 OOV 基线（无法判定生词率，转人工确认难度）。
- pass：oov_rate 在目标区间内。

为什么 oov 过高判 fail 而过低判 review：过高直接伤害学生（看不懂=无效练习），
过低只是「不够挑战」（可能是有意选简易语篇），由教研裁决。

宪法 A5/X6：不 import 学科包/学段包；难度分析来自 difficulty_analyzer（核心域）。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.content.difficulty_analyzer import (
    DifficultyDeviation,
    DifficultyReport,
    analyze_difficulty,
)
from src.core.content.passage_schema import DifficultyTarget
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)


class PassageDifficultyGateValidator(Validator):
    """语篇难度一致性验证器（T-W4-014 验收 #1/#4）.

    ctx.artifact_payload 期望字段：
    - body: str——语篇正文。
    - grade_band: str——学段 L/M/H。
    - difficulty_target: dict——目标难度区间（{min, max}），可选。
    - vocab_baseline: set[str]——课标字表（字级集合），可选；None 时不计算 oov。
    - difficulty_metrics: dict——预计算难度指标（可选；未提供时实时分析）。

    verdict 规则：
    - fail：oov_rate 高于目标上限（语篇过难）。
    - review：oov_rate 低于目标下限（过易）/ 无 OOV 基线 / 无目标区间。
    - pass：oov_rate 在目标区间内。
    """

    validator_id = "passage_difficulty_gate"
    version = "1.0.0+passage"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload = ctx.artifact_payload
        if payload is None:
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "artifact_payload 为 None，无法校验难度"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        body: str = payload.get("body", "") or ""
        grade_band: str = payload.get("grade_band", "M")

        # 目标难度区间（可选）
        target_dict: dict[str, Any] | None = payload.get("difficulty_target")
        difficulty_target: DifficultyTarget | None = None
        if target_dict is not None:
            difficulty_target = DifficultyTarget(
                min=float(target_dict.get("min", 0.0)),
                max=float(target_dict.get("max", 1.0)),
            )

        # 课标字表（可选，由学科包/pipeline 注入）
        vocab_baseline: set[str] | None = payload.get("vocab_baseline")

        # 难度分析：优先用预计算指标中的 oov_rate，否则实时分析
        report: DifficultyReport
        pre_metrics = payload.get("difficulty_metrics")
        if pre_metrics is not None and isinstance(pre_metrics, dict):
            # 用预计算 oov_rate + 重新做偏差比对（确保与目标区间一致）
            # 为什么不直接构造 DifficultyMetrics：预计算指标可能只含部分字段，
            # 门判定只需 oov_rate；缺字段用 0 兜底，完整构造会因缺字段报错。
            from src.core.models.passage import DifficultyMetrics

            oov_rate = float(pre_metrics.get("oov_rate", 0.0) or 0.0)
            avg_sent_len = float(pre_metrics.get("avg_sentence_length", 0.0) or 0.0)
            metrics = DifficultyMetrics(
                avg_sentence_length=avg_sent_len,
                oov_rate=oov_rate,
                total_chars=int(pre_metrics.get("total_chars", 0) or 0),
                total_sentences=int(pre_metrics.get("total_sentences", 0) or 0),
                char_freq=pre_metrics.get("char_freq", {}) or {},
            )
            baseline_available = vocab_baseline is not None
            deviations: list[DifficultyDeviation] = []
            if difficulty_target is not None and baseline_available:
                from src.core.content.difficulty_analyzer import _build_deviation

                deviations.append(
                    _build_deviation("oov_rate", oov_rate, difficulty_target)
                )
            report = DifficultyReport(
                metrics=metrics,
                oov_baseline_available=baseline_available,
                deviations=deviations,
            )
        else:
            report = analyze_difficulty(
                body,
                grade_band,
                vocab_baseline=vocab_baseline,
                difficulty_target=difficulty_target,
            )

        # 无 OOV 基线：无法判定生词率，转 review
        if not report.oov_baseline_available:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "未提供课标字表(vocab_baseline)，无法计算生词率，转人工确认难度",
                    "oov_rate": report.metrics.oov_rate,
                    "avg_sentence_length": report.metrics.avg_sentence_length,
                    "oov_baseline_available": False,
                },
                confidence=Decimal("0.300"),
                elapsed_ms=elapsed_ms(),
            )

        # 无目标区间：无法比对，转 review
        if difficulty_target is None:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "未提供 difficulty_target，无法比对难度区间",
                    "oov_rate": report.metrics.oov_rate,
                    "avg_sentence_length": report.metrics.avg_sentence_length,
                    "oov_baseline_available": True,
                },
                confidence=Decimal("0.300"),
                elapsed_ms=elapsed_ms(),
            )

        # 有基线 + 有目标：按偏差判定
        if report.deviations:
            dev = report.deviations[0]
            if dev.status == "above":
                return self._timed_result(
                    verdict="fail",
                    evidence={
                        "reason": f"生词率 {dev.actual} 高于目标上限 {dev.target_max}（语篇过难）",
                        "oov_rate": dev.actual,
                        "target_min": dev.target_min,
                        "target_max": dev.target_max,
                        "delta": dev.delta,
                        "status": dev.status,
                    },
                    confidence=Decimal("1.000"),
                    elapsed_ms=elapsed_ms(),
                )
            if dev.status == "below":
                return self._timed_result(
                    verdict="review",
                    evidence={
                        "reason": f"生词率 {dev.actual} 低于目标下限 {dev.target_min}（语篇过易，需教研确认）",
                        "oov_rate": dev.actual,
                        "target_min": dev.target_min,
                        "target_max": dev.target_max,
                        "delta": dev.delta,
                        "status": dev.status,
                    },
                    confidence=Decimal("0.700"),
                    elapsed_ms=elapsed_ms(),
                )

        # within
        return self._timed_result(
            verdict="pass",
            evidence={
                "oov_rate": report.metrics.oov_rate,
                "target_min": difficulty_target.min,
                "target_max": difficulty_target.max,
                "avg_sentence_length": report.metrics.avg_sentence_length,
                "oov_baseline_available": True,
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册
register_validator("platform", PassageDifficultyGateValidator)
