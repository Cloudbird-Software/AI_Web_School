"""T-W2-026 数学可解性采样验证器.

架构 v2 §5.2：对模板在参数空间采样，检测退化情况：
  - division_by_zero：除零（表达式含 a/b 且 b=0）
  - duplicate_options：干扰项之间值重复（选项不可区分）
  - distractor_collision：干扰项值 == 正解值（选项与正解碰撞）

设计要点：
  1. **零代码共享**：不 import src.core.instantiation.*；SymPy 求值复用
     同包 dual_check.py 的独立实现（AST→SymPy 转换器），与引擎完全独立。
  2. **采样策略**：参数空间 ≤ sample_count 时穷举，否则用固定 seed 随机
     采样（可复现）。
  3. **退化计数**：每个参数组合只计一次 degenerate（即使有多种 issue），
     保证 degeneration_rate ∈ [0, 1]；degenerate_examples 每条 issue 一条记录。
  4. **独立实现**：干扰项碰撞检测不依赖引擎 distractor 生成器——本模块
     直接用 SymPy 求值每条 distractor rule 的 expression，与正解比对。

目录名 subject-math 含连字符无法作为 Python 包名，故用 importlib 加载
同目录 dual_check.py（与 functions.py 加载 variable_types.py 同模式）。
"""
from __future__ import annotations

import importlib.util
import itertools
import random
import sys
import time
from decimal import Decimal as _PyDecimal
from pathlib import Path
from typing import Any

import sympy
from pydantic import BaseModel, ConfigDict, Field

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

__all__ = [
    "SolvabilityReport",
    "SolvabilityValidator",
    "sample_solvability",
]


# ────────────────────────────────────────────────────────────────────
# 加载同包 dual_check.py（连字符目录无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_DC_MODULE_NAME = "subject_math_dual_check"
_DC_PATH = Path(__file__).parent / "dual_check.py"


