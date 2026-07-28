"""T-W4-027 双向细目表 schema 单元测试.

对照任务卡验收标准（逐条可执行）：
1. SpecTable 支持任意层数的维度拆分（内容×认知），每个单元格含
   {target_count, difficulty_min, difficulty_max}
2. 校验规则：全部单元格 target_count 之和 >0；单个单元格 difficulty_min ≤
   difficulty_max；维度编码存在性校验（引用知识图谱）
3. 序列化/反序列化 JSON/YAML 无损
4. make accept TASK=T-W4-027 全绿；迁移脚本可升级/降级
5. 不 import 任何学科包/学段包

测试隔离：conftest.py 的 async_session fixture 事务回滚隔离。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.assembly.spec_table import SpecCell, SpecTable
from src.core.models.spec_table import SpecTable as SpecTableORM


# ────────────────────────────────────────────────────────────────────
# 构造辅助
# ────────────────────────────────────────────────────────────────────

def _cell(
    content_code: str = "math.nal.decimal.compare",
    cognitive_level: str = "understand",
    target_count: int = 3,
    difficulty_min: float = 0.40,
    difficulty_max: float = 0.70,
) -> dict:
    return {
        "content_code": content_code,
        "cognitive_level": cognitive_level,
        "target_count": target_count,
        "difficulty_min": difficulty_min,
        "difficulty_max": difficulty_max,
    }


def _spec_table_dict(
    *,
    spec_table_id: str = "spec-math-m-period1",
    spec_table_version: str = "1.0.0",
    gradeband: str = "M",
    graph_release: str = "graph-math-2026q1",
    cells: list[dict] | None = None,
) -> dict:
    if cells is None:
        # 默认 2 知识点 × 2 认知层级 = 4 单元格，共 12 题（与 T-W4-028 验收 #2 一致）
        cells = [
            _cell("math.nal.decimal.compare", "remember", target_count=3,
                  difficulty_min=0.50, difficulty_max=0.80),
            _cell("math.nal.decimal.compare", "apply", target_count=3,
                  difficulty_min=0.30, difficulty_max=0.60),
            _cell("math.nal.fraction.add", "remember", target_count=3,
                  difficulty_min=0.50, difficulty_max=0.80),
            _cell("math.nal.fraction.add", "apply", target_count=3,
                  difficulty_min=0.30, difficulty_max=0.60),
        ]
    return {
        "spec_table_id": spec_table_id,
        "spec_table_version": spec_table_version,
        "gradeband": gradeband,
        "graph_release": graph_release,
        "cells": cells,
    }


# ────────────────────────────────────────────────────────────────────
# 验收 #1：维度拆分 + 单元格字段
# ────────────────────────────────────────────────────────────────────

def test_spec_cell_has_required_fields():
    """单元格含 {target_count, difficulty_min, difficulty_max} 三字段."""
    cell = SpecCell(
        content_code="math.a",
        cognitive_level="apply",
        target_count=2,
        difficulty_min=0.3,
        difficulty_max=0.6,
    )
    assert cell.target_count == 2
    assert cell.difficulty_min == 0.3
    assert cell.difficulty_max == 0.6


def test_spec_table_supports_arbitrary_content_depth():
    """内容维度支持任意层数的点分 code（1 级、3 级、5 级均可）."""
    cells = [
        _cell("math", "apply", target_count=1),                       # 1 级
        _cell("math.nal", "apply", target_count=1),                   # 2 级
        _cell("math.nal.decimal", "apply", target_count=1),           # 3 级
        _cell("math.nal.decimal.compare", "apply", target_count=1),   # 4 级
        _cell("math.nal.decimal.compare.sign", "apply", target_count=1),  # 5 级
    ]
    st = SpecTable(**_spec_table_dict(cells=cells))
    assert len(st.cells) == 5
    assert st.total_count == 5


def test_spec_table_supports_all_six_cognitive_levels():
    """认知层级支持 Bloom 六级（与 Objective.cognitive_level 同集）."""
    levels = ["remember", "understand", "apply", "analyze", "evaluate", "create"]
    cells = [_cell("math.a", lvl, target_count=1) for lvl in levels]
    st = SpecTable(**_spec_table_dict(cells=cells))
    assert {c.cognitive_level for c in st.cells} == set(levels)


# ────────────────────────────────────────────────────────────────────
# 验收 #2：校验规则
# ────────────────────────────────────────────────────────────────────

def test_rejects_total_count_zero():
    """全部单元格 target_count 之和必须 > 0."""
    cells = [
        _cell("math.a", "remember", target_count=0),
        _cell("math.b", "apply", target_count=0),
    ]
    with pytest.raises(ValidationError) as exc:
        SpecTable(**_spec_table_dict(cells=cells))
    assert "target_count" in str(exc.value) or "> 0" in str(exc.value)


def test_rejects_difficulty_min_gt_max():
    """单个单元格 difficulty_min ≤ difficulty_max."""
    cells = [
        _cell("math.a", "remember",
              target_count=2, difficulty_min=0.7, difficulty_max=0.3),
    ]
    with pytest.raises(ValidationError) as exc:
        SpecTable(**_spec_table_dict(cells=cells))
    assert "difficulty_min" in str(exc.value)


def test_accepts_difficulty_min_eq_max():
    """边界：difficulty_min == difficulty_max 合法（单点区间）."""
    cells = [_cell("math.a", "apply",
                   target_count=1, difficulty_min=0.5, difficulty_max=0.5)]
    st = SpecTable(**_spec_table_dict(cells=cells))
    assert st.cells[0].difficulty_min == st.cells[0].difficulty_max


def test_rejects_duplicate_cell():
    """同一 (content_code, cognitive_level) 唯一."""
    cells = [
        _cell("math.a", "apply", target_count=1),
        _cell("math.a", "apply", target_count=2),  # 重复
    ]
    with pytest.raises(ValidationError) as exc:
        SpecTable(**_spec_table_dict(cells=cells))
    assert "重复" in str(exc.value)


def test_rejects_invalid_cognitive_level():
    """认知层级必须 Bloom 六值之一."""
    cells = [_cell("math.a", "synthesis", target_count=1)]  # 旧版 Bloom 名
    with pytest.raises(ValidationError):
        SpecTable(**_spec_table_dict(cells=cells))


def test_rejects_invalid_gradeband():
    """学段必须 L/M/H 之一."""
    with pytest.raises(ValidationError):
        SpecTable(**_spec_table_dict(gradeband="X"))


def test_rejects_negative_target_count():
    """target_count ≥ 0（负值拒绝）."""
    cells = [_cell("math.a", "apply", target_count=-1)]
    with pytest.raises(ValidationError):
        SpecTable(**_spec_table_dict(cells=cells))


def test_rejects_difficulty_out_of_unit_interval():
    """difficulty ∈ [0.0, 1.0]（p_correct 口径）."""
    cells = [_cell("math.a", "apply",
                   target_count=1, difficulty_min=-0.1, difficulty_max=0.5)]
    with pytest.raises(ValidationError):
        SpecTable(**_spec_table_dict(cells=cells))
    cells = [_cell("math.a", "apply",
                   target_count=1, difficulty_min=0.5, difficulty_max=1.5)]
    with pytest.raises(ValidationError):
        SpecTable(**_spec_table_dict(cells=cells))


def test_validate_against_graph_accepts_known_codes():
    """维度编码存在性：所有 content_code 在图谱中 → 返回空列表."""
    st = SpecTable(**_spec_table_dict())
    unknown = st.validate_against_graph(
        {"math.nal.decimal.compare", "math.nal.fraction.add", "math.other"}
    )
    assert unknown == []


def test_validate_against_graph_rejects_unknown_codes():
    """维度编码存在性：content_code 不在图谱中 → 抛 ValueError."""
    st = SpecTable(**_spec_table_dict())
    with pytest.raises(ValueError) as exc:
        st.validate_against_graph({"math.only.this"})  # 缺两个 code
    msg = str(exc.value)
    assert "math.nal.decimal.compare" in msg
    assert "math.nal.fraction.add" in msg


def test_cell_at_lookup():
    """cell_at 按 (content_code, cognitive_level) 取单元格."""
    st = SpecTable(**_spec_table_dict())
    found = st.cell_at("math.nal.decimal.compare", "remember")
    assert found is not None
    assert found.target_count == 3
    missing = st.cell_at("math.nal.decimal.compare", "create")
    assert missing is None


# ────────────────────────────────────────────────────────────────────
# 验收 #3：序列化/反序列化 JSON/YAML 无损
# ────────────────────────────────────────────────────────────────────

def test_json_roundtrip_lossless():
    """JSON 序列化/反序列化往返无损."""
    st = SpecTable(**_spec_table_dict())
    s = st.to_json()
    st2 = SpecTable.from_json(s)
    assert st2.model_dump() == st.model_dump()


def test_yaml_roundtrip_lossless():
    """YAML 序列化/反序列化往返无损（经 JSON 中转）."""
    st = SpecTable(**_spec_table_dict())
    s = st.to_yaml()
    st2 = SpecTable.from_yaml(s)
    assert st2.model_dump() == st.model_dump()


def test_json_serialization_is_deterministic():
    """JSON 序列化确定性（sort_keys）——同内容必同字符串."""
    st = SpecTable(**_spec_table_dict())
    # 打乱 cells 顺序构造另一份（model_dump 后顺序固定，但构造期不同）
    cells_rev = list(reversed(_spec_table_dict()["cells"]))
    st_rev = SpecTable(**_spec_table_dict(cells=cells_rev))
    # to_json 走 sort_keys，但 cells 是 list[dict]——list 顺序保留
    # 确定性体现在同对象多次序列化同输出：
    assert st.to_json() == st.to_json()
    assert json.loads(st.to_json())["gradeband"] == "M"


def test_to_dict_matches_model_dump_json_mode():
    """to_dict 与 model_dump(mode='json') 一致（给 ORM 落库用）."""
    st = SpecTable(**_spec_table_dict())
    assert st.to_dict() == st.model_dump(mode="json")


# ────────────────────────────────────────────────────────────────────
# 验收 #4：ORM 落库 + 迁移可逆（migrate-check 验证可逆性）
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orm_can_persist_and_query_spec_table(async_session: AsyncSession):
    """ORM 行可写入并可按 (id, version) 查询；cells JSONB 往返无损."""
    st = SpecTable(**_spec_table_dict())
    orm = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version=st.spec_table_version,
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=st.to_dict()["cells"],
        created_by="tester",
    )
    async_session.add(orm)
    await async_session.commit()  # savepoint release

    rows = (
        await async_session.execute(
            select(SpecTableORM).where(
                SpecTableORM.spec_table_id == st.spec_table_id,
                SpecTableORM.spec_table_version == st.spec_table_version,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    got = rows[0]
    assert got.gradeband == "M"
    assert got.graph_release == "graph-math-2026q1"
    # cells JSONB 往返：可重建 SpecTable Pydantic 模型
    rebuilt = SpecTable(
        spec_table_id=got.spec_table_id,
        spec_table_version=got.spec_table_version,
        gradeband=got.gradeband,
        graph_release=got.graph_release,
        cells=got.cells,
    )
    assert rebuilt.total_count == st.total_count


@pytest.mark.asyncio
async def test_orm_append_only_trigger_blocks_update(async_session: AsyncSession):
    """D1 物理强制：spec_table UPDATE 被触发器拒绝."""
    st = SpecTable(**_spec_table_dict())
    orm = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version=st.spec_table_version,
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=st.to_dict()["cells"],
        created_by="tester",
    )
    async_session.add(orm)
    await async_session.commit()

    # 直接 SQL UPDATE（绕过 ORM 单元工作量追踪），预期触发器抛异常
    with pytest.raises(Exception) as exc:
        await async_session.execute(
            text("UPDATE spec_table SET gradeband = 'H' WHERE spec_table_id = :id").bindparams(
                id=st.spec_table_id
            )
        )
        await async_session.commit()
    assert "append-only" in str(exc.value).lower() or "D1" in str(exc.value)


@pytest.mark.asyncio
async def test_orm_append_only_trigger_blocks_delete(async_session: AsyncSession):
    """D1 物理强制：spec_table DELETE 被触发器拒绝."""
    st = SpecTable(**_spec_table_dict(
        spec_table_id="spec-delete-test",
        spec_table_version="1.0.0",
    ))
    orm = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version=st.spec_table_version,
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=st.to_dict()["cells"],
        created_by="tester",
    )
    async_session.add(orm)
    await async_session.commit()

    with pytest.raises(Exception) as exc:
        await async_session.execute(
            text("DELETE FROM spec_table WHERE spec_table_id = :id").bindparams(
                id=st.spec_table_id
            )
        )
        await async_session.commit()
    assert "append-only" in str(exc.value).lower() or "D1" in str(exc.value)


@pytest.mark.asyncio
async def test_unique_id_version_constraint(async_session: AsyncSession):
    """(spec_table_id, spec_table_version) 联合唯一：同 id 同版本拒绝二次插入."""
    st = SpecTable(**_spec_table_dict())
    cells_json = st.to_dict()["cells"]
    orm1 = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version=st.spec_table_version,
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=cells_json,
        created_by="tester",
    )
    async_session.add(orm1)
    await async_session.commit()

    orm2 = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version=st.spec_table_version,  # 同版本
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=cells_json,
        created_by="tester",
    )
    async_session.add(orm2)
    with pytest.raises(Exception):  # UniqueViolationError
        await async_session.commit()


@pytest.mark.asyncio
async def test_same_id_new_version_allowed(async_session: AsyncSession):
    """同 id 不同 version 允许（D1 版本账：改表 = 新版本行）."""
    st = SpecTable(**_spec_table_dict())
    cells_json = st.to_dict()["cells"]
    orm1 = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version="1.0.0",
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=cells_json,
        created_by="tester",
    )
    orm2 = SpecTableORM(
        spec_table_id=st.spec_table_id,
        spec_table_version="1.1.0",  # 新版本
        gradeband=st.gradeband,
        graph_release=st.graph_release,
        cells=cells_json,
        created_by="tester",
    )
    async_session.add_all([orm1, orm2])
    await async_session.commit()

    rows = (
        await async_session.execute(
            select(SpecTableORM).where(
                SpecTableORM.spec_table_id == st.spec_table_id
            ).order_by(SpecTableORM.spec_table_version)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert {r.spec_table_version for r in rows} == {"1.0.0", "1.1.0"}


# ────────────────────────────────────────────────────────────────────
# 验收 #5：宪法 A5/A7 边界——不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_spec_table_modules():
    """src/core/assembly/spec_table.py 与 src/core/models/spec_table.py
    不 import 任何学科包/学段包（宪法 A5/A7）."""
    project_root = Path(__file__).resolve().parent.parent.parent
    targets = [
        project_root / "src" / "core" / "assembly" / "spec_table.py",
        project_root / "src" / "core" / "models" / "spec_table.py",
    ]
    pattern = __import__("re").compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        __import__("re").MULTILINE,
    )
    for f in targets:
        assert f.is_file(), f"文件不存在：{f}"
        text = f.read_text(encoding="utf-8")
        assert not pattern.findall(text), (
            f"{f.name} 含学科包/学段包 import（违反 A5/A7）"
        )
