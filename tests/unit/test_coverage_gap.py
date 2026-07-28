"""T-W4-005 覆盖缺口盘点五轴热力图数据测试.

覆盖任务卡验收 §1-§5：
  §1 compute_coverage_gap(profile, snapshot_id) 返回五轴缺口矩阵，
     含目标配比/现有量/缺口数/覆盖率百分比。
  §2 至少支持数学 3-4 年级首批图谱维度（~400 节点级）的盘点。
  §3 输出格式可被外部排期工具消费（JSON/CSV 导出接口）。
  §4 make accept TASK=T-W4-005 全绿。
  §5 不 import 任何学科包/学段包；学科维度通过参数注入。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.coverage_gap import (
    PURPOSES,
    CoverageCell,
    CoverageGapMatrix,
    CoverageTarget,
    build_profile_from_grid,
    compute_coverage_gap,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：清表 + 插入 item/item_version/item_kp
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    """每测试前清空相关表."""
    await async_session.execute(text("TRUNCATE TABLE item_kp RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item_version CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item CASCADE"))
    await async_session.commit()
    yield


async def _insert_published_item(
    db: AsyncSession,
    *,
    item_id: str,
    item_version_id: str,
    pack_id: str = "subject-math",
    kp_code: str = "math.nal.decimal.compare",
    cognitive_level: str = "apply",
    gradeband: str = "M",
) -> None:
    """插入已发布 item + item_version + item_kp（最小结构供覆盖盘点取数）."""
    await db.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, :pid, 'C')"
            " ON CONFLICT (item_id) DO NOTHING"
        ),
        {"iid": item_id, "pid": pack_id},
    )
    objective = {
        "kp_set": [{"dimension": "kp", "code": kp_code}],
        "kp_set_mode": "single",
        "cognitive_level": cognitive_level,
        "gradeband": gradeband,
        "graph_release": "2026.1",
    }
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage,"
            " rendered_snapshot, gate_certificate_id, published_at)"
            " VALUES (:vid, :iid, 'published', CAST(:obj AS jsonb),"
            " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, 'gate-test', now())"
            " ON CONFLICT (item_version_id) DO NOTHING"
        ),
        {
            "vid": item_version_id, "iid": item_id,
            "obj": json.dumps(objective, ensure_ascii=False),
        },
    )
    await db.execute(
        text(
            "INSERT INTO item_kp (item_id, item_version_id, dimension, kp_code, gradeband)"
            " VALUES (:iid, :vid, 'kp', :kp, :gb)"
        ),
        {"iid": item_id, "vid": item_version_id, "kp": kp_code, "gb": gradeband},
    )
    await db.commit()


def _target(
    *, kp="math.nal.decimal.compare", cog="apply", purpose="practice",
    grade="M", subject="subject-math", target=10,
) -> CoverageTarget:
    """构造单个 CoverageTarget（测试辅助）."""
    return CoverageTarget(
        kp_code=kp, cognitive_level=cog, purpose=purpose,
        gradeband=grade, subject=subject, target=target,
    )


# ════════════════════════════════════════════════════════════════════
# §1 compute_coverage_gap 返回五轴缺口矩阵
# ════════════════════════════════════════════════════════════════════


class TestComputeCoverageGap:
    """§1 返回含 target/actual/gap/coverage_pct 的缺口矩阵."""

    async def test_returns_matrix_with_summary(self, async_session):
        """返回 CoverageGapMatrix 含 cells + 全局汇总."""
        # 插入 5 道已发布题（同一 kp/cognitive/grade/subject）
        for i in range(5):
            await _insert_published_item(
                async_session,
                item_id=f"item-cg-{i}",
                item_version_id=f"sha256:cg-iv-{i}",
            )

        profile = [_target(target=10)]
        matrix = await compute_coverage_gap(async_session, profile, snapshot_id="snap-1")

        assert isinstance(matrix, CoverageGapMatrix)
        assert matrix.snapshot_id == "snap-1"
        assert len(matrix.cells) == 1
        assert matrix.total_target == 10
        assert matrix.total_actual == 5
        assert matrix.total_gap == 5
        assert matrix.overall_coverage_pct == 50.0

    async def test_cell_fields_correct(self, async_session):
        """单元格字段：target/actual/gap/coverage_pct 正确."""
        await _insert_published_item(
            async_session, item_id="item-cf", item_version_id="sha256:cf-iv",
        )
        profile = [_target(target=10)]
        matrix = await compute_coverage_gap(async_session, profile)

        cell = matrix.cells[0]
        assert cell.kp_code == "math.nal.decimal.compare"
        assert cell.cognitive_level == "apply"
        assert cell.purpose == "practice"
        assert cell.gradeband == "M"
        assert cell.subject == "subject-math"
        assert cell.target == 10
        assert cell.actual == 1
        assert cell.gap == 9
        assert cell.coverage_pct == 10.0

    async def test_gap_zero_when_actual_exceeds_target(self, async_session):
        """actual > target → gap=0, coverage_pct=100（不报超额）."""
        for i in range(15):
            await _insert_published_item(
                async_session,
                item_id=f"item-exc-{i}",
                item_version_id=f"sha256:exc-iv-{i}",
            )
        profile = [_target(target=10)]
        matrix = await compute_coverage_gap(async_session, profile)

        cell = matrix.cells[0]
        assert cell.actual == 15
        assert cell.gap == 0
        assert cell.coverage_pct == 100.0
        # total_actual 不超过 total_target（实际贡献不超过需求）
        assert matrix.total_actual == 10

    async def test_zero_target_coverage_100(self, async_session):
        """target=0 → coverage_pct=100（无需求即满足），gap=0."""
        profile = [_target(target=0)]
        matrix = await compute_coverage_gap(async_session, profile)

        cell = matrix.cells[0]
        assert cell.gap == 0
        assert cell.coverage_pct == 100.0

    async def test_no_actual_items(self, async_session):
        """无已发布题 → actual=0, gap=target, coverage_pct=0."""
        profile = [_target(target=8)]
        matrix = await compute_coverage_gap(async_session, profile)

        cell = matrix.cells[0]
        assert cell.actual == 0
        assert cell.gap == 8
        assert cell.coverage_pct == 0.0
        assert matrix.overall_coverage_pct == 0.0

    async def test_purpose_uses_same_item_pool(self, async_session):
        """用途是目标侧维度：同一题池服务全部用途（actual 不随 purpose 变）."""
        for i in range(5):
            await _insert_published_item(
                async_session,
                item_id=f"item-pp-{i}",
                item_version_id=f"sha256:pp-iv-{i}",
            )
        # 三用途各 10 题需求
        profile = [
            _target(purpose="practice", target=10),
            _target(purpose="diagnosis", target=10),
            _target(purpose="measurement", target=10),
        ]
        matrix = await compute_coverage_gap(async_session, profile)

        assert len(matrix.cells) == 3
        # 三用途的 actual 都是 5（同一题池）
        for cell in matrix.cells:
            assert cell.actual == 5
            assert cell.gap == 5
        # 汇总：需求 30，实际贡献 15（每用途贡献 5）
        assert matrix.total_target == 30
        assert matrix.total_actual == 15
        assert matrix.overall_coverage_pct == 50.0

    async def test_different_kp_counted_separately(self, async_session):
        """不同知识点的题量分别计数."""
        await _insert_published_item(
            async_session, item_id="item-kp1", item_version_id="sha256:kp1-iv",
            kp_code="math.nal.decimal.compare",
        )
        await _insert_published_item(
            async_session, item_id="item-kp2", item_version_id="sha256:kp2-iv",
            kp_code="math.nal.int.mul",
        )
        await _insert_published_item(
            async_session, item_id="item-kp2b", item_version_id="sha256:kp2b-iv",
            kp_code="math.nal.int.mul",
        )
        profile = [
            _target(kp="math.nal.decimal.compare", target=5),
            _target(kp="math.nal.int.mul", target=5),
        ]
        matrix = await compute_coverage_gap(async_session, profile)

        cells = {c.kp_code: c for c in matrix.cells}
        assert cells["math.nal.decimal.compare"].actual == 1
        assert cells["math.nal.int.mul"].actual == 2

    async def test_different_cognitive_grade_subject_filtered(self, async_session):
        """认知层级/学段/学科不同 → 分别计数（不串）."""
        await _insert_published_item(
            async_session, item_id="item-a", item_version_id="sha256:a",
            cognitive_level="apply", gradeband="M", pack_id="subject-math",
        )
        await _insert_published_item(
            async_session, item_id="item-b", item_version_id="sha256:b",
            cognitive_level="remember", gradeband="M", pack_id="subject-math",
        )
        await _insert_published_item(
            async_session, item_id="item-c", item_version_id="sha256:c",
            cognitive_level="apply", gradeband="L", pack_id="subject-math",
        )
        await _insert_published_item(
            async_session, item_id="item-d", item_version_id="sha256:d",
            cognitive_level="apply", gradeband="M", pack_id="subject-chinese",
        )
        profile = [
            _target(cog="apply", grade="M", subject="subject-math", target=5),
            _target(cog="remember", grade="M", subject="subject-math", target=5),
            _target(cog="apply", grade="L", subject="subject-math", target=5),
            _target(cog="apply", grade="M", subject="subject-chinese", target=5),
        ]
        matrix = await compute_coverage_gap(async_session, profile)

        cells = matrix.cells
        assert cells[0].actual == 1  # apply/M/math
        assert cells[1].actual == 1  # remember/M/math
        assert cells[2].actual == 1  # apply/L/math
        assert cells[3].actual == 1  # apply/M/chinese

    async def test_invalid_purpose_raises(self, async_session):
        """非法 purpose 抛 ValueError（D5 场景域约束）."""
        profile = [_target(purpose="invalid")]
        with pytest.raises(ValueError, match="非法 purpose"):
            await compute_coverage_gap(async_session, profile)

    async def test_negative_target_raises(self, async_session):
        """负 target 抛 ValueError."""
        profile = [_target(target=-1)]
        with pytest.raises(ValueError, match="target 不能为负"):
            await compute_coverage_gap(async_session, profile)

    async def test_empty_profile_returns_empty_matrix(self, async_session):
        """空 profile → 空矩阵（不报错）."""
        matrix = await compute_coverage_gap(async_session, [])
        assert len(matrix.cells) == 0
        assert matrix.total_target == 0
        assert matrix.overall_coverage_pct == 100.0  # 无需求即满足

    async def test_gap_cells_sorted_by_gap_desc(self, async_session):
        """gap_cells() 返回 gap>0 的单元格按 gap 降序."""
        await _insert_published_item(
            async_session, item_id="item-g1", item_version_id="sha256:g1",
            kp_code="kp.full",  # actual=1
        )
        # kp.full: target=10, gap=9; kp.empty: target=10, gap=10（无题）
        profile = [
            _target(kp="kp.full", target=10),
            _target(kp="kp.empty", target=10),
        ]
        matrix = await compute_coverage_gap(async_session, profile)

        gaps = matrix.gap_cells()
        assert len(gaps) == 2
        assert gaps[0].gap >= gaps[1].gap
        assert gaps[0].kp_code == "kp.empty"  # gap=10 排前


# ════════════════════════════════════════════════════════════════════
# §2 ~400 节点级盘点
# ════════════════════════════════════════════════════════════════════


class TestLargeScaleProfile:
    """§2 支持数学 3-4 年级首批图谱维度（~400 节点级）."""

    async def test_400_node_profile_handled(self, async_session):
        """~400 节点级 profile 单次查询完成（不逐节点查询）."""
        # 模拟数学 3-4 年级首批图谱：~45 kp × 3 cognitive × 1 grade × 3 purpose ≈ 405
        kp_codes = [f"math.nal.kp.{i:03d}" for i in range(45)]
        profile = build_profile_from_grid(
            kp_codes=kp_codes,
            cognitive_levels=["remember", "understand", "apply"],
            gradebands=["M"],
            subject="subject-math",
            target_per_purpose={"practice": 10, "diagnosis": 5, "measurement": 3},
        )
        assert len(profile) == 45 * 3 * 3  # 405 节点

        matrix = await compute_coverage_gap(async_session, profile, snapshot_id="snap-400")

        assert len(matrix.cells) == 405
        # 405 单元格 = 135 practice×10 + 135 diagnosis×5 + 135 measurement×3
        # 每单元格只绑定一个 purpose（build_profile_from_grid 笛卡尔积含 purpose 维度）
        assert matrix.total_target == 135 * (10 + 5 + 3)  # = 2430
        # 全部缺口（无已发布题）
        assert matrix.total_gap == matrix.total_target
        assert matrix.overall_coverage_pct == 0.0

    async def test_partial_coverage_400_nodes(self, async_session):
        """~400 节点级 profile 部分覆盖（插入部分题）."""
        kp_codes = [f"math.nal.kp.{i:03d}" for i in range(45)]
        # 前 10 个 kp 各插 5 题（apply/M/math）
        for i in range(10):
            for j in range(5):
                await _insert_published_item(
                    async_session,
                    item_id=f"item-400-{i}-{j}",
                    item_version_id=f"sha256:400-{i}-{j}",
                    kp_code=kp_codes[i],
                    cognitive_level="apply",
                    gradeband="M",
                )
        profile = build_profile_from_grid(
            kp_codes=kp_codes,
            cognitive_levels=["apply"],
            gradebands=["M"],
            subject="subject-math",
            target_per_purpose={"practice": 10, "diagnosis": 5, "measurement": 3},
        )
        assert len(profile) == 45 * 3  # 135

        matrix = await compute_coverage_gap(async_session, profile)

        # 前 10 个 kp 的 apply/M 实际 5 题
        covered_cells = [c for c in matrix.cells if c.actual > 0]
        assert len(covered_cells) == 30  # 10 kp × 3 purpose
        for c in covered_cells:
            assert c.actual == 5


# ════════════════════════════════════════════════════════════════════
# §3 JSON/CSV 导出接口
# ════════════════════════════════════════════════════════════════════


class TestExportFormats:
    """§3 输出格式可被外部排期工具消费（JSON/CSV）."""

    async def test_to_dict_json_serializable(self, async_session):
        """to_dict 返回 JSON-serializable dict."""
        await _insert_published_item(
            async_session, item_id="item-ex", item_version_id="sha256:ex-iv",
        )
        profile = [_target(target=5)]
        matrix = await compute_coverage_gap(async_session, profile)

        d = matrix.to_dict()
        # 可被 json.dumps 序列化（无 Decimal/datetime 等不可序列化对象）
        json_str = json.dumps(d, ensure_ascii=False)
        assert "cells" in json_str
        assert "summary" in json_str
        parsed = json.loads(json_str)
        assert parsed["summary"]["total_target"] == 5
        assert parsed["cells"][0]["kp_code"] == "math.nal.decimal.compare"

    async def test_to_json_string(self, async_session):
        """to_json 返回 JSON 字符串."""
        await _insert_published_item(
            async_session, item_id="item-j", item_version_id="sha256:j-iv",
        )
        profile = [_target(target=5)]
        matrix = await compute_coverage_gap(async_session, profile)

        j = matrix.to_json()
        assert isinstance(j, str)
        parsed = json.loads(j)
        assert parsed["snapshot_id"] is None
        assert len(parsed["cells"]) == 1

    async def test_to_csv_string(self, async_session):
        """to_csv 返回 CSV 字符串（含表头 + 数据行）."""
        await _insert_published_item(
            async_session, item_id="item-c", item_version_id="sha256:c-iv",
            kp_code="kp.a",  # 与 profile 的 kp.a 对齐，actual=1
        )
        profile = [
            _target(kp="kp.a", target=5),
            _target(kp="kp.b", target=3),
        ]
        matrix = await compute_coverage_gap(async_session, profile)

        csv_str = matrix.to_csv()
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["kp_code"] == "kp.a"
        assert rows[0]["target"] == "5"
        assert rows[0]["actual"] == "1"
        assert rows[0]["gap"] == "4"
        # 表头含全部五轴 + 指标
        assert set(reader.fieldnames) == {
            "kp_code", "cognitive_level", "purpose", "gradeband", "subject",
            "target", "actual", "gap", "coverage_pct",
        }

    async def test_to_dict_consumable_by_external_tool(self, async_session):
        """to_dict 结构扁平可被外部排期工具直接消费."""
        profile = [_target(target=5)]
        matrix = await compute_coverage_gap(async_session, profile)
        d = matrix.to_dict()
        # 结构：{snapshot_id, summary{...}, cells[{五轴+指标}]}
        assert "snapshot_id" in d
        assert "summary" in d
        assert "cells" in d
        cell = d["cells"][0]
        for k in ["kp_code", "cognitive_level", "purpose", "gradeband",
                   "subject", "target", "actual", "gap", "coverage_pct"]:
            assert k in cell


# ════════════════════════════════════════════════════════════════════
# §5 不 import 学科包/学段包；学科维度参数注入
# ════════════════════════════════════════════════════════════════════


class TestNoSubjectPackImport:
    """§5 核心域禁止 import 学科包/学段包（宪法 A5/X6）。"""

    def test_coverage_gap_module_no_subject_pack_imports(self):
        """coverage_gap.py 不 import src.packs / src.gradeband."""
        import src.core.data.coverage_gap as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "src.packs" not in source
        assert "src.gradeband" not in source
        assert "from src.packs" not in source

    def test_subject_is_parameter_not_hardcoded(self):
        """学科维度通过参数注入（CoverageTarget.subject），不硬编码."""
        # build_profile_from_grid 接收 subject 参数
        targets = build_profile_from_grid(
            kp_codes=["kp.test"],
            cognitive_levels=["apply"],
            gradebands=["M"],
            subject="subject-english",  # 英语，非硬编码数学
            target_per_purpose={"practice": 5},
        )
        assert all(t.subject == "subject-english" for t in targets)

    def test_coverage_gap_in_core_data_not_packs(self):
        """模块在 src/core/data/ 下，不在 src/packs/."""
        import src.core.data.coverage_gap as mod
        path = Path(mod.__file__).resolve()
        assert "src/core" in str(path).replace("\\", "/")
        assert "src/packs" not in str(path).replace("\\", "/")


# ════════════════════════════════════════════════════════════════════
# build_profile_from_grid 辅助构造器
# ════════════════════════════════════════════════════════════════════


class TestBuildProfileFromGrid:
    """build_profile_from_grid 笛卡尔积构造 profile."""

    def test_cartesian_product(self):
        """笛卡尔积：kp × cognitive × grade × purpose."""
        targets = build_profile_from_grid(
            kp_codes=["kp.a", "kp.b"],
            cognitive_levels=["remember", "apply"],
            gradebands=["M"],
            subject="subject-math",
            target_per_purpose={"practice": 10, "diagnosis": 5},
        )
        # 2 kp × 2 cognitive × 1 grade × 2 purpose = 8
        assert len(targets) == 8
        # 每个单元格 target 正确
        practice = [t for t in targets if t.purpose == "practice"]
        assert len(practice) == 4
        assert all(t.target == 10 for t in practice)

    def test_purposes_constant(self):
        """PURPOSES 常量含三场景."""
        assert "practice" in PURPOSES
        assert "diagnosis" in PURPOSES
        assert "measurement" in PURPOSES
        assert len(PURPOSES) == 3
