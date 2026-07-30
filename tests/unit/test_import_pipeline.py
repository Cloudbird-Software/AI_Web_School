"""W1-1 (Issue #26): 适配器导入管道单元测试.

验收标准：
1. scripts/import_pack.py --dry-run 在 specs/examples/ 上运行不修改 DB。
2. 两个适配器（JSON/CSV）都能产出合法的 ItemVersionImport 流，schema/pydantic/注册表校验通过。
3. 幂等性逻辑（重复 id skip、新 iv 更新 current_version）在 mock DB 层表现正确。
4. 导入报告 JSON 落盘到 out/import_reports/<timestamp>.json，包含 created/skipped/errors。
"""
from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = PROJECT_ROOT / "specs" / "examples"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_report_dir(tmp_path) -> Path:
    """每个测试独立的报告目录."""
    d = tmp_path / "reports"
    d.mkdir()
    return d


@pytest.fixture
def math_example_json() -> dict[str, Any]:
    return json.loads(
        (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def _three_csv_rows_file(tmp_path) -> Path:
    """一个含 3 条记录的 CSV 临时文件（2 单选 + 1 文本填空）."""
    p = tmp_path / "pack3.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "item_id","pack_id","interaction_id","question_text",
            "options_json","answer_json","gradeband","subject","scorer_id","kp_code",
        ])
        # 1) 数学单选
        w.writerow([
            "math-01","subject-math-L","single_choice","2+3=?",
            json.dumps([{"id":"A","label":"4"},{"id":"B","label":"5"},{"id":"C","label":"6"},{"id":"D","label":"7"}]),
            json.dumps({"selected":"B"}),
            "L","math","exact_match","math.arithmetic.addition",
        ])
        # 2) 语文填空
        w.writerow([
            "cn-01","subject-chinese-L","text_blank","春眠不觉___（填字）",
            "",
            json.dumps({"blanks":{"blank_1":"晓"}}),
            "L","chinese","exact_match","chinese.poem.tang",
        ])
        # 3) 英语单选
        w.writerow([
            "en-01","subject-english-L","single_choice","I ___ a student. (am/is/are)",
            json.dumps([{"id":"A","label":"am"},{"id":"B","label":"is"},{"id":"C","label":"are"}]),
            json.dumps({"selected":"A"}),
            "L","english","exact_match","english.grammar.be_verb",
        ])
    return p


# ────────────────────────────────────────────────────────────────────
# 1. 适配器（JSON/CSV）基本行为
# ────────────────────────────────────────────────────────────────────


class TestJsonAdapter:
    def test_example_dir_yields_three_items(self):
        """specs/examples 目录递归应有 3 条（math/chinese/english 各一）."""
        from src.registry.adapters import JsonAdapter

        a = JsonAdapter()
        items = list(a.iter_items(EXAMPLES_DIR))
        assert len(items) == 3, f"期望 3 条，实际 {len(items)} 条"
        ids = sorted(i.data.get("item_id") for i in items)
        assert ids == sorted([
            "math-item-0001","chinese-item-0001","english-item-0001"
        ])
        # 每条都有 6 大块顶层字段
        for it in items:
            for k in ("item_version_id","item_id","objective","interaction_ref",
                      "content","scoring_ref","error_bindings","lineage"):
                assert k in it.data, f"缺字段 {k} in {it.source}:{it.line}"

    def test_missing_file_records_error(self):
        """不存在的文件 → adapter.errors 记录，不抛异常."""
        from src.registry.adapters import JsonAdapter

        a = JsonAdapter()
        list(a.iter_items("/definitely/not/exist/path_xyz.json"))
        assert len(a.errors) >= 1
        assert "不存在" in a.errors[0].message or "exist" in a.errors[0].message.lower()

    def test_invalid_json_records_error(self, tmp_path):
        """非法 JSON 文件 → adapter.errors 记录，不抛."""
        from src.registry.adapters import JsonAdapter

        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not valid json ]", encoding="utf-8")
        a = JsonAdapter()
        list(a.iter_items(bad))
        assert len(a.errors) >= 1

    def test_single_file_list(self, tmp_path, math_example_json):
        """顶层是数组的 JSON 文件：每条作为独立 item."""
        from src.registry.adapters import JsonAdapter

        arr = [dict(math_example_json, item_id=f"dup-{i}",
                    item_version_id=f"iv-dup-{i}") for i in range(4)]
        f = tmp_path / "many.json"
        f.write_text(json.dumps(arr), encoding="utf-8")
        a = JsonAdapter()
        items = list(a.iter_items(f))
        assert len(items) == 4


