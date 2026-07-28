"""T-W4-005 覆盖缺口盘点五轴热力图数据.

架构 v2 §4.7「飞轮闭环」：覆盖缺口盘点（知识点×认知层级×用途×学段×学科
五轴热力图数据）← 组装缺口报告+库存盘点 → 驱动四线生产排期。

核心接口：
- compute_coverage_gap(db, profile, snapshot_id=None) → CoverageGapMatrix
  按五轴统计现有题量与目标配比，输出缺口矩阵数据。
- CoverageGapMatrix.to_dict() / to_json() / to_csv()
  输出格式可被外部排期工具消费（验收 §3）。

五轴定义：
1. 知识点 kp_code（来自 item_kp 表）
2. 认知层级 cognitive_level（来自 item_version.objective->>'cognitive_level'）
3. 用途 purpose（practice/diagnosis/measurement；目标侧维度——items 不绑定用途，
   同一题池服务全部用途，profile 指定各用途的需求量）
4. 学段 gradeband（来自 item_version.objective->>'gradeband'，L/M/H）
5. 学科 subject（来自 item.pack_id，如 subject-math；通过参数注入，§5）

设计要点：
- 实际题量按 4 轴聚合（kp × cognitive × grade × subject），用途为 profile 目标维度；
  同一题池服务全部用途——practice 需 10 题、measurement 需 3 题，actual 是同一池。
- 单次 SQL 查询全量实际计数，Python 端匹配 profile 目标（支持 ~400 节点级，验收 §2）。
- 不做自动排期决策（任务卡 non_goals）；仅产出缺口数据供外部消费。

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
学科维度通过 profile.subject 参数注入（§5）。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 场景三值域（与 ctt / health / D5 对齐）
PURPOSES: tuple[str, ...] = ("practice", "diagnosis", "measurement")


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CoverageTarget:
    """五轴目标配比单元格（profile 输入）.

    - subject：学科标识（如 'subject-math'；通过参数注入，§5 不 import 学科包）
    - target：该单元格期望的题目数量
    """

    kp_code: str
    cognitive_level: str
    purpose: str
    gradeband: str
    subject: str
    target: int


@dataclass(frozen=True)
class CoverageCell:
    """五轴缺口矩阵单元格（compute_coverage_gap 输出）.

    - target：目标题量（来自 profile）
    - actual：现有已发布题量（4 轴聚合：kp × cognitive × grade × subject）
    - gap：缺口数 = max(0, target - actual)
    - coverage_pct：覆盖率 = actual / target * 100（target=0 时为 100.0，已满足）
    """

    kp_code: str
    cognitive_level: str
    purpose: str
    gradeband: str
    subject: str
    target: int
    actual: int
    gap: int
    coverage_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageGapMatrix:
    """五轴覆盖缺口矩阵（含汇总统计）.

    - cells：所有五轴单元格（含 target=0 的也保留，便于排期工具全量消费）
    - snapshot_id：内容快照标识（可选，用于追溯取数时点）
    - total_target / total_actual / total_gap：全局汇总
    - overall_coverage_pct：全局覆盖率
    """

    cells: list[CoverageCell] = field(default_factory=list)
    snapshot_id: Optional[str] = None
    total_target: int = 0
    total_actual: int = 0
    total_gap: int = 0
    overall_coverage_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转 JSON-serializable dict（验收 §3：可被外部排期工具消费）."""
        return {
            "snapshot_id": self.snapshot_id,
            "summary": {
                "total_target": self.total_target,
                "total_actual": self.total_actual,
                "total_gap": self.total_gap,
                "overall_coverage_pct": round(self.overall_coverage_pct, 2),
                "cell_count": len(self.cells),
            },
            "cells": [c.to_dict() for c in self.cells],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """转 JSON 字符串（验收 §3）."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_csv(self) -> str:
        """转 CSV 字符串（验收 §3：外部排期工具消费格式）.

        列：kp_code,cognitive_level,purpose,gradeband,subject,target,actual,gap,coverage_pct
        """
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "kp_code", "cognitive_level", "purpose", "gradeband", "subject",
            "target", "actual", "gap", "coverage_pct",
        ])
        for c in self.cells:
            writer.writerow([
                c.kp_code, c.cognitive_level, c.purpose, c.gradeband, c.subject,
                c.target, c.actual, c.gap, round(c.coverage_pct, 2),
            ])
        return buf.getvalue()

    def gap_cells(self) -> list[CoverageCell]:
        """仅返回有缺口的单元格（gap > 0），按 gap 降序."""
        return sorted(
            [c for c in self.cells if c.gap > 0],
            key=lambda c: c.gap,
            reverse=True,
        )


# ────────────────────────────────────────────────────────────────────
# 取数 SQL：4 轴实际题量聚合
# ────────────────────────────────────────────────────────────────────

# 为什么按 4 轴聚合（无 purpose）：items 不绑定用途（schema 无 purpose 字段）；
# 同一题池服务全部用途。purpose 是 profile 目标侧维度。
# 为什么 LEFT JOIN item_kp：一个 item_version 可能标注多个 kp（kp_set_mode=single
# 时只有一个，但 multi 时有多个）；按 kp_code 展开计数。
# 为什么 status='published'：仅已发布题目计入库存（未过门的不算可用题量）。
_FETCH_ACTUAL_COUNTS_SQL = """
SELECT ikp.kp_code AS kp_code,
       iv.objective->>'cognitive_level' AS cognitive_level,
       iv.objective->>'gradeband' AS gradeband,
       i.pack_id AS subject,
       COUNT(DISTINCT iv.item_version_id) AS actual
FROM item_version iv
JOIN item i ON iv.item_id = i.item_id
JOIN item_kp ikp ON iv.item_version_id = ikp.item_version_id
WHERE iv.status = 'published'
  AND ikp.kp_code = ANY(:kp_codes)
  AND iv.objective->>'cognitive_level' IS NOT NULL
  AND iv.objective->>'gradeband' IS NOT NULL
GROUP BY ikp.kp_code, iv.objective->>'cognitive_level',
         iv.objective->>'gradeband', i.pack_id
"""


async def _fetch_actual_counts(
    db: AsyncSession,
    kp_codes: list[str],
) -> dict[tuple[str, str, str, str], int]:
    """取 4 轴实际题量，返回 {(kp, cognitive, grade, subject): count}."""
    if not kp_codes:
        return {}
    rows = (
        await db.execute(
            text(_FETCH_ACTUAL_COUNTS_SQL),
            {"kp_codes": kp_codes},
        )
    ).fetchall()
    result: dict[tuple[str, str, str, str], int] = {}
    for row in rows:
        key = (row[0], row[1], row[2], row[3])
        result[key] = int(row[4])
    return result


# ────────────────────────────────────────────────────────────────────
# 主接口
# ────────────────────────────────────────────────────────────────────


def _validate_profile(profile: Sequence[CoverageTarget]) -> None:
    """校验 profile：purpose 必须在三值域内（D5 分场景禁混估的边界保护）."""
    for t in profile:
        if t.purpose not in PURPOSES:
            raise ValueError(
                f"非法 purpose={t.purpose!r}；合法值 {PURPOSES}"
            )
        if t.target < 0:
            raise ValueError(f"target 不能为负：{t.target}")


async def compute_coverage_gap(
    db: AsyncSession,
    profile: Sequence[CoverageTarget],
    snapshot_id: Optional[str] = None,
) -> CoverageGapMatrix:
    """计算五轴覆盖缺口矩阵.

    Args:
        db：异步会话
        profile：目标配比列表（CoverageTarget）
        snapshot_id：内容快照标识（可选，用于追溯取数时点）

    Returns:
        CoverageGapMatrix：含每个五轴单元格的 target/actual/gap/coverage_pct
        + 全局汇总统计

    Raises:
        ValueError：非法 purpose 或负 target
    """
    _validate_profile(profile)

    # 收集 profile 涉及的所有 kp_code（单次 SQL 查询实际计数，验收 §2 ~400 节点级）
    kp_codes = sorted({t.kp_code for t in profile})
    actual_map = await _fetch_actual_counts(db, kp_codes)

    # 构造缺口单元格
    cells: list[CoverageCell] = []
    total_target = 0
    total_actual = 0
    total_gap = 0

    for target in profile:
        # 实际题量：4 轴聚合（kp × cognitive × grade × subject），purpose 无关
        actual = actual_map.get(
            (target.kp_code, target.cognitive_level, target.gradeband, target.subject),
            0,
        )
        gap = max(0, target.target - actual)
        # 覆盖率：target=0 视为已满足（无需求），避免除零
        coverage_pct = (
            (actual / target.target * 100.0) if target.target > 0 else 100.0
        )
        # 覆盖率上限 100%（actual > target 时不算超额覆盖，只算满足）
        coverage_pct = min(coverage_pct, 100.0)

        cells.append(CoverageCell(
            kp_code=target.kp_code,
            cognitive_level=target.cognitive_level,
            purpose=target.purpose,
            gradeband=target.gradeband,
            subject=target.subject,
            target=target.target,
            actual=actual,
            gap=gap,
            coverage_pct=coverage_pct,
        ))
        total_target += target.target
        total_actual += min(actual, target.target)  # 实际贡献不超过需求
        total_gap += gap

    overall = (
        (total_actual / total_target * 100.0) if total_target > 0 else 100.0
    )

    return CoverageGapMatrix(
        cells=cells,
        snapshot_id=snapshot_id,
        total_target=total_target,
        total_actual=total_actual,
        total_gap=total_gap,
        overall_coverage_pct=overall,
    )


# ────────────────────────────────────────────────────────────────────
# 辅助：profile 构造器（便于调用方批量生成目标配比）
# ────────────────────────────────────────────────────────────────────


def build_profile_from_grid(
    *,
    kp_codes: Sequence[str],
    cognitive_levels: Sequence[str],
    gradebands: Sequence[str],
    subject: str,
    target_per_purpose: dict[str, int],
) -> list[CoverageTarget]:
    """从网格批量构造 profile（笛卡尔积 × 3 用途）.

    便于调用方按「数学 3-4 年级首批图谱维度」批量生成 ~400 节点级目标配比
    （验收 §2）。target_per_purpose 指定每个用途的每单元格目标题量。

    Args:
        kp_codes：知识点列表（如 ['math.nal.decimal.compare', ...]）
        cognitive_levels：认知层级（如 ['remember', 'understand', 'apply']）
        gradebands：学段（如 ['M'] 表示 3-4 年级）
        subject：学科标识（如 'subject-math'；参数注入，§5）
        target_per_purpose：各用途每单元格目标题量
            如 {'practice': 10, 'diagnosis': 5, 'measurement': 3}

    Returns:
        CoverageTarget 列表（笛卡尔积 kp × cognitive × grade × purpose）
    """
    targets: list[CoverageTarget] = []
    for kp in kp_codes:
        for cog in cognitive_levels:
            for grade in gradebands:
                for purpose, count in target_per_purpose.items():
                    targets.append(CoverageTarget(
                        kp_code=kp,
                        cognitive_level=cog,
                        purpose=purpose,
                        gradeband=grade,
                        subject=subject,
                        target=count,
                    ))
    return targets


__all__ = [
    "PURPOSES",
    "CoverageTarget",
    "CoverageCell",
    "CoverageGapMatrix",
    "compute_coverage_gap",
    "build_profile_from_grid",
]
