"""W3 S5 弱项报告 v1（业务全景 §三：诊断报告是差异化核心；架构 §4.5/§4.6）.

子模块：
- aggregator：纯函数聚合——按 error_type 归并作答事件的错误推断，
  证据计数 + Beta 贝叶斯累积后验（§4.5「多题证据贝叶斯累积，报告置信度即后验」）
- schemas：报告 Pydantic 模型（API 响应契约）
- service：build_weakness_report（事件取数 + 聚合 + 阈值判定）+
  recommend_practice（按错误类型查已发布实例池组 5 题小卷）

关键语义：证据阈值以下输出「证据不足」而非定论（架构 §4.7「允许输出证据不足」）；
未达阈值不给推荐——没有定论的练习推荐是对学生的误导。

宪法 A5：本包禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from src.core.report.aggregator import (
    MIN_EVIDENCE_DEFAULT,
    ErrorEvidence,
    aggregate_inferences,
)
from src.core.report.schemas import WeaknessItem, WeaknessReport
from src.core.report.service import build_weakness_report, recommend_practice

__all__ = [
    "MIN_EVIDENCE_DEFAULT",
    "ErrorEvidence",
    "aggregate_inferences",
    "WeaknessItem",
    "WeaknessReport",
    "build_weakness_report",
    "recommend_practice",
]