def _load_dual_check() -> Any:
    """以 importlib 加载 dual_check.py，复用 SymPy 求值助手.

    注册到 sys.modules 保证后续加载（如测试再次加载）复用同一模块实例。
    """
    if _DC_MODULE_NAME in sys.modules:
        return sys.modules[_DC_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_DC_MODULE_NAME, _DC_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_DC_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_DC_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_dc = _load_dual_check()
# 复用 dual_check 的 SymPy 求值助手（同包内共享，非与引擎共享）
_DivisionByZeroMarker = _dc._DivisionByZeroMarker
_build_sympy_env = _dc._build_sympy_env
evaluate_with_sympy = _dc.evaluate_with_sympy
_answers_equal = _dc._answers_equal


# ────────────────────────────────────────────────────────────────────
# 可解性报告
# ────────────────────────────────────────────────────────────────────


class SolvabilityReport(BaseModel):
    """可解性采样报告.

    - total_samples：实际采样数（穷举时 = 参数空间大小；随机时 = sample_count）。
    - degenerate_count：存在至少一种退化的参数组合数（≤ total_samples）。
    - degeneration_rate：退化率 = degenerate_count / total_samples。
    - degenerate_examples：退化样例列表（每条 issue 一条，可能多条同 params）。
    """

    model_config = ConfigDict(extra="forbid")

    total_samples: int = Field(..., ge=0)
    degenerate_count: int = Field(..., ge=0)
    degeneration_rate: float = Field(..., ge=0.0, le=1.0)
    degenerate_examples: list[dict[str, Any]] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 采样器
# ────────────────────────────────────────────────────────────────────


def sample_solvability(
    spec: dict,
    param_ranges: dict[str, list],
    *,
    sample_count: int = 100,
    seed: int = 0,
) -> SolvabilityReport:
    """在参数空间采样，检测退化情况.

    Args:
        spec: 母题 spec dict（含 answer_program, slots, distractor_rules）。
        param_ranges: 参数取值范围（槽名 → 候选值列表）。
        sample_count: 最小采样数（参数空间更小时穷举）。
        seed: 随机采样种子（可复现）。

    Returns:
        SolvabilityReport：采样数 / 退化数 / 退化率 / 退化样例。

    退化检测：
        - division_by_zero：正解或干扰项表达式求值时除零。
        - distractor_collision：某干扰项值 == 正解值。
        - duplicate_options：两个干扰项值彼此相等。
    """
    keys = list(param_ranges.keys())
    values_lists = [param_ranges[k] for k in keys]

    # 参数空间 ≤ sample_count → 穷举；否则随机采样
    all_combos = list(itertools.product(*values_lists))
    total_space = len(all_combos)
    if total_space == 0:
        return SolvabilityReport(
            total_samples=0,
            degenerate_count=0,
            degeneration_rate=0.0,
            degenerate_examples=[],
        )
    if total_space <= sample_count:
        samples = [dict(zip(keys, combo)) for combo in all_combos]
    else:
        rng = random.Random(seed)
        sampled = rng.sample(range(total_space), sample_count)
        samples = [dict(zip(keys, all_combos[i])) for i in sampled]

    total = len(samples)
    degenerate_count = 0
    degenerate_examples: list[dict[str, Any]] = []

    slots = spec.get("slots") or {}
    answer_program = spec.get("answer_program") or {}
    answer_expr = answer_program.get("expression")
    distractor_rules = (spec.get("distractor_rules") or {}).get("rules") or []

    for params in samples:
        issues: list[tuple[str, str]] = []

        # ── 1. 求正解 ──
        env = _build_sympy_env(params, slots)
        answer_val: sympy.Basic | None = None
        if answer_expr:
            try:
                answer_val = evaluate_with_sympy(answer_expr, env)
            except _DivisionByZeroMarker as e:
                issues.append(
                    ("division_by_zero", f"正解表达式除零：{e}")
                )
            except (ValueError, SyntaxError) as e:
                issues.append(
                    ("division_by_zero", f"正解表达式求值失败：{type(e).__name__}: {e}")
                )

        # ── 2. 求干扰项 ──
        distractor_vals: list[tuple[str, sympy.Basic]] = []
        for rule in distractor_rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("rule_type") != "deterministic":
                continue
            expr = rule.get("expression")
            if not expr:
                continue
            error_type_id = rule.get("error_type_id", "")
            try:
                dval = evaluate_with_sympy(expr, env)
                distractor_vals.append((error_type_id, dval))
            except _DivisionByZeroMarker as e:
                issues.append(
                    (
                        "division_by_zero",
                        f"干扰项表达式除零 (error_type_id={error_type_id})：{e}",
                    )
                )
            except (ValueError, SyntaxError) as e:
                issues.append(
                    (
                        "division_by_zero",
                        f"干扰项表达式求值失败 (error_type_id={error_type_id})：{e}",
                    )
                )

        # ── 3. 干扰项碰撞（=正解） ──
        if answer_val is not None:
            for eid, dval in distractor_vals:
                if _answers_equal(dval, answer_val):
                    issues.append(
                        (
                            "distractor_collision",
                            f"干扰项 (error_type_id={eid}) 与正解碰撞：值={dval}",
                        )
                    )

        # ── 4. 选项重复（干扰项之间） ──
        for i, (eid_i, vi) in enumerate(distractor_vals):
            for j, (eid_j, vj) in enumerate(distractor_vals):
                if i < j and _answers_equal(vi, vj):
                    issues.append(
                        (
                            "duplicate_options",
                            f"干扰项 (error_type_id={eid_i}) 与 (error_type_id={eid_j}) 重复：值={vi}",
                        )
                    )

        # ── 汇总 ──
        if issues:
            degenerate_count += 1
            for issue_type, detail in issues:
                degenerate_examples.append(
                    {
                        "params": dict(params),
                        "issue_type": issue_type,
                        "detail": detail,
                    }
                )

    rate = degenerate_count / total if total > 0 else 0.0
    # 限制示例数量（避免巨大报告）
    return SolvabilityReport(
        total_samples=total,
        degenerate_count=degenerate_count,
        degeneration_rate=rate,
        degenerate_examples=degenerate_examples[:50],
    )


# ────────────────────────────────────────────────────────────────────
# 可解性验证器
# ────────────────────────────────────────────────────────────────────


class SolvabilityValidator(Validator):
    """可解性采样验证器.

    对模板在参数空间采样，报告退化率。退化率高 → fail（阻断）；
    有退化但不高 → review（人工复核参数空间）；无退化 → pass。

    ctx.artifact_payload 期望字段：
    - spec: 母题 spec dict。
    - param_ranges: 参数取值范围（槽名 → 候选值列表）。
    - sample_count: 可选，采样数（默认 100）。
    - seed: 可选，随机种子（默认 0）。

    为什么 blocking=False：可解性采样是统计性校验，单次实例化的参数组合
    可能完全合法；采样发现的退化区域应提示人工复核参数空间约束，而非
    阻断当前实例签发。退化率极高（≥50%）时升级为 fail 阻断。
    """

    validator_id = "solvability"
    version = "1.0.0+subject-math"
    blocking = False
    cost_tier = "expensive"

    async def validate(
        self, artifact_ref: str, ctx: GateContext
    ) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload = ctx.artifact_payload
        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        spec = payload.get("spec")
        param_ranges = payload.get("param_ranges")
        if not isinstance(spec, dict) or not isinstance(param_ranges, dict):
            return self._timed_result(
                verdict="review",
                evidence={"reason": "payload 缺少 spec(dict) 或 param_ranges(dict)"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        sample_count = int(payload.get("sample_count", 100))
        seed = int(payload.get("seed", 0))

        report = sample_solvability(
            spec, param_ranges, sample_count=sample_count, seed=seed
        )

        if report.total_samples == 0:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "参数空间为空，无法采样"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        if report.degeneration_rate >= 0.5:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "退化率过高（≥50%），参数空间存在严重退化",
                    "report": report.model_dump(),
                },
                confidence=_PyDecimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        if report.degenerate_count > 0:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "存在退化样本，建议人工复核参数空间约束",
                    "report": report.model_dump(),
                },
                confidence=_PyDecimal("0.900"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "report": report.model_dump(),
            },
            confidence=_PyDecimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册
register_validator("subject-math", SolvabilityValidator)
