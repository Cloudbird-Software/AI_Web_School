"""T-W2-023 / T-W2-024 黄金数据集回归测试.

每条 tests/golden/items/**/*.yaml 黄金用例经 ``golden_case`` fixture 加载后，
本测试用实例化引擎重新实例化，断言：
  1. 实际 ``item_version_id`` == ``expected_item_version_id``（公式一稳定性）
  2. 实际 ``content`` == ``expected_content_snapshot``（content 漂移捕获）

这是 W2 防线 5：实例化引擎/DSL/评分器任何变更若破坏既有产物，本回归立刻报红。

宪法 X6：本模块不 import 任何学科包/学段包（引擎是核心域，无学科依赖）。
"""
from __future__ import annotations

from typing import Any

from src.core.instantiation.engine import instantiate

from tests.golden.conftest import GoldenCase


def _instantiate_from_case(case: GoldenCase) -> Any:
    """用实例化引擎从黄金用例重建 ItemVersionResult.

    Args:
        case: 已加载校验的黄金用例.

    Returns:
        ItemVersionResult: 引擎实际产出的实例化结果.
    """
    tv_dict = case.template_version.model_dump()
    result = instantiate(
        tv_dict,
        case.params,
        pack_digest=case.pack_digest,
        interaction_id=case.interaction_id,
        scorer_id=case.scorer_id,
        scorer_params=case.scorer_params,
        locale=case.locale,
        corpus_digests=case.corpus_digests,
        seed=case.seed,
    )
    return result


def test_golden_case_item_version_id_matches(golden_case: GoldenCase) -> None:
    """每条黄金用例：实际实例化 item_version_id 必须与 expected 逐字节一致.

    这是 D3（内容寻址）的核心回归断言：同输入必同 id.
    """
    result = _instantiate_from_case(golden_case)
    assert result.item_version_id == golden_case.expected_item_version_id, (
        f"case={golden_case.case_id}: "
        f"actual={result.item_version_id} "
        f"expected={golden_case.expected_item_version_id}"
    )


def test_golden_case_content_matches(golden_case: GoldenCase) -> None:
    """每条黄金用例：实际 content 快照必须与 expected 深相等.

    捕获 id 之外的内容漂移（presentation 插值、干扰项装配等）.
    """
    result = _instantiate_from_case(golden_case)
    actual = result.content
    expected = golden_case.expected_content_snapshot
    assert actual == expected, (
        f"case={golden_case.case_id}: content mismatch\n"
        f"actual={actual}\nexpected={expected}"
    )


def test_golden_cases_count_meets_floor() -> None:
    """黄金用例总数 ≥ 30（T-W2-023 验收 §1）.

    T-W2-023 产出 30 题；T-W2-024 追加至合计 50 题.
    本断言只卡下限 30，T-W2-024 完成后自然满足 50.
    """
    from tests.golden.conftest import discover_golden_case_paths

    paths = discover_golden_case_paths()
    assert len(paths) >= 30, f"黄金用例数 {len(paths)} < 30（T-W2-023 下限）"


def test_golden_cases_cover_at_least_6_interactions() -> None:
    """T-W2-023 验收 §3：覆盖 ≥6 种不同交互类型."""
    from tests.golden.conftest import discover_golden_case_paths, load_golden_case

    interactions: set[str] = set()
    for p in discover_golden_case_paths():
        case = load_golden_case(p)
        interactions.add(case.interaction_id)
    assert len(interactions) >= 6, (
        f"交互类型覆盖 {len(interactions)} 种 < 6：{interactions}"
    )


def test_golden_cases_cover_all_10_active_interactions() -> None:
    """T-W2-024 验收 §1：合计覆盖 10 种现役交互类型.

    10 种现役交互（interaction.yaml status=active）：
      single_choice / multi_choice / text_blank / numeric_blank /
      matching / ordering / short_answer / stepwise_process /
      writing / drawing_operation
    """
    from tests.golden.conftest import discover_golden_case_paths, load_golden_case

    required = {
        "single_choice", "multi_choice", "text_blank", "numeric_blank",
        "matching", "ordering", "short_answer", "stepwise_process",
        "writing", "drawing_operation",
    }
    covered: set[str] = set()
    for p in discover_golden_case_paths():
        case = load_golden_case(p)
        covered.add(case.interaction_id)
    missing = required - covered
    assert not missing, (
        f"未覆盖交互类型 {missing}；已覆盖 {len(covered)}：{sorted(covered)}"
    )


def test_golden_cases_total_at_least_50() -> None:
    """T-W2-024 验收 §3 / E2E-3：50 母题回归全绿（总数 ≥50）."""
    from tests.golden.conftest import discover_golden_case_paths

    paths = discover_golden_case_paths()
    assert len(paths) >= 50, (
        f"黄金用例总数 {len(paths)} < 50（E2E-3 要求）"
    )
