"""T-W4-020 影子模式框架 + 基准验证数据集单元测试.

验收对照：
  §1 ``shadow_score(response, rubric)`` 返回评分结果，写入 ``shadow_score`` 表，
     不触碰 ``response_event`` 主 score 字段。
  §2 基准数据集 20 篇：含不同学段/主题的作文/看图写话模拟作答，每篇附人工量规结论。
  §3 一致率：AI 量规与人工结论逐维偏差≤1 分视为一致，整体一致率≥70%。
  §4 ``make accept TASK=T-W4-020`` 全绿。
  §5 不 import 任何学科包/学段包。

测试不消耗真实 LLM API：``_SequenceMockLLMClient`` 按 case 顺序返回预设 JSON
（与人工结论对齐 → 100% 一致 → 通过 ≥70% 门槛）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.ai.bus.models import AIResult
from src.core.production.rubric_template import RubricTemplate
from src.core.scoring.shadow_mode import (
    DEFAULT_CONSISTENCY_RATE_THRESHOLD,
    DEFAULT_CONSISTENCY_TOLERANCE,
    DEFAULT_DATASET_PATH,
    ShadowScoreRecord,
    benchmark_against_dataset,
    compute_consistency,
    load_shadow_dataset,
    persist_shadow_score,
    shadow_score,
)
from src.core.scoring.rubric_parser import AIRubricScore, AIRubricScoreDimension

# ────────────────────────────────────────────────────────────────────
# 路径常量与数据集加载
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATASET_PATH = _PROJECT_ROOT / "tests" / "golden" / "shadow_dataset.json"


def _load_dataset() -> dict[str, Any]:
    """加载基准数据集（模块级缓存避免重复 IO）."""
    with _DATASET_PATH.open(encoding="utf-8") as f:
        return json.load(f)


_DATASET = _load_dataset()


# ────────────────────────────────────────────────────────────────────
# Mock LLM 客户端：按 case 顺序返回 AI JSON 响应
# ────────────────────────────────────────────────────────────────────


class _SequenceMockLLMClient:
    """按调用顺序返回预设 AI JSON 响应的 mock 客户端.

    ``benchmark_against_dataset`` 按 cases 顺序调用 ``shadow_score``，每次触发
    一次 LLM 调用；本 mock 第 i 次调用返回第 i 个 case 的「人工结论作为 AI 响应」，
    模拟「AI 与人工完全一致」场景 → 100% 一致率（验收③ ≥70% 通过）。

    也可传入 ``offset`` 列表故意制造偏差，用于测试不一致路径。
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> AIResult:
        idx = self._idx
        self._idx += 1
        content = self._responses[idx] if idx < len(self._responses) else "{}"
        self.calls.append(
            {"prompt": prompt, "model": model, "call_idx": idx}
        )
        return AIResult(
            content=content,
            model=model,
            token_in=len(prompt),
            token_out=len(content),
            duration_ms=0.5,
        )


def _ai_json_from_human(human_score: dict[str, Any]) -> str:
    """把人工结论转为 AI JSON 响应（模拟 AI 与人工一致）.

    人工结论 ``{dimensions:[{id, score}]}`` → AI JSON ``{dimensions:[{id, score,
    rationale, confidence}]}``，rationale 非空、confidence 高（0.9）。
    """
    dims = []
    for hd in human_score.get("dimensions", []):
        dims.append(
            {
                "id": hd["id"],
                "score": hd["score"],
                "rationale": f"与人工结论一致：{hd['id']} 得 {hd['score']} 分",
                "confidence": 0.9,
            }
        )
    return json.dumps({"dimensions": dims}, ensure_ascii=False)


def _build_consistent_mock(dataset: dict[str, Any]) -> _SequenceMockLLMClient:
    """构造「全一致」mock：每个 case 返回其人工结论作为 AI 响应."""
    responses = [
        _ai_json_from_human(c["human_score"]) for c in dataset["cases"]
    ]
    return _SequenceMockLLMClient(responses)


# ────────────────────────────────────────────────────────────────────
# 共享 fixture
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def dataset() -> dict[str, Any]:
    """基准数据集 dict."""
    return _DATASET


