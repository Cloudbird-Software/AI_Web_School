"""T-W4-014 语篇事实安全验证器.

架构 v2 §4.3 / 宪法 D2：语篇校验门第一道——事实安全。
- 政治敏感词：规则匹配（小规模敏感词表，生产由合规包扩展）。
- 暴力词：规则匹配（打斗/凶器/血腥词汇）。
- 明显常识错误：规则无法覆盖，返回 review（转人工/AI 抽检，non_goals 不做完整知识库）。

verdict 规则：
- fail：命中政治敏感词或暴力词。
- review：未命中规则但需人工复核常识正确性（规则覆盖有限）。
- pass：规则全过（常识由人工兜底，本验证器不阻断 pass 的常识风险）。

为什么用规则而非 AI：验证器在门热路径，AI 调用经总线（T-W4-007）增加成本与延迟；
规则匹配零成本、确定、可审计，敏感词表可由合规包运行时注入扩展。
明显常识错误需知识库（non_goals），本验证器只做规则能覆盖的部分，余下转 review。

宪法 A5/X6：不 import 学科包/学段包；宪法 D2：本验证器只校验，不绕过写入服务。
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

# ────────────────────────────────────────────────────────────────────
# 敏感词表（最小可用集，生产由合规包扩展）
# ────────────────────────────────────────────────────────────────────
# 政治敏感词：仅含示例性词条，证明规则链可运行；生产应由合规包注入扩展表。
_POLITICAL_SENSITIVE: frozenset[str] = frozenset(
    {
        # 示例性政治敏感词（生产扩展完整词表）
        "反动",
        "颠覆",
        "分裂国家",
        "敌对势力",
    }
)

# 暴力词：打斗/凶器/血腥
_VIOLENCE_TERMS: frozenset[str] = frozenset(
    {
        "杀人",
        "砍杀",
        "血腥",
        "屠杀",
        "凶器",
        "持刀",
        "枪杀",
        "暴力",
    }
)


def _scan_terms(text: str, terms: frozenset[str]) -> list[str]:
    """扫描文本中命中的敏感词，返回命中词列表（去重保序）."""
    hit: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in text and term not in seen:
            hit.append(term)
            seen.add(term)
    return hit


class PassageFactCheckValidator(Validator):
    """语篇事实安全验证器（T-W4-014 验收 #1/#2）.

    ctx.artifact_payload 期望字段：
    - body: str——语篇正文（必填）。

    verdict 规则：
    - fail：命中政治敏感词或暴力词（任一 fail 阻断入库）。
    - review：规则全过但常识正确性需人工复核（规则覆盖有限）。
    - pass：规则全过（常识风险由 review 兜底，不在此 pass 阻断）。

    evidence 结构：
    - political_hits: list[str]——命中的政治敏感词。
    - violence_hits: list[str]——命中的暴力词。
    - reason: str——判定原因。
    """

    validator_id = "passage_fact_check"
    version = "1.0.0+passage"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload = ctx.artifact_payload
        if payload is None or "body" not in payload:
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "artifact_payload 缺 body 字段，无法校验事实安全"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        body: str = payload.get("body", "") or ""

        political_hits = _scan_terms(body, _POLITICAL_SENSITIVE)
        violence_hits = _scan_terms(body, _VIOLENCE_TERMS)

        if political_hits:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "命中政治敏感词",
                    "political_hits": political_hits,
                    "violence_hits": violence_hits,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        if violence_hits:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "命中暴力词",
                    "political_hits": political_hits,
                    "violence_hits": violence_hits,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 规则全过：常识正确性需人工复核（规则覆盖有限，转 review）
        # 为什么不直接 pass：小学语篇常识错误（如「太阳从西边升起」）规则难穷举，
        # review 标记让教研人工确认，不阻断但留痕。
        return self._timed_result(
            verdict="review",
            evidence={
                "reason": "规则匹配全过，常识正确性需人工复核",
                "political_hits": [],
                "violence_hits": [],
                "needs_human_review": True,
            },
            confidence=Decimal("0.500"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册（pack_id='platform'，语篇为跨学科通用产物类型）
register_validator("platform", PassageFactCheckValidator)
