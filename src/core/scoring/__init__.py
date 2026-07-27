"""W3-S4 评分域：评分器插件框架 + 平台通用评分器 + 评分执行服务.

- registry：统一契约（ScoreResult/Scorer）与注册制（学科桶+platform 回退）。
- platform_scorers：exact_match / keypoint_hit / stepwise_rubric（import 即注册）。
- service：run_scorer（调度）/ infer_option_errors（选择题选项→错误类型映射）/
  score_and_record（评分 + response_event 落账）。

宪法 A5/X6：本包不 import 任何学科包/学段包；学科评分器由学科包侧注册。
"""
from __future__ import annotations

from src.core.scoring.registry import (
    ScoreResult,
    Scorer,
    ScorerLike,
    get_scorer,
    list_scorers,
    register_scorer,
    reset_scorer_registry,
)
from src.core.scoring.service import (
    ScorerNotRegisteredError,
    ScoringOutcome,
    build_scoring_trace,
    infer_option_errors,
    run_scorer,
    score_and_record,
)

__all__ = [
    "ScoreResult",
    "Scorer",
    "ScorerLike",
    "ScorerNotRegisteredError",
    "ScoringOutcome",
    "build_scoring_trace",
    "get_scorer",
    "infer_option_errors",
    "list_scorers",
    "register_scorer",
    "reset_scorer_registry",
    "run_scorer",
    "score_and_record",
]
