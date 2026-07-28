"""T-W4-014 语篇适龄性验证器.

架构 v2 §4.3：语篇校验门第二道——适龄性。
- 学段×体裁匹配：低段（L）排除复杂社会议题体裁（argumentative/news_report）。
- 句长适龄：平均句长不超过学段上限（低段短句为主）。
- 主题适龄：低段排除复杂社会议题主题词（战争/贫困/死亡等）。

verdict 规则：
- fail：体裁与学段不匹配 / 句长超学段上限 / 低段命中复杂主题词。
- pass：全部适龄检查通过。

为什么句长用软阈值：句长是阅读负荷的代理指标，超限不一定是「不适龄」
（如长篇叙事的低段选段），但显著超限需教研确认；本验证器对显著超限
（>1.5 倍上限）判 fail，轻微超限（上限~1.5倍）判 review。

宪法 A5/X6：不 import 学科包/学段包；句长上限来自 difficulty_analyzer（核心域）。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.content.difficulty_analyzer import (
    grade_band_sentence_length_ceiling,
)
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

# 低段（L）不适配的体裁（与 passage_schema._LOW_BAND_BLOCKED_GENRES 一致）
_LOW_BAND_BLOCKED_GENRES: frozenset[str] = frozenset(
    {"argumentative", "news_report"}
)

# 低段（L）不适配的主题词（复杂社会议题）
_LOW_BAND_BLOCKED_TOPICS: frozenset[str] = frozenset(
    {
        "战争",
        "贫困",
        "死亡",
        "离婚",
        "犯罪",
        "自杀",
        "毒品",
    }
)

# 句长显著超限倍数（>上限 × 此倍数 → fail；上限~此倍数 → review）
_SENTENCE_LENGTH_FAIL_MULTIPLIER = 1.5


class PassageAgeAppropriateValidator(Validator):
    """语篇适龄性验证器（T-W4-014 验收 #1/#3）.

    ctx.artifact_payload 期望字段：
    - body: str——语篇正文。
    - grade_band: str——学段 L/M/H。
    - genre: str——体裁。
    - difficulty_metrics: dict——难度指标（含 avg_sentence_length）。

    verdict 规则：
    - fail：低段+复杂体裁 / 低段+复杂主题词 / 句长显著超限（>1.5×上限）。
    - review：句长轻微超限（上限~1.5×上限），需教研确认。
    - pass：全部适龄。
    """

    validator_id = "passage_age_appropriate"
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
                evidence={"reason": "artifact_payload 为 None，无法校验适龄性"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        grade_band: str = payload.get("grade_band", "")
        genre: str = payload.get("genre", "")
        body: str = payload.get("body", "") or ""
        metrics: dict[str, Any] = payload.get("difficulty_metrics", {}) or {}

        errors: list[str] = []

        # 1. 学段×体裁匹配：低段排除复杂社会议题体裁
        if grade_band == "L" and genre in _LOW_BAND_BLOCKED_GENRES:
            errors.append(
                f"低段(L)不适配体裁 {genre!r}：低段不出现复杂社会议题"
            )

        # 2. 低段主题词检查
        if grade_band == "L":
            topic_hits = [
                t for t in _LOW_BAND_BLOCKED_TOPICS if t in body
            ]
            if topic_hits:
                errors.append(
                    f"低段(L)命中复杂社会议题主题词：{topic_hits}"
                )
        else:
            topic_hits = []

        # 3. 句长适龄性
        avg_sent_len = float(metrics.get("avg_sentence_length", 0.0) or 0.0)
        ceiling = grade_band_sentence_length_ceiling(grade_band)
        sentence_verdict = "pass"
        if avg_sent_len > ceiling * _SENTENCE_LENGTH_FAIL_MULTIPLIER:
            errors.append(
                f"平均句长 {avg_sent_len} 显著超 {grade_band} 段上限 "
                f"{ceiling}（>{_SENTENCE_LENGTH_FAIL_MULTIPLIER}x）"
            )
            sentence_verdict = "fail"
        elif avg_sent_len > ceiling:
            # 轻微超限：review
            sentence_verdict = "review"

        if errors:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "; ".join(errors),
                    "grade_band": grade_band,
                    "genre": genre,
                    "avg_sentence_length": avg_sent_len,
                    "ceiling": ceiling,
                    "topic_hits": topic_hits,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        if sentence_verdict == "review":
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": f"平均句长 {avg_sent_len} 轻微超 {grade_band} 段上限 {ceiling}，需教研确认",
                    "grade_band": grade_band,
                    "avg_sentence_length": avg_sent_len,
                    "ceiling": ceiling,
                },
                confidence=Decimal("0.700"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "grade_band": grade_band,
                "genre": genre,
                "avg_sentence_length": avg_sent_len,
                "ceiling": ceiling,
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册
register_validator("platform", PassageAgeAppropriateValidator)
