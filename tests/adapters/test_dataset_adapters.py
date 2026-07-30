"""数据集适配器联合测试：RACE / CMRC / ASSISTments (Issue #27/#28/#30).

验收标准：
- RACE adapter 输出 passage+MCQ 的 item_version，通过 JSON Schema + Pydantic 校验。
- CMRC adapter 输出 passage+简答/MCQ 的 item_version，通过校验。
- ASSISTments adapter：50 行样例 CSV/JSON → item_version 匹配 schema（JSON Schema validator）。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import jsonschema
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "specs" / "item_version_import_schema.json"


@pytest.fixture(scope="module")
def import_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ────────────────────────────────────────────────────────────────────
# RACE (#27)
# ────────────────────────────────────────────────────────────────────

_SAMPLE_RACE = {
    "id": "high1.txt",
    "article": "One day, Tom went to the store. He bought 3 apples and 5 oranges. "
               "On his way home, he ate 1 apple and gave 2 oranges to his friend.",
    "questions": [
        "How many apples did Tom buy?",
        "What did Tom give to his friend?",
        "Where did Tom go?",
    ],
    "options": [
        ["A. 1", "B. 3", "C. 5", "D. 8"],
        ["A. apples", "B. oranges", "C. bananas", "D. money"],
        ["A. school", "B. park", "C. store", "D. library"],
    ],
    "answers": ["B", "B", "C"],
}


class TestRaceAdapter:
    def test_convert_returns_three_items(self, import_schema):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from specs.mappings.race_adapter import convert

        items = convert(_SAMPLE_RACE)
        assert len(items) == 3
        for it in items:
            jsonschema.validate(instance=it, schema=import_schema)
        # 正确答案：每题 scoring_params.answer.selected 匹配原文
        selected = [it["scoring_ref"]["scorer_params"]["answer"]["selected"] for it in items]
        assert selected == ["B", "B", "C"]

    def test_option_prefixes_stripped(self):
        from specs.mappings.race_adapter import _normalize_option

        assert _normalize_option("A. Hello") == "Hello"
        assert _normalize_option("B) World") == "World"
        assert _normalize_option("C、你好") == "你好"
        assert _normalize_option("plain") == "plain"

    def test_iter_items_registers_as_adapter(self, tmp_path):
        """--adapter race 能解析文件."""
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from specs.mappings import race_adapter  # noqa: F401（触发 register_adapter）
        from src.registry.adapters import get_adapter_cls

        Race = get_adapter_cls("race")
        f = tmp_path / "race.json"
        f.write_text(json.dumps(_SAMPLE_RACE), encoding="utf-8")
        items = list(Race().iter_items(f))
        assert len(items) == 3


# ────────────────────────────────────────────────────────────────────
# CMRC (#28)
# ────────────────────────────────────────────────────────────────────

_SAMPLE_CMRC = {
    "context": "小白兔蹦蹦跳跳地来到森林里，看到了五颜六色的花朵。"
               "它最喜欢红色的玫瑰，于是摘下了三朵带回家送给妈妈。",
    "id": "DEV_001",
    "qas": [
        {
            "id": "DEV_001_Q0",
            "question": "小白兔看到了什么？",
            "answers": [
                {"text": "五颜六色的花朵", "answer_start": 14},
                {"text": "花朵", "answer_start": 20},
            ],
        },
        {
            "id": "DEV_001_Q1",
            "question": "小白兔最喜欢什么花？",
            "answers": [
                {"text": "红色的玫瑰", "answer_start": 28},
            ],
        },
    ],
}


class TestCmrcAdapter:
    def test_convert_two_short_answer(self, import_schema):
        from specs.mappings.cmrc_adapter import convert

        items = convert(_SAMPLE_CMRC)
        assert len(items) == 2
        for it in items:
            jsonschema.validate(instance=it, schema=import_schema)
        # 均映射为 short_answer + keypoint_hit
        for it in items:
            assert it["interaction_ref"]["interaction_id"] == "short_answer"
            assert it["scoring_ref"]["scorer_id"] == "keypoint_hit"
            keypoints = it["scoring_ref"]["scorer_params"]["keypoints"]
            assert len(keypoints) >= 1

    def test_mcq_variant(self, import_schema):
        """qa 带 options 时 → single_choice."""
        from specs.mappings.cmrc_adapter import convert

        data = {
            "context": "春天到了，小鸟在树上唱歌。",
            "id": "DEV_MC",
            "qas": [{
                "id": "Q_MC",
                "question": "谁在唱歌？",
                "answers": [{"text": "小鸟", "answer_start": 5}],
                "options": ["小鸟", "小狗", "小猫", "青蛙"],
            }],
        }
        items = convert(data)
        assert len(items) == 1
        jsonschema.validate(instance=items[0], schema=import_schema)
        assert items[0]["interaction_ref"]["interaction_id"] == "single_choice"
        assert items[0]["scoring_ref"]["scorer_id"] == "exact_match"
        # correct = A（小鸟）
        assert items[0]["scoring_ref"]["scorer_params"]["answer"]["selected"] == "A"

    def test_registered_adapter(self, tmp_path):
        from specs.mappings import cmrc_adapter  # noqa: F401
        from src.registry.adapters import get_adapter_cls

        C = get_adapter_cls("cmrc")
        f = tmp_path / "cmrc.json"
        f.write_text(json.dumps(_SAMPLE_CMRC), encoding="utf-8")
        items = list(C().iter_items(f))
        assert len(items) == 2


# ────────────────────────────────────────────────────────────────────
# ASSISTments (#30) — 50 行样例
# ────────────────────────────────────────────────────────────────────


def _gen_50_assistments_rows() -> list[dict]:
    """生成 50 行 ASSISTments 样例（MCQ + 填空 + 开放题混合）."""
    rows = []
    mcq_skills = [
        ("1", "Addition within 20"),
        ("2", "Subtraction within 20"),
        ("3", "Multiplication Basic Facts"),
        ("4", "Division Basic Facts"),
        ("9", "Area of a Rectangle"),
        ("10", "Perimeter"),
        ("25", "One Digit Multiplication"),
        ("27", "Counting Money"),
    ]
    numeric_skills = [
        ("11", "Evaluating Expressions"),
        ("12", "Length Conversion"),
        ("13", "Mass Conversion"),
        ("14", "Time Conversion"),
        ("7", "Adding Fractions"),
    ]
    text_skills = [
        ("16", "One Step Word Problems"),
        ("17", "Two Step Word Problems"),
        ("30", "Multi-step Word Problems"),
    ]
    idx = 1
    # 32 MCQ
    for _ in range(32):
        s = mcq_skills[idx % len(mcq_skills)]
        a_idx = (idx * 3) % 4
        rows.append({
            "problem_id": f"P{idx:04d}",
            "skill_id": s[0],
            "skill_name": s[1],
            "question_text": f"Sample MCQ problem {idx}",
            "answer_type": "mcq",
            "choices": [f"distractor_A_{idx}", f"distractor_B_{idx}",
                        f"correct_{idx}", f"distractor_D_{idx}"],
            "correct_choice_index": a_idx,
            "choice_count": 4,
            "grade_level": 3 + (idx % 4),
        })
        idx += 1
    # 12 numeric
    for _ in range(12):
        s = numeric_skills[idx % len(numeric_skills)]
        rows.append({
            "problem_id": f"P{idx:04d}",
            "skill_id": s[0],
            "skill_name": s[1],
            "question_text": f"Sample numeric problem {idx}",
            "answer_text": str(idx * 2),
            "answer_type": "fill_in",
            "grade_level": 4,
        })
        idx += 1
    # 6 text fill-in
    for _ in range(6):
        s = text_skills[idx % len(text_skills)]
        rows.append({
            "problem_id": f"P{idx:04d}",
            "skill_id": s[0],
            "skill_name": s[1],
            "question_text": f"Sample text problem {idx}",
            "answer_text": f"answer_text_{idx}",
            "answer_type": "open_response",
            "grade_level": 5,
        })
        idx += 1
    assert len(rows) == 50
    return rows


class TestAssistmentsAdapter:
    def test_json_convert_50_rows(self, import_schema):
        from specs.mappings.assistments_adapter import convert

        rows = _gen_50_assistments_rows()
        items = convert(rows)
        # 50 行全部成功转换
        assert len(items) == 50
        for it in items:
            jsonschema.validate(instance=it, schema=import_schema)
        # 类型分布：32 MCQ → single_choice，12 numeric → numeric_blank，6 text → text_blank
        counts = {}
        for it in items:
            k = it["interaction_ref"]["interaction_id"]
            counts[k] = counts.get(k, 0) + 1
        assert counts.get("single_choice") == 32, f"actual counts: {counts}"
        assert counts.get("numeric_blank") == 12, f"actual counts: {counts}"
        assert counts.get("text_blank") == 6, f"actual counts: {counts}"

    def test_kp_map_applied(self):
        from specs.mappings.assistments_adapter import convert, load_kp_map

        mp = load_kp_map()
        rows = [_gen_50_assistments_rows()[0]]
        items = convert(rows, kp_map=mp)
        kp_code = items[0]["objective"]["kp_set"][0]["code"]
        # skill_id="1" → math.arithmetic.addition_within_20（在样例 mp 中）
        assert kp_code.startswith("math.")
        assert kp_code != "math.generic.unknown_skill"

    def test_csv_input(self, tmp_path, import_schema):
        """CSV 输入：通过 Adapter 接口读取 50 行 CSV."""
        from specs.mappings import assistments_adapter  # noqa: F401
        from src.registry.adapters import get_adapter_cls

        rows = _gen_50_assistments_rows()
        f = tmp_path / "ast.csv"
        fieldnames = [
            "problem_id","skill_id","skill_name","question_text","answer_type",
            "choices","correct_choice_index","choice_count","answer_text","grade_level"
        ]
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                rr = dict(r)
                if rr.get("choices"):
                    rr["choices"] = json.dumps(rr["choices"], ensure_ascii=False)
                w.writerow(rr)
        A = get_adapter_cls("assistments")
        items = list(A().iter_items(f))
        assert len(items) == 50, f"errors: {A().errors if A else 'n/a'}"
        for it in items:
            jsonschema.validate(instance=it.data, schema=import_schema)

    def test_import_pipeline_dry_run(self, tmp_path):
        """end-to-end：通过 run_import 跑 50 行 JSON，应全部通过校验."""
        import asyncio
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from specs.mappings import assistments_adapter  # noqa: F401
        from src.registry.importer import run_import

        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        f = tmp_path / "ast50.json"
        f.write_text(json.dumps(_gen_50_assistments_rows(), ensure_ascii=False),
                      encoding="utf-8")
        rpt = asyncio.run(run_import(
            source=f, adapter="assistments", mode="dry-run",
            report_dir=report_dir,
        ))
        assert rpt.total_seen == 50
        assert rpt.validation_passed == 50, (
            f"validation failed for: {rpt.validation_errors[:5]}"
        )
        assert rpt.validation_failed == 0
        # 报告文件落盘
        files = list(report_dir.glob("*.json"))
        assert len(files) >= 1, "run_import 应写出报告 JSON"
