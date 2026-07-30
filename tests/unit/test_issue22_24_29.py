"""Issues #22/#24/#29 集成测试：CI/monitoring + DB indexes/perf baseline + Paper pipeline.

本文件聚焦「验收级」断言，不依赖数据库（除了 migration 结构检查用 pytest.skip 跳过）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ────────────────────────────────────────────────────────────────────
# #29 Paper generation & rendering pipeline
# ────────────────────────────────────────────────────────────────────


class TestBuildPaperScript:
    """验收 #1/#2/#3：build_paper.py CLI 从 specs/examples 生成 1 份/批量卷，
    确定性（shared_fingerprint）、paper_row/paper_item_rows 与 ORM 对齐。
    """

    def test_dry_run_3_items_ok(self):
        out = _run_build([
            "--subject", "subject-math",
            "--gradeband", "M",
            "--kp-snapshot", "snap_t29_1",
            "--kp", "math.arithmetic.addition",
            "--num-items", "3",
            "--interaction", "single_choice=2",
            "--interaction", "text_blank=1",
            "--title", "T29 One",
            "--seed", "7",
            "--source", "specs/examples",
            "--output", "/tmp/t29_dry",
            "--no-pdf", "--dry-run",
        ])
        assert out.returncode == 0, f"stdout: {out.stdout}\n stderr: {out.stderr}"
        assert b"[DRY-RUN]" in out.stdout or b"[DRY-RUN]" in out.stderr

    def test_3_copies_determinism(self, tmp_path):
        import subprocess
        out_dir = tmp_path / "p"
        res = _run_build([
            "--subject", "subject-chinese",
            "--gradeband", "L",
            "--kp-snapshot", "snap_t29_2",
            "--num-items", "3",
            "--interaction", "single_choice=2",
            "--interaction", "text_blank=1",
            "--title", "T29 Batch",
            "--seed", "13",
            "--source", "specs/examples",
            "--output", str(out_dir),
            "--copies", "3",
            "--no-pdf",
        ])
        assert res.returncode == 0, res.stderr.decode(errors="replace")
        summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["copies"] == 3
        fp = summary["shared_fingerprint"]
        assert isinstance(fp, str) and len(fp) == 16
        # 每一份 meta 的 fingerprint 相同
        for r in summary["results"]:
            meta_path = Path(r["meta"])
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            assert m["fingerprint"] == fp
            # paper_row 对齐 Paper ORM：字段最少集合
            paper = m["paper"]
            for k in ("paper_id", "paper_code", "paper_spec_id", "paper_title",
                      "gradeband", "subject_pack_id", "kp_snapshot_ref",
                      "seed", "created_by"):
                assert k in paper, f"paper_row 缺字段 {k}"
            # paper_item_rows 对齐 PaperItem ORM
            for pi in m["paper_items"]:
                for k in ("paper_item_id", "paper_id", "item_version_id",
                          "placement_token", "item_number", "item_short_code"):
                    assert k in pi, f"paper_item_row 缺字段 {k}"


# ────────────────────────────────────────────────────────────────────
# #24 DB indexes / search / perf baseline
# ────────────────────────────────────────────────────────────────────


class TestIssue24SearchPerf:
    """验收：search_in_pool 三围搜索 + perf_baseline 报告有值.

    - kp 搜索、交互类型搜索、学段搜索三个维度能非空。
    - 1000 行内存基准：4 类 p50 latency 非负整数。
    """

    def test_search_three_dims(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.core.registry.search_service import (
            SearchQuery,
            _generate_random_pool,
            search_in_pool,
        )
        pool = _generate_random_pool(500)
        r_kp = search_in_pool(pool, SearchQuery(kp_codes=["math.arithmetic.addition"],
                                                  limit=500))
        assert 1 <= r_kp.total <= 500
        r_ia = search_in_pool(pool, SearchQuery(
            interaction_ids=["single_choice"], limit=500))
        assert 1 <= r_ia.total <= 500
        r_gb = search_in_pool(pool, SearchQuery(gradebands=["L"], limit=500))
        assert 1 <= r_gb.total <= 500

    def test_perf_baseline_1000(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.core.registry.search_service import perf_baseline_in_memory

        r = asyncio.run(perf_baseline_in_memory(n_items=1000))
        assert r.n_items_inserted >= 1000
        # 4 个指标非负
        for field_name in ("kp_search_latency_ms", "keyword_search_latency_ms",
                           "interaction_filter_latency_ms",
                           "gradeband_filter_latency_ms"):
            val = getattr(r, field_name)
            assert isinstance(val, int) and val >= 0, f"{field_name}={val}"

    def test_alembic_0023_migration_structure(self):
        """0023 文件存在，包含关键索引（文本检查，不跑 DB）."""
        v23 = PROJECT_ROOT / "alembic" / "versions" / "0023_search_indexes_perf_baseline.py"
        assert v23.is_file(), "0023 migration 文件不存在"
        text = v23.read_text(encoding="utf-8")
        for token in ("ix_item_version_item_id_status",
                      "ix_item_version_created_at",
                      "ix_paper_item_item_version_id",
                      "ix_response_event_session_created",
                      "ix_item_param_iv_source_scenario"):
            assert token in text, f"0023 缺索引 {token}"


# ────────────────────────────────────────────────────────────────────
# #22 CI tests & monitoring scaffold
# ────────────────────────────────────────────────────────────────────


class TestIssue22MonitoringAndSmoke:
    """验收：run_smoke_suite 可执行，metrics 落盘，overall_ok=True.

    不强依赖 DB/Redis（默认 require_*=False）。
    """

    def test_smoke_suite_no_deps(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.core.registry.monitoring_scaffold import run_smoke_suite

        r = asyncio.run(run_smoke_suite(require_db=False, require_redis=False,
                                        n_items_baseline=500))
        assert r.overall_ok is True, f"errors: {r.errors}"
        assert r.perf_baseline is not None and r.perf_baseline.get("n_items_inserted") >= 500
        # latency 采样：数字
        assert isinstance(r.search_latency_sample_ms, int)
        # metrics 落盘目录下至少有一个 metrics-*.json 和一个 health-*.json
        metrics_json = sorted(PROJECT_ROOT.joinpath("out", "metrics").glob("metrics-*.json"))
        health_json = sorted(PROJECT_ROOT.joinpath("out", "metrics").glob("health-*.json"))
        assert metrics_json, "metrics 未写出文件"
        assert health_json, "health 未写出文件"
        m = json.loads(metrics_json[-1].read_text(encoding="utf-8"))
        assert "summary" in m and "counters" in m["summary"]
        h = json.loads(health_json[-1].read_text(encoding="utf-8"))
        assert h.get("overall_ok") is True

    def test_metrics_counter_and_timer(self):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.core.registry.monitoring_scaffold import get_metrics_store

        s = get_metrics_store()
        s.incr("t22.counter", 2, env="test")
        s.observe("t22.timer_ms", 123, env="test")
        sm = s.summary()
        any_counter_t22 = any(c["name"] == "t22.counter" for c in sm["counters"])
        assert any_counter_t22
        any_timer_t22 = any(t["name"] == "t22.timer_ms" for t in sm["timers"])
        assert any_timer_t22
        timer = next(t for t in sm["timers"] if t["name"] == "t22.timer_ms")
        assert timer["samples"] >= 1 and timer["p50_ms"] == 123


# ────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────


def _run_build(args: list[str]):
    import subprocess
    return subprocess.run(
        ["python", "scripts/build_paper.py", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=False,
    )
