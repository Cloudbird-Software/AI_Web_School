"""T-W4-001 贝叶斯收缩三档融合策略引擎测试.

覆盖任务卡验收 §1-§5：
  §1 shrink 返回融合 dict，含 source 标记与置信区间（结构可映射 item_param）。
  §2 三档边界正确：n=50 先验主导 / n=500 居中 / n=1500 实测主导（误差<1%）。
  §3 三场景独立测试通过，交叉混估被断言拒绝（D5）。
  §4 make accept TASK=T-W4-001 全绿（本文件即单元测试主体）。
  §5 不 import 任何学科包/学段包（A5/X6 静态扫描）。

纯函数无副作用——本文件全部为同步单测，不触 DB（D6 可重放的最直接实证）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.data.bayesian_shrinkage import (
    DECAY_TAU,
    SHRINKAGE_METHOD_VERSION,
    SHRINKAGE_SOURCE,
    TIER_MID_MAX,
    TIER_PRIOR_MAX,
    VALID_PURPOSE_SCOPES,
    _weight_measured,
    shrink,
)

# 一组贯穿用例的先验/实测参数体（difficulty/discrimination 两键）
PRIOR = {"difficulty": 0.5, "discrimination": 0.30}
MEASURED = {"difficulty": 0.8, "discrimination": 0.50}


# ────────────────────────────────────────────────────────────────────
# §1 返回结构与 source 标记 / 置信区间
# ────────────────────────────────────────────────────────────────────


class TestReturnShape:
    """shrink 返回结构与 item_param 列对齐，携带 CI。"""

    def test_returns_required_keys(self) -> None:
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        assert set(out) >= {
            "params", "source", "purpose_scope", "sample_size",
            "method_version", "weight_measured", "confidence_interval",
        }

    def test_source_marker_is_measured_shrinkage(self) -> None:
        """source 标记落 item_param.source CHECK 正则 measured_.+."""
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        assert out["source"] == SHRINKAGE_SOURCE
        assert re.match(r"^measured_.+$", out["source"])

    def test_purpose_scope_and_sample_size_echoed(self) -> None:
        out = shrink(PRIOR, MEASURED, n=321, purpose_scope="diagnosis")
        assert out["purpose_scope"] == "diagnosis"
        assert out["sample_size"] == 321

    def test_method_version_default(self) -> None:
        out = shrink(PRIOR, MEASURED, n=10, purpose_scope="measurement")
        assert out["method_version"] == SHRINKAGE_METHOD_VERSION
        assert out["method_version"] == "shrinkage-v1"

    def test_confidence_interval_per_key(self) -> None:
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        ci = out["confidence_interval"]
        assert set(ci) == {"difficulty", "discrimination"}
        for key, interval in ci.items():
            assert len(interval) == 2
            low, high = interval
            assert low <= high
            # shrunk 值落在 CI 内
            assert low <= out["params"][key] <= high

    def test_ci_within_valid_range(self) -> None:
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        ci = out["confidence_interval"]
        assert 0.0 <= ci["difficulty"][0] and ci["difficulty"][1] <= 1.0
        assert -1.0 <= ci["discrimination"][0] and ci["discrimination"][1] <= 1.0


# ────────────────────────────────────────────────────────────────────
# §2 三档边界
# ────────────────────────────────────────────────────────────────────


class TestThreeTiers:
    """n<200 先验主导 / 200-1000 精度加权 / >1000 实测主导（误差<1%）。"""

    def test_n50_prior_dominated(self) -> None:
        """n=50：融合值更靠近先验（|sh-prior| < |sh-measured|）."""
        out = shrink(PRIOR, MEASURED, n=50, purpose_scope="practice")
        sh = out["params"]["difficulty"]
        assert abs(sh - PRIOR["difficulty"]) < abs(sh - MEASURED["difficulty"])
        # 权重应很小（先验主导）
        assert out["weight_measured"] < 0.2

    def test_n500_in_between(self) -> None:
        """n=500：融合值介于先验与实测之间，权重 ∈ (0.5, 0.9)."""
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        sh = out["params"]["difficulty"]
        assert PRIOR["difficulty"] < sh < MEASURED["difficulty"]
        assert 0.5 < out["weight_measured"] < 0.9

    def test_n1500_measured_dominated_error_under_1pct(self) -> None:
        """n=1500：融合值接近实测，与实测误差 <1%（先验权重 <1%）."""
        out = shrink(PRIOR, MEASURED, n=1500, purpose_scope="practice")
        sh = out["params"]["difficulty"]
        # 先验权重 = 1 - w < 1%
        assert out["weight_measured"] > 0.99
        # 与实测绝对误差 < 0.01（difficulty ∈ [0,1]，1% 即 0.01）
        assert abs(sh - MEASURED["difficulty"]) < 0.01

    def test_weight_monotonic_increasing(self) -> None:
        """权重随 n 单调递增（n 越大越信实测）."""
        ns = [0, 1, 50, 200, 500, 1000, 1500, 5000]
        ws = [_weight_measured(n) for n in ns]
        assert ws == sorted(ws)
        assert ws[0] == 0.0  # n=0 无实测

    def test_weight_continuous_at_boundaries(self) -> None:
        """三档拼接处权重连续：tier1 在 n=200 处取 0.5，tier2 在 n=200 起始
        也取 0.5；tier2 在 n=1000 处取 0.9，tier3 在 n=1000 起始也取 0.9
        （exp(0)=1 → 1-0.1=0.9）。边界值相等即拼接无跳变。"""
        eps = 1e-9
        # tier1 公式在 n=TIER_PRIOR_MAX 处 = 0.5；tier2 公式在 n=TIER_PRIOR_MAX 处 = 0.5
        assert abs(_weight_measured(TIER_PRIOR_MAX) - 0.5) < eps
        # tier2 公式在 n=TIER_MID_MAX 处 = 0.9；tier3 公式在 n=TIER_MID_MAX 处 = 0.9
        assert abs(_weight_measured(TIER_MID_MAX) - 0.9) < eps
        # tier3 在 n 远大于边界时趋近 1.0（实测主导上界）
        assert _weight_measured(TIER_MID_MAX + 50 * DECAY_TAU) > 0.999

    def test_ci_shrinks_as_n_grows(self) -> None:
        """置信区间随 n 增大收窄（数据越多越确定）."""
        small = shrink(PRIOR, MEASURED, n=50, purpose_scope="practice")
        large = shrink(PRIOR, MEASURED, n=2000, purpose_scope="practice")
        w_small = (
            small["confidence_interval"]["difficulty"][1]
            - small["confidence_interval"]["difficulty"][0]
        )
        w_large = (
            large["confidence_interval"]["difficulty"][1]
            - large["confidence_interval"]["difficulty"][0]
        )
        assert w_large < w_small


# ────────────────────────────────────────────────────────────────────
# §3 三场景独立 + 交叉混估被拒绝（D5）
# ────────────────────────────────────────────────────────────────────


class TestSceneIsolation:
    """practice/diagnosis/measurement 三场景独立，混估被拒绝."""

    @pytest.mark.parametrize("scope", sorted(VALID_PURPOSE_SCOPES))
    def test_each_scope_produces_valid_fusion(self, scope: str) -> None:
        """三场景各自调用均产出正确标记的融合 dict."""
        out = shrink(PRIOR, MEASURED, n=500, purpose_scope=scope)
        assert out["purpose_scope"] == scope
        assert out["source"] == SHRINKAGE_SOURCE
        # 三场景同输入下数值结果一致（数学与 scope 无关，scope 仅是标签）
        ref = shrink(PRIOR, MEASURED, n=500, purpose_scope="practice")
        assert out["params"] == ref["params"]

    def test_invalid_scope_rejected(self) -> None:
        """purpose_scope 越域抛 ValueError（混估入口不存在）."""
        with pytest.raises(ValueError, match="purpose_scope"):
            shrink(PRIOR, MEASURED, n=500, purpose_scope="mixed")
        with pytest.raises(ValueError, match="purpose_scope"):
            shrink(PRIOR, MEASURED, n=500, purpose_scope="all")

    def test_prior_with_mismatched_scope_rejected(self) -> None:
        """prior 自带 scope 标记与融合 scope 不一致 → 拒绝（显式禁混估）."""
        prior_tagged = {**PRIOR, "purpose_scope": "diagnosis"}
        with pytest.raises(ValueError, match="prior"):
            shrink(prior_tagged, MEASURED, n=500, purpose_scope="practice")

    def test_measured_with_mismatched_scope_rejected(self) -> None:
        """measured 自带 scope 标记与融合 scope 不一致 → 拒绝."""
        measured_tagged = {**MEASURED, "purpose_scope": "measurement"}
        with pytest.raises(ValueError, match="measured"):
            shrink(PRIOR, measured_tagged, n=500, purpose_scope="practice")

    def test_matched_scope_tags_accepted(self) -> None:
        """prior/measured 自带 scope 与融合 scope 一致 → 接受."""
        prior_tagged = {**PRIOR, "purpose_scope": "practice"}
        measured_tagged = {**MEASURED, "purpose_scope": "practice"}
        out = shrink(prior_tagged, measured_tagged, n=500, purpose_scope="practice")
        # scope 元数据键不进 params 数值融合
        assert "purpose_scope" not in out["params"]


# ────────────────────────────────────────────────────────────────────
# 边界与确定性（D6 可重放）
# ────────────────────────────────────────────────────────────────────


class TestEdgeCasesAndDeterminism:
    """n=0 / measured None / 确定性。"""

    def test_n0_pure_prior_with_full_range_ci(self) -> None:
        """n=0：无实测，输出=先验，CI 为全值域（不伪造精度）."""
        out = shrink(PRIOR, MEASURED, n=0, purpose_scope="practice")
        assert out["weight_measured"] == 0.0
        assert out["params"]["difficulty"] == PRIOR["difficulty"]
        ci = out["confidence_interval"]["difficulty"]
        assert ci == [0.0, 1.0]

    def test_measured_none_falls_back_to_prior(self) -> None:
        """某键 measured=None（如 CTT 区分度 n<2）→ 回退先验."""
        measured = {"difficulty": 0.8, "discrimination": None}
        out = shrink(PRIOR, measured, n=50, purpose_scope="practice")
        assert out["params"]["discrimination"] == PRIOR["discrimination"]
        # 该键 CI 为全值域（实测缺失）
        assert out["confidence_interval"]["discrimination"] == [-1.0, 1.0]

    def test_prior_missing_key_uses_measured(self) -> None:
        """先验未覆盖的维度直接用实测（无先验可借力）."""
        prior = {"difficulty": 0.5}
        measured = {"difficulty": 0.8, "discrimination": 0.5}
        out = shrink(prior, measured, n=500, purpose_scope="practice")
        assert "discrimination" in out["params"]

    def test_negative_n_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            shrink(PRIOR, MEASURED, n=-1, purpose_scope="practice")

    def test_deterministic_same_input_same_output(self) -> None:
        """同输入必得同输出（D6 可重放）."""
        a = shrink(PRIOR, MEASURED, n=733, purpose_scope="diagnosis")
        b = shrink(PRIOR, MEASURED, n=733, purpose_scope="diagnosis")
        assert a == b

    def test_clipping_to_valid_range(self) -> None:
        """融合值裁剪到合法值域（difficulty ∈ [0,1]）."""
        out = shrink(
            {"difficulty": 1.0}, {"difficulty": 1.0}, n=2000,
            purpose_scope="practice",
        )
        assert 0.0 <= out["params"]["difficulty"] <= 1.0


# ────────────────────────────────────────────────────────────────────
# §5 不 import 学科包/学段包（A5/X6 静态扫描）
# ────────────────────────────────────────────────────────────────────


def test_no_subject_pack_imports_in_data() -> None:
    """src/core/data/ 不 import 任何学科包/学段包（宪法 A5/A7）.

    扫描 src/core/data 下所有 .py，禁止出现 from packs / import packs /
    from subject_ / import subject_ 形态的 import 语句。
    """
    data_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "data"
    )
    assert data_dir.is_dir(), f"目录不存在：{data_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(data_dir.rglob("*.py")):
        if pattern.findall(py_file.read_text(encoding="utf-8")):
            violations.append(str(py_file.relative_to(data_dir)))
    assert not violations, (
        f"src/core/data/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