class TestCsvAdapter:
    def test_three_rows_valid(self, _three_csv_rows_file):
        from src.registry.adapters import CsvAdapter

        a = CsvAdapter()
        items = list(a.iter_items(_three_csv_rows_file))
        assert len(items) == 3, f"实际 {len(items)}；errors={[(e.message, e.detail) for e in a.errors]}"
        # 每条都有 objective.gradeband、scoring_ref.scorer_id
        for it in items:
            assert it.data["objective"]["gradeband"] in {"L", "M", "H"}
            assert it.data["scoring_ref"]["scorer_id"] in {"exact_match", "keypoint_hit",
                                                             "math_equivalence"}
            assert it.data["interaction_ref"]["interaction_id"]  # 非空

    def test_missing_required_column(self, tmp_path):
        """缺必填列（item_id / interaction_id / question_text / answer_json）→ 报错."""
        from src.registry.adapters import CsvAdapter

        f = tmp_path / "bad.csv"
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            # 故意缺 item_id 和 answer_json
            w.writerow(["interaction_id","question_text"])
            w.writerow(["single_choice","hello"])
        a = CsvAdapter()
        list(a.iter_items(f))
        assert len(a.errors) >= 1
        assert "缺少必填列" in a.errors[0].message

    def test_invalid_answer_json_skipped(self, tmp_path):
        """answer_json 不是合法 JSON → 该行跳过（AdapterError 记录，不抛）."""
        from src.registry.adapters import CsvAdapter

        f = tmp_path / "badans.csv"
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "item_id","pack_id","interaction_id","question_text",
                "options_json","answer_json","gradeband","subject","scorer_id","kp_code",
            ])
            w.writerow(["i1","p","single_choice","q","","NOT JSON AT ALL","L","s","exact_match","k"])
        a = CsvAdapter()
        items = list(a.iter_items(f))
        assert len(items) == 0, f"期望 0 条（answer_json 非法应跳过），实际 {len(items)}"
        assert len(a.errors) == 1
        assert "answer_json 非法" in a.errors[0].message

    def test_default_scorer_guess(self, tmp_path):
        """scorer_id 为空 → 按 interaction_id 猜（short_answer → keypoint_hit）."""
        from src.registry.adapters import CsvAdapter

        f = tmp_path / "g.csv"
        with f.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "item_id","pack_id","interaction_id","question_text",
                "options_json","answer_json","gradeband","subject","scorer_id","kp_code",
            ])
            # scorer_id 空
            w.writerow(["s1","p","short_answer","简答题目","",
                        json.dumps({"keypoints":[]}),"L","s","","k"])
        a = CsvAdapter()
        items = list(a.iter_items(f))
        assert len(items) == 1
        assert items[0].data["scoring_ref"]["scorer_id"] == "keypoint_hit"


# ────────────────────────────────────────────────────────────────────
# 2. run_import: dry-run 对 specs/examples/ 全绿
# ────────────────────────────────────────────────────────────────────


