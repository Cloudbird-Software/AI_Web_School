"""T-W4-001 先验/实测贝叶斯收缩三档融合策略引擎.

架构 v2 §4.7「参数标定」融合档：source=prior_*（先验）与 source=measured_*
（实测）按样本量 n 三档收缩融合，产出可写入 item_param 的融合参数行。

三档策略（BRIEF S1 / 验收 §2）：
- n < 200   ：先验主导（实测信息不足，向先验收缩保稳）
- 200 ≤ n ≤ 1000：精度加权收缩（先验与实测按精度加权融合）
- n > 1000  ：实测主导（实测信息充足，贴近实测，误差 < 1%）

实现为可替换纯函数（D6 估计器可替换）：
- shrink(prior, measured, n, purpose_scope) → 融合参数 dict，
  含 source 标记与置信区间，结构与 ItemParam 列对齐（params/source/
  purpose_scope/sample_size/method_version），额外携带 weight_measured
  与 confidence_interval 供报告层使用。
- 权重函数 _weight_measured(n) 分段连续、单调递增、值域 [0,1]，
  三档边界严格满足验收：n=50 先验主导、n=500 居中、n=1500 实测主导
  （与实测误差 < 1%）。

宪法 D5 分场景禁混估：
- purpose_scope 必填单值（practice/diagnosis/measurement），越域抛 ValueError；
- 函数结构上不存在跨场景聚合路径——prior/measured/himself 同属一个 scope，
  若 prior/measured 携带 purpose_scope 键则必须与入参一致（显式拒绝混估）。
- 练习场景数据因自适应暴露偏差仅用于粗校准与差题预警（评审报告 D4），
  本函数不做用途特判，由调用方按场景取数后调用。

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 融合产出来源标识（item_param.source 域 measured_.+，与 ctt 的 measured_ctt 同族；
# 收缩是实测参数的精炼手段，仍属实测侧——先验只是借力，不改变 source 性质）
SHRINKAGE_SOURCE = "measured_shrinkage"
# 融合方法版本（D6：策略迭代时递增，历史行引用当时版本）
SHRINKAGE_METHOD_VERSION = "shrinkage-v1"

# 场景三值域（与 ctt.VALID_PURPOSE_SCOPES / D5 对齐）
VALID_PURPOSE_SCOPES: frozenset[str] = frozenset(
    {"practice", "diagnosis", "measurement"}
)

# 三档边界（架构 v2 §4.7 默认档）
TIER_PRIOR_MAX = 200          # n < 200：先验主导
TIER_MID_MAX = 1000           # 200 ≤ n ≤ 1000：精度加权收缩；>1000 实测主导
# 第三档指数衰减常数：w = 1 - 0.1*exp(-(n-1000)/DECAY_TAU)
# 取 200 使 n=1500 时 w≈0.9918（与实测误差 <1%，验收 §2）。
DECAY_TAU = 200.0

# 95% 置信区间 z 值（标准正态双侧 0.05）
Z_95 = 1.959964

# 参数值域（CI 裁剪用）
_RANGE: dict[str, tuple[float, float]] = {
    "difficulty": (0.0, 1.0),       # 正确率 p ∈ [0,1]
    "discrimination": (-1.0, 1.0),  # Pearson r ∈ [-1,1]
}


# ────────────────────────────────────────────────────────────────────
# 权重函数（纯函数，三档分段连续单调）
# ────────────────────────────────────────────────────────────────────


def _weight_measured(n: int) -> float:
    """返回实测权重 w(n) ∈ [0,1]，三档分段连续单调递增.

    为什么分段而非单一 n/(n+τ)：单一 τ 无法同时满足「n<200 先验主导」
    与「n>1000 实测误差<1%」——前者要 τ 大、后者要 τ 相对 n 可忽略。
    分段在三档语义边界处用连续拼接，保证单调且无跳变。

    分段：
    - n ≤ 200：w = 0.5 * n/200            （线性 0→0.5，先验主导）
    - 200 < n ≤ 1000：w = 0.5 + 0.4*(n-200)/800  （线性 0.5→0.9，精度加权）
    - n > 1000：w = 1 - 0.1*exp(-(n-1000)/DECAY_TAU)
      （指数趋近 1.0，实测主导；n=1500 时 w≈0.9918）

    边界连续性：n=200 处两侧均 0.5；n=1000 处两侧均 0.9。
    """
    if n <= 0:
        return 0.0
    if n <= TIER_PRIOR_MAX:
        return 0.5 * (n / TIER_PRIOR_MAX)
    if n <= TIER_MID_MAX:
        return 0.5 + 0.4 * (n - TIER_PRIOR_MAX) / (TIER_MID_MAX - TIER_PRIOR_MAX)
    return 1.0 - 0.1 * math.exp(-(n - TIER_MID_MAX) / DECAY_TAU)


def _clip(value: float, key: str) -> float:
    """按参数键裁剪到合法值域."""
    lo, hi = _RANGE.get(key, (float("-inf"), float("inf")))
    return max(lo, min(hi, value))


def _param_ci(
    key: str,
    shrunk: float,
    measured: Optional[float],
    n: int,
) -> tuple[float, float]:
    """单参数 95% 置信区间.

    为什么按 key 区分：difficulty（正确率）与 discrimination（相关系数）
    的标准误公式不同；错误套用会给出误导性 CI。n=0 或 measured 缺失时
    返回该参数的全值域（最大不确定性，不伪造精度）。

    - difficulty：二项 se = sqrt(p*(1-p)/n)，p 取 measured（实测是精度来源）
    - discrimination：Pearson r 的 se ≈ sqrt((1-r^2)/(n-2))（n>2）
    - 其它数值：保守 se = 1/sqrt(max(n,1))，裁剪到已知值域或不动
    """
    lo, hi = _RANGE.get(key, (float("-inf"), float("inf")))
    if n <= 0 or measured is None:
        return lo, hi
    if key == "difficulty":
        p = min(max(measured, 0.0), 1.0)
        se = math.sqrt(p * (1.0 - p) / n)
    elif key == "discrimination":
        r = min(max(measured, -1.0), 1.0)
        se = math.sqrt((1.0 - r * r) / max(n - 2, 1)) if n > 2 else float("inf")
    else:
        se = 1.0 / math.sqrt(max(n, 1))
    if not math.isfinite(se):
        return lo, hi
    return _clip(shrunk - Z_95 * se, key), _clip(shrunk + Z_95 * se, key)


# ────────────────────────────────────────────────────────────────────
# 主接口：shrink
# ────────────────────────────────────────────────────────────────────


def shrink(
    prior: Mapping[str, float],
    measured: Mapping[str, Optional[float]],
    n: int,
    purpose_scope: str,
    *,
    method_version: str = SHRINKAGE_METHOD_VERSION,
) -> dict[str, Any]:
    """先验/实测贝叶斯收缩三档融合（纯函数，无副作用）.

    Args:
        prior: 先验参数体（source=prior_* 的 params），如 {"difficulty": 0.5}。
        measured: 实测参数体（source=measured_* 的 params）；某键不可计算时
            可为 None（如 CTT 区分度在 n<2 时为 None），该键回退到先验。
        n: 实测样本量；n=0 表示无实测，纯先验输出。
        purpose_scope: 场景（practice/diagnosis/measurement），D5 禁混估——
            若 prior/measured 携带 ``purpose_scope`` 键，必须与本参数一致，
            否则抛 ValueError（显式拒绝跨场景混估）。
        method_version: 融合方法版本（D6 可替换，默认 shrinkage-v1）。

    Returns:
        融合参数 dict，与 ItemParam 列对齐，额外携带报告元数据::

            {
              "params": {<key>: <shrunk_value>, ...},
              "source": "measured_shrinkage",
              "purpose_scope": <scope>,
              "sample_size": <n>,
              "method_version": "shrinkage-v1",
              "weight_measured": <w>,
              "confidence_interval": {<key>: [low, high], ...},
            }

    Notes:
        - 输出 source=measured_shrinkage 落 item_param 时满足 CHECK 正则
          ``measured_.+``；先验行与实测行各自只增不改，融合产生新行（D1/D6）。
        - 同 (prior, measured, n, scope) 输入必得同输出（D6 可重放）。
    """
    if purpose_scope not in VALID_PURPOSE_SCOPES:
        raise ValueError(
            f"purpose_scope 越域：{purpose_scope!r}"
            f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
        )
    # 显式拒绝混估：prior/measured 若自带 scope 标记，必须与本次融合 scope 一致
    for _label, _bag in (("prior", prior), ("measured", measured)):
        _scope = _bag.get("purpose_scope") if isinstance(_bag, Mapping) else None
        if _scope is not None and _scope != purpose_scope:
            raise ValueError(
                f"{_label} 携带 purpose_scope={_scope!r} 与融合 scope="
                f"{purpose_scope!r} 不一致（D5 禁止跨场景混估）"
            )

    if n < 0:
        # sample_size 是非负整数（item_param CHECK），负值无意义
        raise ValueError(f"n 不能为负：{n}")

    w = _weight_measured(n)
    params: dict[str, float] = {}
    ci: dict[str, list[float]] = {}

    # 以 prior 与 measured 的键并集为融合键（先验可能定义实测未覆盖的维度）
    keys: Sequence[str] = sorted(set(prior) | set(measured))
    for key in keys:
        if key == "purpose_scope":
            continue  # 元数据键不参与数值融合
        p_val = prior.get(key)
        m_val = measured.get(key)
        # 实测缺失（None）或 n=0 → 回退先验
        if m_val is None or n <= 0:
            if p_val is None:
                continue  # 双侧都无该维度，跳过
            shrunk = float(p_val)
        elif p_val is None:
            # 无先验、有实测 → 直接用实测（无先验可借力）
            shrunk = float(m_val)
        else:
            shrunk = w * float(m_val) + (1.0 - w) * float(p_val)
        shrunk = _clip(shrunk, key)
        params[key] = shrunk
        ci[key] = list(_param_ci(key, shrunk, m_val if m_val is not None else None, n))

    return {
        "params": params,
        "source": SHRINKAGE_SOURCE,
        "purpose_scope": purpose_scope,
        "sample_size": n,
        "method_version": method_version,
        "weight_measured": w,
        "confidence_interval": ci,
    }


__all__ = [
    "SHRINKAGE_SOURCE",
    "SHRINKAGE_METHOD_VERSION",
    "VALID_PURPOSE_SCOPES",
    "TIER_PRIOR_MAX",
    "TIER_MID_MAX",
    "DECAY_TAU",
    "shrink",
]