@pytest.fixture
def rubric_dict(dataset: dict[str, Any]) -> dict[str, Any]:
    """数据集中的量规（dict 形态，对齐 scorer.yaml params_schema.rubric）."""
    return dataset["rubric"]


# ────────────────────────────────────────────────────────────────────
# §2 基准数据集结构验收
# ────────────────────────────────────────────────────────────────────


class TestDatasetStructure:
    """验收 §2：20 篇含不同学段/主题的作文/看图写话 + 人工量规结论."""

    def test_dataset_has_20_cases(self, dataset: dict) -> None:
        """基准数据集含 20 篇（验收②约定）."""
        assert len(dataset["cases"]) == 20

    def test_dataset_covers_three_grade_bands(self, dataset: dict) -> None:
        """覆盖低/中/高三学段（验收②：不同学段）."""
        bands = {c["grade_band"] for c in dataset["cases"]}
        assert bands == {"L", "M", "H"}

    def test_dataset_covers_two_writing_types(self, dataset: dict) -> None:
        """覆盖作文与看图写话两类（验收②）."""
        types = {c["writing_type"] for c in dataset["cases"]}
        assert types == {"composition", "picture_writing"}

    def test_each_case_has_human_score(self, dataset: dict) -> None:
        """每篇附人工量规结论（逐维分数，验收②）."""
        rubric_dim_ids = [d["id"] for d in dataset["rubric"]["dimensions"]]
        for case in dataset["cases"]:
            assert "response_text" in case and case["response_text"]
            assert "human_score" in case
            dims = case["human_score"]["dimensions"]
            assert len(dims) == len(rubric_dim_ids)
            for dim, rid in zip(dims, rubric_dim_ids):
                assert dim["id"] == rid, (
                    f"{case['case_id']} 维度顺序错：{dim['id']} != {rid}"
                )
                assert isinstance(dim["score"], (int, float))

    def test_rubric_is_valid_scorer_params(self, rubric_dict: dict) -> None:
        """量规可被 parse_rubric 解析（对齐 scorer.yaml params_schema）."""
        from src.core.scoring.rubric_parser import parse_rubric

        parsed = parse_rubric(rubric_dict)
        assert len(parsed.dimensions) == 4
        assert parsed.total_max_score == 20
        for dim in parsed.dimensions:
            assert dim.max_score == 5
            assert len(dim.anchors) >= 2  # 等级描述非空
            assert len(dim.score_bands) >= 2

    def test_load_shadow_dataset_returns_dict(self) -> None:
        """load_shadow_dataset 加载默认路径."""
        d = load_shadow_dataset()
        assert d["dataset_id"] == "shadow-baseline-v1"
        assert len(d["cases"]) == 20

    def test_load_shadow_dataset_rejects_bad_structure(
        self, tmp_path: Path
    ) -> None:
        """load_shadow_dataset 对非法结构抛 ValueError."""
        bad = tmp_path / "bad.json"
        bad.write_text('{"cases": []}', encoding="utf-8")
        with pytest.raises(ValueError):
            load_shadow_dataset(bad)

    def test_load_shadow_dataset_rejects_missing_file(
        self, tmp_path: Path
    ) -> None:
        """load_shadow_dataset 对不存在文件抛 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_shadow_dataset(tmp_path / "nope.json")


# ────────────────────────────────────────────────────────────────────
# §1 shadow_score 返回 ShadowScoreRecord，不触碰 response_event
# ────────────────────────────────────────────────────────────────────


class TestShadowScore:
    """验收 §1：shadow_score 返回记录，不写入 response_event."""

    def test_shadow_score_returns_record(
        self, rubric_dict: dict, dataset: dict
    ) -> None:
        """shadow_score 返回 ShadowScoreRecord（含 ai_score 与一致性状态）."""
        case = dataset["cases"][0]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            writing_type=case["writing_type"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
            human_score=case["human_score"],
        )
        assert isinstance(record, ShadowScoreRecord)
        assert record.case_id == case["case_id"]
        assert record.dataset_id == "shadow-baseline-v1"
        assert record.grade_band == case["grade_band"]
        assert record.writing_type == case["writing_type"]
        assert record.response_text == case["response_text"]
        assert record.response_text_digest.startswith("sha256:")
        assert record.shadow_id.startswith("sha256:")
        assert isinstance(record.ai_score, AIRubricScore)
        # 与人工一致 → consistency_status='consistent'
        assert record.consistency_status == "consistent"

    def test_shadow_score_pending_without_human(
        self, rubric_dict: dict, dataset: dict
    ) -> None:
        """未提供 human_score 时 consistency_status='pending'."""
        case = dataset["cases"][0]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert record.consistency_status == "pending"
        assert record.human_score is None

    def test_shadow_score_id_deterministic(
        self, rubric_dict: dict, dataset: dict
    ) -> None:
        """同输入派生同 shadow_id（确定性，便于重放与去重）."""
        case = dataset["cases"][1]
        kwargs = dict(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            writing_type=case["writing_type"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": _SequenceMockLLMClient(
                [_ai_json_from_human(case["human_score"])]
            )},
            bypass_pii_filter=True,
        )
        r1 = shadow_score(**kwargs)
        kwargs["clients"] = {"deepseek": _SequenceMockLLMClient(
            [_ai_json_from_human(case["human_score"])]
        )}
        r2 = shadow_score(**kwargs)
        assert r1.shadow_id == r2.shadow_id

    def test_shadow_score_does_not_touch_response_event(
        self, rubric_dict: dict, dataset: dict
    ) -> None:
        """验收①：shadow_score 是纯函数，不写 response_event（无 DB 副作用）."""
        case = dataset["cases"][0]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        # shadow_score 不接受 db 参数，物理上无法写 response_event
        import inspect

        sig = inspect.signature(shadow_score)
        assert "db" not in sig.parameters
        assert "session" not in sig.parameters
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert record.ai_score.total_max == 20


# ────────────────────────────────────────────────────────────────────
# §1 persist_shadow_score 写入 shadow_score 表 + append-only
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_shadow_score(async_session: AsyncSession):
    """每测试前清空 shadow_score（TRUNCATE 不触发 append-only 触发器）."""
    await async_session.execute(
        text("TRUNCATE TABLE shadow_score RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield


class TestPersistShadowScore:
    """验收 §1：persist_shadow_score 写 shadow_score 表 + append-only 强制."""

    async def test_persist_writes_row(
        self, async_session: AsyncSession, rubric_dict: dict, dataset: dict
    ) -> None:
        """persist_shadow_score 写入一行，可回读."""
        case = dataset["cases"][0]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            writing_type=case["writing_type"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
            human_score=case["human_score"],
        )
        await persist_shadow_score(async_session, record)
        await async_session.commit()

        row = (
            await async_session.execute(
                text(
                    "SELECT shadow_id, dataset_id, case_id, rubric_id, grade_band,"
                    " writing_type, response_text_digest, overall_confidence,"
                    " needs_human_review, consistency_status"
                    " FROM shadow_score WHERE shadow_id = :sid"
                ),
                {"sid": record.shadow_id},
            )
        ).one()
        d = row._mapping
        assert d["shadow_id"] == record.shadow_id
        assert d["dataset_id"] == "shadow-baseline-v1"
        assert d["case_id"] == case["case_id"]
        assert d["grade_band"] == case["grade_band"]
        assert d["writing_type"] == case["writing_type"]
        assert d["response_text_digest"] == record.response_text_digest
        assert d["consistency_status"] == "consistent"
        assert d["overall_confidence"] >= 0.0
        assert d["needs_human_review"] is False

    async def test_update_rejected_by_trigger(
        self, async_session: AsyncSession, rubric_dict: dict, dataset: dict
    ) -> None:
        """append-only：UPDATE shadow_score 被触发器拒绝（D1 物理强制）."""
        case = dataset["cases"][2]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        await persist_shadow_score(async_session, record)
        await async_session.commit()

        with pytest.raises(Exception, match="append-only"):
            await async_session.execute(
                text("UPDATE shadow_score SET consistency_status = 'inconsistent'")
            )
            await async_session.commit()
        await async_session.rollback()

    async def test_delete_rejected_by_trigger(
        self, async_session: AsyncSession, rubric_dict: dict, dataset: dict
    ) -> None:
        """append-only：DELETE shadow_score 被触发器拒绝（D1 物理强制）."""
        case = dataset["cases"][3]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        await persist_shadow_score(async_session, record)
        await async_session.commit()

        with pytest.raises(Exception, match="append-only"):
            await async_session.execute(text("DELETE FROM shadow_score"))
            await async_session.commit()
        await async_session.rollback()

    async def test_duplicate_insert_rejected_by_pk(
        self, async_session: AsyncSession, rubric_dict: dict, dataset: dict
    ) -> None:
        """同 shadow_id 重复 INSERT 被 PK 拒绝（content-addressed 不可覆盖）."""
        case = dataset["cases"][4]
        client = _SequenceMockLLMClient([_ai_json_from_human(case["human_score"])])
        record = shadow_score(
            response_text=case["response_text"],
            rubric=rubric_dict,
            grade_band=case["grade_band"],
            case_id=case["case_id"],
            dataset_id=dataset["dataset_id"],
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        await persist_shadow_score(async_session, record)
        await async_session.commit()

        with pytest.raises(Exception):
            await persist_shadow_score(async_session, record)
            await async_session.commit()
        await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §3 一致性计算
# ────────────────────────────────────────────────────────────────────


class TestComputeConsistency:
    """验收 §3：逐维偏差≤1 分视为一致."""

    def _make_ai_score(
        self, scores: list[float], total_max: float = 20.0
    ) -> AIRubricScore:
        dims = [
            AIRubricScoreDimension(
                name=f"dim{i}", score=s, max=5.0,
                rationale="测试", confidence=0.9,
            )
            for i, s in enumerate(scores)
        ]
        return AIRubricScore(
            dimensions=dims,
            total_score=sum(scores),
            total_max=total_max,
            overall_confidence=0.9,
            needs_human_review=False,
        )

    def test_all_dimensions_within_tolerance(self) -> None:
        """所有维度偏差≤1 → 一致."""
        ai = self._make_ai_score([5, 4, 4, 3])
        human = {"dimensions": [
            {"id": "content", "score": 5},
            {"id": "structure", "score": 5},
            {"id": "language", "score": 3},
            {"id": "handwriting", "score": 4},
        ]}
        # 偏差：0, 1, 1, 1 → 全 ≤1 → 一致
        assert compute_consistency(ai, human) is True

    def test_one_dimension_exceeds_tolerance(self) -> None:
        """任一维度偏差>1 → 不一致."""
        ai = self._make_ai_score([5, 4, 4, 3])
        human = {"dimensions": [
            {"id": "content", "score": 5},
            {"id": "structure", "score": 4},
            {"id": "language", "score": 1},  # 偏差 3 > 1
            {"id": "handwriting", "score": 3},
        ]}
        assert compute_consistency(ai, human) is False

    def test_missing_human_dimension_inconsistent(self) -> None:
        """人工结论缺维度 → 不一致."""
        ai = self._make_ai_score([5, 4, 4, 3])
        human = {"dimensions": [
            {"id": "content", "score": 5},
            {"id": "structure", "score": 4},
        ]}
        assert compute_consistency(ai, human) is False

    def test_tolerance_threshold_boundary(self) -> None:
        """偏差恰等于 tolerance → 一致（≤ tolerance）."""
        ai = self._make_ai_score([5, 3])
        human = {"dimensions": [
            {"id": "content", "score": 4},  # 偏差 1
            {"id": "structure", "score": 4},  # 偏差 1
        ]}
        assert compute_consistency(ai, human, tolerance=1.0) is True
        # tolerance=0.5 时偏差 1 > 0.5 → 不一致
        assert compute_consistency(ai, human, tolerance=0.5) is False


# ────────────────────────────────────────────────────────────────────
# §3 基准数据集一致率 ≥ 70%
# ────────────────────────────────────────────────────────────────────


class TestBenchmarkAgainstDataset:
    """验收 §3：AI 量规与人工结论整体一致率≥70%."""

    def test_full_consistency_passes_threshold(self, dataset: dict) -> None:
        """全一致 mock → 100% 一致率 → 通过 ≥70% 门槛."""
        client = _build_consistent_mock(dataset)
        report = benchmark_against_dataset(
            dataset,
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert report.total_cases == 20
        assert report.consistency_rate == pytest.approx(1.0)
        assert report.consistent_cases == 20
        assert report.passed is True
        assert report.consistency_rate >= DEFAULT_CONSISTENCY_RATE_THRESHOLD

    def test_low_consistency_fails_threshold(self, dataset: dict) -> None:
        """全偏差 mock → 0% 一致率 → 不通过门槛."""
        # 每个 case 返回全 1 分（与大部分人工结论偏差>1）
        responses = []
        for case in dataset["cases"]:
            dims = [
                {"id": d["id"], "score": 1, "rationale": "偏差测试",
                 "confidence": 0.9}
                for d in dataset["rubric"]["dimensions"]
            ]
            responses.append(json.dumps({"dimensions": dims}, ensure_ascii=False))
        client = _SequenceMockLLMClient(responses)
        report = benchmark_against_dataset(
            dataset,
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert report.consistency_rate < DEFAULT_CONSISTENCY_RATE_THRESHOLD
        assert report.passed is False

    def test_partial_consistency_70_percent(self, dataset: dict) -> None:
        """70% 一致（14/20）→ 刚好通过门槛."""
        responses = []
        for i, case in enumerate(dataset["cases"]):
            if i < 14:
                # 前 14 个一致
                responses.append(_ai_json_from_human(case["human_score"]))
            else:
                # 后 6 个全偏差（score=1，与人工偏差>1）
                dims = [
                    {"id": d["id"], "score": 1, "rationale": "偏差",
                     "confidence": 0.9}
                    for d in dataset["rubric"]["dimensions"]
                ]
                responses.append(json.dumps({"dimensions": dims}, ensure_ascii=False))
        client = _SequenceMockLLMClient(responses)
        report = benchmark_against_dataset(
            dataset,
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert report.consistent_cases == 14
        assert report.consistency_rate == pytest.approx(0.70)
        assert report.passed is True  # ≥ 0.70

    def test_per_case_detail_populated(self, dataset: dict) -> None:
        """报告含逐 case 一致性详情（含维度对比）."""
        client = _build_consistent_mock(dataset)
        report = benchmark_against_dataset(
            dataset,
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert len(report.per_case) == 20
        for case_result in report.per_case:
            assert case_result.case_id
            assert len(case_result.dimensions) == 4
            assert case_result.consistent is True
            assert case_result.max_delta <= DEFAULT_CONSISTENCY_TOLERANCE

    def test_report_serializable(self, dataset: dict) -> None:
        """报告可序列化为 dict（落库/展示用）."""
        client = _build_consistent_mock(dataset)
        report = benchmark_against_dataset(
            dataset,
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        d = report.to_dict()
        assert d["total_cases"] == 20
        assert d["passed"] is True
        assert "per_case" in d and len(d["per_case"]) == 20
        # 可 JSON 序列化
        json.dumps(d, ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────
# §5 不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """验收 §5：src/core/scoring/ 禁止 import 学科包/学段包（宪法 A5/X6）."""

    def test_shadow_mode_no_packs_import(self) -> None:
        """shadow_mode.py 不 import 学科包/学段包."""
        scoring_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "scoring"
        )
        assert scoring_dir.is_dir()
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(scoring_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(scoring_dir)))
        assert not violations, (
            f"core/scoring 存在学科包 import（违反 A5）：{violations}"
        )

    def test_default_dataset_path_points_to_golden(self) -> None:
        """DEFAULT_DATASET_PATH 指向 tests/golden/shadow_dataset.json."""
        assert DEFAULT_DATASET_PATH.name == "shadow_dataset.json"
        assert "golden" in str(DEFAULT_DATASET_PATH)
        assert DEFAULT_DATASET_PATH.is_file()
