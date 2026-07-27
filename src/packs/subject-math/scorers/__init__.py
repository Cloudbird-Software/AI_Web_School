"""数学评分器包.

W3-S4：提供 register_math_scorers()，把 W2 已实现的 math_equivalence
评分器注入核心域 scorer 注册表（src/core/scoring/registry.py）。

为什么需要显式注册函数而非模块级副作用：目录名含连字符（subject-math），
无法普通 import；调用方（生产线/E2E 入口/测试）按需以 importlib 加载本模块
后调用 register_math_scorers()——与 pinyin_to_word_pipeline 加载学科
验证器的既有模式一致。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from src.core.scoring.registry import register_scorer

PACK_ID = "subject-math"

_ME_MODULE_NAME = "subject_math_scorers_math_equivalence"
_ME_PATH = Path(__file__).resolve().parent / "math_equivalence.py"


def _load_math_equivalence_module():
    """以 importlib 加载 math_equivalence.py（连字符目录无法普通 import）."""
    if _ME_MODULE_NAME in sys.modules:
        return sys.modules[_ME_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_ME_MODULE_NAME, _ME_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_ME_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_ME_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def register_math_scorers() -> None:
    """把数学包评分器注册到核心 scorer 注册表（幂等）.

    注册项：
    - math_equivalence（T-W2-028 实现，句柄类 MathEquivalenceScorer，
      满足 ScorerLike 协议：scorer_id/version/score 三要素）。
    """
    mod = _load_math_equivalence_module()
    register_scorer(PACK_ID, mod.MathEquivalenceScorer())


__all__ = ["PACK_ID", "register_math_scorers"]