class TestRunImportDryRun:
    @pytest.mark.asyncio
    async def test_examples_dir_all_pass(self, tmp_report_dir):
        """dry-run specs/examples/：3/3 通过，0 失败，0 db_*（非 commit 模式）."""
        from src.registry.importer import run_import

        rpt = await run_import(
            source=EXAMPLES_DIR,
            adapter="json",
            mode="dry-run",
            report_dir=tmp_report_dir,
        )
        assert rpt.total_seen == 3
        assert rpt.validation_passed == 3
        assert rpt.validation_failed == 0
        assert rpt.adapter_errors == []
        # 非 commit 模式下 DB 计数必须全 0
        assert rpt.db_created_item == 0
        assert rpt.db_created_iv == 0
        assert rpt.db_updated_current == 0
        assert rpt.db_skipped_duplicate_iv == 0
        assert rpt.db_skipped_duplicate_item == 0
        # 报告落盘（report_file 字段写回）
        files = list(tmp_report_dir.glob("*.json"))
        assert len(files) == 1
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert saved["report_file"] == str(files[0])
        assert saved["total_seen"] == 3

    @pytest.mark.asyncio
    async def test_bad_interaction_id_fails_validation(self, tmp_report_dir, tmp_path, math_example_json):
        """interaction_id 未注册 → validation_failed 计数+1，写明细."""
        from src.registry.importer import run_import

        bad = dict(math_example_json)
        bad["interaction_ref"]["interaction_id"] = "NONEXISTENT_INTERACTION_XYZ"
        bad["item_id"] = "bad-iid"
        bad["item_version_id"] = "bad-ivid"
        f = tmp_path / "bad.json"
        f.write_text(json.dumps(bad), encoding="utf-8")

        rpt = await run_import(source=f, adapter="json", mode="dry-run",
                               report_dir=tmp_report_dir)
        assert rpt.validation_passed == 0
        assert rpt.validation_failed == 1
        assert rpt.total_seen == 1
        err = rpt.validation_errors[0]
        assert "NONEXISTENT_INTERACTION_XYZ" in err["error"] or "未注册" in err["error"]

    @pytest.mark.asyncio
    async def test_csv_dry_run_three_rows(self, tmp_report_dir, _three_csv_rows_file):
        """CSV 3 条 dry-run：3/3 通过."""
        from src.registry.importer import run_import

        rpt = await run_import(source=_three_csv_rows_file, adapter="csv",
                               mode="dry-run", report_dir=tmp_report_dir)
        assert rpt.total_seen == 3, f"实际 {rpt.total_seen}; errors={rpt.adapter_errors}; valerr={rpt.validation_errors}"
        assert rpt.validation_passed == 3, f"未通过的 errors: {rpt.validation_errors}"
        assert rpt.validation_failed == 0


# ────────────────────────────────────────────────────────────────────
# 3. CLI 行为：退出码/参数
# ────────────────────────────────────────────────────────────────────


class TestCli:
    def test_list_adapters(self):
        import subprocess

        result = subprocess.run(
            ["python", "scripts/import_pack.py", "--list-adapters"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0
        assert "json" in result.stdout and "csv" in result.stdout

    def test_missing_source_exits_1(self):
        import subprocess

        result = subprocess.run(
            ["python", "scripts/import_pack.py", "--adapter", "json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0  # argparse 或 1：缺 source 报错

    def test_nonexistent_source_exits_1(self):
        import subprocess

        result = subprocess.run(
            ["python", "scripts/import_pack.py", "--source", "/no/such/file_xyz.json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 1

    def test_dry_run_examples_returns_0(self, tmp_report_dir):
        """scripts/import_pack.py --source specs/examples --dry-run：退出 0，stderr 含校验通过信息."""
        import subprocess, os

        env = os.environ.copy()
        env["TEMP_REPORT_DIR"] = str(tmp_report_dir)
        result = subprocess.run(
            ["python", "scripts/import_pack.py",
             "--source", "specs/examples",
             "--adapter", "json",
             "--dry-run",
             "--report-dir", str(tmp_report_dir),
             "--no-progress",
             "--quiet"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"stdout={result.stdout}; stderr={result.stderr}"
        # quiet 模式 stdout 仅输出报告文件路径
        assert result.stdout.strip().endswith(".json")
