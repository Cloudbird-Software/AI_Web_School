"""W3 S5 弱项报告聚合核（纯函数，无 IO）.

贝叶斯累积（架构 §4.5「多题证据贝叶斯累积，报告置信度即后验」）：
- 每错误类型一个 Beta(α, β) 后验，先验 Beta(1, 1)（无信息均匀先验）
- 每条错误推断（error_inferences[] 元素）是一次证据：
  α += confidence，β += 1 - confidence（置信度即该证据支持归因的强度）
- 报告置信度 = 后验均值 α / (α + β)

为什么置信度加权而非 0/1 计数：契约 §4 的 confidence 是规则给出的推断强度
（§4.5 置信度四层分离之推断层），直接计入后验让弱证据自然稀释——
「选某项是证据非因果」，高置信孤立题证据与低置信 compensatory 佐证
对后验的拉动应当不同。

证据阈值：evidence_count < min_evidence ⇒ 「证据不足」（§4.7 允许输出证据不足）。
v1 的 min_evidence 默认 3（诊断 Profile 每知识点≥3 孤立题的同源直觉：
3 条独立证据以下不做定论）。

已知限制（v1 明示）：未区分孤立题/compensatory 题的定位效力（评审 D8
「compensatory 只佐证不定位」由规则置信度间接承载），证据强度完全
委托给 error_inferences[].confidence。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 证据阈值默认值：3 条独立证据以下输出「证据不足」
MIN_EVIDENCE_DEFAULT = 3


@dataclass(frozen=True)
class InferenceEventView:
    """作答事件的报告视图（response_event 的最小投影）."""

    item_version_id: str
    error_inferences: tuple[dict[str, Any], ...]


@dataclass
class ErrorEvidence:
    """单错误类型的累积证据（Beta 后验 + 计数 + 来源题集合）."""

    error_type_id: str
    evidence_count: int = 0
    alpha: float = 1.0  # Beta 先验 α0 = 1
    beta: float = 1.0  # Beta 先验 β0 = 1
    contributing_item_version_ids: set[str] = field(default_factory=set)

    @property
    def posterior(self) -> float:
        """后验均值 = 归因置信度（§4.5 报告置信度即后验）."""
        return self.alpha / (self.alpha + self.beta)

    def add(self, confidence: float, item_version_id: str) -> None:
        """累积一条证据（置信度加权）."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"error_inferences[].confidence 越界 [0,1]: {confidence!r}"
            )
        self.evidence_count += 1
        self.alpha += confidence
        self.beta += 1.0 - confidence
        self.contributing_item_version_ids.add(item_version_id)


def aggregate_inferences(
    events: Iterable[InferenceEventView],
) -> dict[str, ErrorEvidence]:
    """按 error_type_id 归并全部事件的错误推断（纯函数）.

    Args:
        events: 报告视图事件流（场景过滤由调用方在取数层完成——D5 分场景
            取数在 SQL WHERE 定型，不在聚合层混合后再拆）。

    Returns:
        {error_type_id: ErrorEvidence}；只含有过错误推断的类型。
    """
    evidences: dict[str, ErrorEvidence] = {}
    for event in events:
        for inference in event.error_inferences:
            error_type_id = inference.get("error_type_id")
            if not isinstance(error_type_id, str) or not error_type_id:
                # 契约 §4 required=error_type_id；脏数据跳过而非炸报告
                continue
            confidence = inference.get("confidence", 0.0)
            ev = evidences.setdefault(
                error_type_id, ErrorEvidence(error_type_id=error_type_id)
            )
            ev.add(float(confidence), event.item_version_id)
    return evidences
