"""T-W1-003 ORM 模型单元测试.

对照 T-W01-T03 验证卡 §T-W1-003 部分（§1-§10）：
1.  test_all_tablenames_match_ddl               — 九实体 __tablename__ 正确
2.  test_objective_pydantic_model_exists        — Objective 必填字段校验
3.  test_interaction_ref_pydantic_model_exists
4.  test_content_pydantic_model_exists
5.  test_scoring_ref_pydantic_model_exists
6.  test_error_bindings_pydantic_model_exists   — error_bindings 为 list[dict] permissive
7.  test_lineage_pydantic_model_exists          — tier/pipeline/signed_by/signed_at 必填
8.  test_lineage_pydantic_rejects_missing_required
9.  test_compute_instance_id_signature_matches_contract
10. test_content_addressing_same_input_same_output
11. test_content_addressing_different_input_different_id
12. test_orm_insert_query_item
13. test_orm_insert_item_with_version
14. test_jsonb_six_blocks_roundtrip
15. test_no_subject_pack_imports_in_models       — 宪法 A5/A7 边界扫描

测试隔离：conftest.py 的 async_session fixture 不做 TRUNCATE（见 conftest.py 注释），
本文件所有 INSERT 用 uuid4().hex 生成唯一 id 避免跨测试 PK 冲突。
"""
from __future__ import annotations

import inspect
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args, get_origin

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.content_addressing import (
    compute_canonical_item_version_id,
    compute_instance_id,
    compute_material_version_id,
)
from src.core.models.corpus_asset import CorpusAsset
from src.core.models.corpus_version import CorpusVersion
from src.core.models.item import Item
from src.core.models.item_group import ItemGroup
from src.core.models.item_template import ItemTemplate
from src.core.models.item_template_version import ItemTemplateVersion
from src.core.models.item_version import (
    Content,
    CorpusRef,
    ErrorBindings,
    InteractionRef,
    ItemVersion,
    KpRef,
    Lineage,
    Objective,
    Pipeline,
    ScoringRef,
    StepRef,
)
from src.core.models.material import Material
from src.core.models.material_version import MaterialVersion


# ────────────────────────────────────────────────────────────────────
# 辅助函数
# ────────────────────────────────────────────────────────────────────

def _uid(prefix: str = "") -> str:
    """生成 uuid4 hex 唯一 id（替代 ulid，避免引入新依赖）."""
    return prefix + uuid.uuid4().hex


def _make_objective_dict() -> dict:
    """构造一份最小可用的 objective dict（契约 §2.2.1 结构）."""
    return {
        "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
        "kp_set_mode": "single",
        "cognitive_level": "understand",
        "gradeband": "M",
        "graph_release": "2026.1",
    }


def _make_lineage_dict(tier: str = "C") -> dict:
    """构造一份最小可用的 lineage dict（契约 §2.2.2 结构）."""
    return {
        "tier": tier,
        "pipeline": {"id": "manual", "version": "1.0"},
        "signed_by": "tester",
        "signed_at": "2026-01-01T00:00:00Z",
    }


def _make_version_kwargs(item_id: str, status: str = "draft", tier: str = "C") -> dict:
    """构造一份最小可用的 ItemVersion 列字段（除主键与 item_id 外）.

    tier 同时设置到 lineage.tier（契约 §2.2.2：lineage.tier 与 item.tier 一致）。
    """
    return {
        "item_version_id": f"sha256:{_uid()}",
        "item_id": item_id,
        "status": status,
        "objective": _make_objective_dict(),
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "content": {"blocks": [{"type": "text", "value": "比较大小：0.5 __ 0.8"}]},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"correct_answer": "B"}},
        "error_bindings": [],
        "lineage": _make_lineage_dict(tier=tier),
    }


# ════════════════════════════════════════════════════════════════════
# §1 九实体 __tablename__ 与 DDL 一致
# ════════════════════════════════════════════════════════════════════

def test_all_tablenames_match_ddl():
    """验收 #2：九实体 __tablename__ 与迁移 0002 DDL 逐字对齐."""
    expected = {
        Item: "item",
        ItemVersion: "item_version",
        ItemTemplate: "item_template",
        ItemTemplateVersion: "item_template_version",
        Material: "material",
        MaterialVersion: "material_version",
        ItemGroup: "item_group",
        CorpusAsset: "corpus_asset",
        CorpusVersion: "corpus_version",
    }
    for cls, name in expected.items():
        assert cls.__tablename__ == name, (
            f"{cls.__name__}.__tablename__ 应为 {name!r}，"
            f"实际 {cls.__tablename__!r}"
        )


# ════════════════════════════════════════════════════════════════════
# §3 ItemVersion 六大块 Pydantic 子模型存在性与校验
# ════════════════════════════════════════════════════════════════════

def test_objective_pydantic_model_exists():
    """验收 #3：Objective Pydantic 模型存在且必填字段校验有效."""
    # 必填字段缺失应抛 ValidationError
    with pytest.raises(ValidationError):
        Objective()  # type: ignore[call-arg]

    # 正常构造
    obj = Objective(
        kp_set=[{"dimension": "kp", "code": "math.test"}],
        kp_set_mode="single",
        cognitive_level="remember",
        gradeband="L",
        graph_release="2026.1",
    )
    assert obj.kp_set[0].code == "math.test"
    assert obj.kp_set_mode == "single"
    # 可选字段 steps 默认 None
    assert obj.steps is None

    # steps 可填
    obj2 = Objective(
        kp_set=[{"dimension": "kp", "code": "math.test"}],
        kp_set_mode="single",
        cognitive_level="apply",
        gradeband="H",
        graph_release="2026.1",
        steps=[{"step_id": "s1", "kp": ["math.test"]}],
    )
    assert obj2.steps[0].step_id == "s1"

    # 非法 enum 值应抛 ValidationError
    with pytest.raises(ValidationError):
        Objective(
            kp_set=[{"dimension": "kp", "code": "x"}],
            kp_set_mode="invalid_mode",  # 非法
            cognitive_level="remember",
            gradeband="L",
            graph_release="2026.1",
        )


def test_interaction_ref_pydantic_model_exists():
    """验收 #3：InteractionRef Pydantic 模型存在且必填字段校验有效."""
    with pytest.raises(ValidationError):
        InteractionRef()  # type: ignore[call-arg]

    ir = InteractionRef(
        interaction_id="single_choice",
        interaction_params={"options": ["A", "B", "C", "D"]},
    )
    assert ir.interaction_id == "single_choice"
    assert ir.interaction_params["options"] == ["A", "B", "C", "D"]


def test_content_pydantic_model_exists():
    """验收 #3：Content Pydantic 模型存在（permissive，块结构因交互类型而异）."""
    with pytest.raises(ValidationError):
        Content()  # type: ignore[call-arg] — blocks 必填

    c = Content(blocks=[{"type": "text", "value": "题干"}])
    assert c.blocks[0]["type"] == "text"
    assert c.blocks[0]["value"] == "题干"

    # 空数组也合法（minimal content）
    c_empty = Content(blocks=[])
    assert c_empty.blocks == []


def test_scoring_ref_pydantic_model_exists():
    """验收 #3：ScoringRef Pydantic 模型存在且必填字段校验有效."""
    with pytest.raises(ValidationError):
        ScoringRef()  # type: ignore[call-arg]

    sr = ScoringRef(
        scorer_id="exact_match",
        scorer_params={"correct_answer": "B"},
    )
    assert sr.scorer_id == "exact_match"
    assert sr.scorer_params["correct_answer"] == "B"


def test_error_bindings_pydantic_model_exists():
    """验收 #3：ErrorBindings 为 list[dict] permissive（RootModel）.

    为什么用 RootModel：error_bindings JSONB 顶层是数组（list[dict]），
    RootModel 让 Pydantic 模型直接对应数组，而非 {bindings: [...]} 包装。
    """
    # 空数组合法
    eb_empty = ErrorBindings.model_validate([])
    assert eb_empty.root == []

    # 单元素合法，元素结构 permissive（dict[str, Any]）
    eb = ErrorBindings.model_validate([
        {"option": "A", "error_type_id": "math.decimal.digits_more_is_larger"}
    ])
    assert eb.root[0]["option"] == "A"

    # 顶层非数组应抛 ValidationError
    with pytest.raises(ValidationError):
        ErrorBindings.model_validate({"not": "a list"})  # type: ignore[arg-type]


def test_lineage_pydantic_model_exists():
    """验收 #3/§5.2：Lineage 必填 tier/pipeline/signed_by/signed_at."""
    with pytest.raises(ValidationError):
        Lineage()  # type: ignore[call-arg]

    lin = Lineage(
        tier="C",
        pipeline={"id": "manual", "version": "1.0"},
        signed_by="author1",
        signed_at="2026-01-01T00:00:00Z",
    )
    assert lin.tier == "C"
    assert isinstance(lin.pipeline, Pipeline)
    assert lin.pipeline.id == "manual"
    assert lin.signed_by == "author1"
    assert lin.signed_at == "2026-01-01T00:00:00Z"
    # 可选字段默认 None
    assert lin.template_version_id is None
    assert lin.params is None
    assert lin.seed is None
    assert lin.corpus_refs is None
    assert lin.ai_ledger_refs is None

    # 可选字段可填
    lin2 = Lineage(
        tier="A",
        pipeline={"id": "instantiation-engine", "version": "1.0.0"},
        template_version_id="sha256:abc",
        params={"difficulty": 0.5},
        seed=42,
        corpus_refs=[{"corpus_version_id": "sha256:c1", "digest": "sha256:c1"}],
        ai_ledger_refs=["ledger-1"],
        signed_by="engine",
        signed_at="2026-07-26T12:00:00Z",
    )
    assert lin2.tier == "A"
    assert lin2.template_version_id == "sha256:abc"
    assert lin2.seed == 42
    assert lin2.corpus_refs[0].corpus_version_id == "sha256:c1"
    assert lin2.ai_ledger_refs == ["ledger-1"]


def test_lineage_pydantic_rejects_missing_required():
    """验收 #3：lineage 缺必填字段时抛 ValidationError."""
    # 缺 tier
    with pytest.raises(ValidationError):
        Lineage(  # type: ignore[call-arg]
            pipeline={"id": "manual", "version": "1.0"},
            signed_by="x",
            signed_at="2026-01-01T00:00:00Z",
        )
    # 缺 pipeline
    with pytest.raises(ValidationError):
        Lineage(  # type: ignore[call-arg]
            tier="C",
            signed_by="x",
            signed_at="2026-01-01T00:00:00Z",
        )
    # 缺 signed_by
    with pytest.raises(ValidationError):
        Lineage(  # type: ignore[call-arg]
            tier="C",
            pipeline={"id": "manual", "version": "1.0"},
            signed_at="2026-01-01T00:00:00Z",
        )
    # 缺 signed_at
    with pytest.raises(ValidationError):
        Lineage(  # type: ignore[call-arg]
            tier="C",
            pipeline={"id": "manual", "version": "1.0"},
            signed_by="x",
        )
    # 非法 tier 值
    with pytest.raises(ValidationError):
        Lineage(  # type: ignore[call-arg]
            tier="X",  # 仅 A/B/C/D 合法
            pipeline={"id": "manual", "version": "1.0"},
            signed_by="x",
            signed_at="2026-01-01T00:00:00Z",
        )


# ════════════════════════════════════════════════════════════════════
# §4 内容寻址函数签名验证（D3）
# ════════════════════════════════════════════════════════════════════

def test_compute_instance_id_signature_matches_contract():
    """验收 #4：compute_instance_id 参数名与顺序与契约 §3 公式一逐字对齐."""
    sig = inspect.signature(compute_instance_id)
    params = list(sig.parameters.keys())
    expected = [
        "template_version_digest",
        "normalized_params",
        "pack_digest",
        "engine_digest",
        "corpus_digests",
        "locale",
    ]
    assert params == expected, (
        f"参数顺序错误，期望 {expected}，实际 {params}"
    )

    # corpus_digests 类型注解必须为 list[str]
    ann = sig.parameters["corpus_digests"].annotation
    # 兼容 from __future__ import annotations 的字符串形式
    if isinstance(ann, str):
        assert ann in ("list[str]", "List[str]"), (
            f"corpus_digests 注解应为 list[str]，实际 {ann!r}"
        )
    else:
        # 非字符串形式：list[str] 等价于 get_origin(list) + get_args(str)
        assert get_origin(ann) is list, (
            f"corpus_digests 注解 origin 应为 list，实际 {get_origin(ann)!r}"
        )
        args = get_args(ann)
        assert args == (str,), (
            f"corpus_digests 注解 args 应为 (str,)，实际 {args!r}"
        )

    # 返回值注解为 str
    assert sig.return_annotation is str or sig.return_annotation == "str", (
        f"返回值注解应为 str，实际 {sig.return_annotation!r}"
    )


def test_content_addressing_same_input_same_output():
    """验收 #5：D3 确定性——同输入必产生同输出."""
    args = (
        "sha256:template_v1",
        {"difficulty": 0.5, "seed": 42},
        "sha256:math-pack-v2",
        "sha256:engine-v1",
        ["sha256:corpus1", "sha256:corpus2"],
        "zh-CN",
    )
    id1 = compute_instance_id(*args)
    id2 = compute_instance_id(*args)
    assert id1 == id2, "相同输入应产生相同 instance_id"
    assert isinstance(id1, str)
    assert id1.startswith("sha256:"), "返回值应以 'sha256:' 前缀"
    assert len(id1) == len("sha256:") + 64  # sha256 hex = 64 chars

    # 公式二同样确定性
    obj = _make_objective_dict()
    ir = {"interaction_id": "single_choice", "interaction_params": {}}
    ct = {"blocks": []}
    sr = {"scorer_id": "exact_match", "scorer_params": {}}
    eb: list = []
    vid1 = compute_canonical_item_version_id(obj, ir, ct, sr, eb, "zh-CN")
    vid2 = compute_canonical_item_version_id(obj, ir, ct, sr, eb, "zh-CN")
    assert vid1 == vid2

    # 公式三同样确定性
    m1 = compute_material_version_id("minio:materials/sha256:abc")
    m2 = compute_material_version_id("minio:materials/sha256:abc")
    assert m1 == m2


def test_content_addressing_different_input_different_id():
    """验收 #6：任一参数变化应产生不同 id（D3 可区分性）."""
    base_args = (
        "sha256:template_v1",
        {"difficulty": 0.5},
        "sha256:math-pack-v2",
        "sha256:engine-v1",
        [],
        "zh-CN",
    )
    id_base = compute_instance_id(*base_args)

    # 修改 template_version_digest
    args_diff_tvd = list(base_args)
    args_diff_tvd[0] = "sha256:template_v2"
    assert compute_instance_id(*args_diff_tvd) != id_base, "改 template_version_digest 应得不同 id"

    # 修改 normalized_params
    args_diff_np = list(base_args)
    args_diff_np[1] = {"difficulty": 0.6}
    assert compute_instance_id(*args_diff_np) != id_base, "改 normalized_params 应得不同 id"

    # 修改 pack_digest
    args_diff_pd = list(base_args)
    args_diff_pd[2] = "sha256:math-pack-v3"
    assert compute_instance_id(*args_diff_pd) != id_base, "改 pack_digest 应得不同 id"

    # 修改 engine_digest
    args_diff_ed = list(base_args)
    args_diff_ed[3] = "sha256:engine-v2"
    assert compute_instance_id(*args_diff_ed) != id_base, "改 engine_digest 应得不同 id"

    # 修改 corpus_digests（从 [] 到 ['sha256:c1']）
    args_diff_cd = list(base_args)
    args_diff_cd[4] = ["sha256:c1"]
    assert compute_instance_id(*args_diff_cd) != id_base, "改 corpus_digests 应得不同 id"

    # 修改 locale
    args_diff_l = list(base_args)
    args_diff_l[5] = "en-US"
    assert compute_instance_id(*args_diff_l) != id_base, "改 locale 应得不同 id"

    # corpus_digests 元素顺序变化也应得不同 id（顺序敏感）
    id_order1 = compute_instance_id(
        "sha256:t", {"d": 0.5}, "sha256:p", "sha256:e",
        ["sha256:c1", "sha256:c2"], "zh-CN",
    )
    id_order2 = compute_instance_id(
        "sha256:t", {"d": 0.5}, "sha256:p", "sha256:e",
        ["sha256:c2", "sha256:c1"], "zh-CN",
    )
    assert id_order1 != id_order2, "corpus_digests 顺序变化应得不同 id"


# ════════════════════════════════════════════════════════════════════
# §7 ORM 插入/查询烟测
# ════════════════════════════════════════════════════════════════════

async def test_orm_insert_query_item(async_session: AsyncSession):
    """验收 #7：通过 ORM 可插入 item 并查询回来."""
    item_id = _uid()
    item = Item(
        item_id=item_id,
        pack_id="subject-math",
        tier="C",
    )
    async_session.add(item)
    await async_session.commit()

    result = await async_session.get(Item, item_id)
    assert result is not None
    assert result.item_id == item_id
    assert result.pack_id == "subject-math"
    assert result.tier == "C"
    # C/D 级实例 template_version_id 为 NULL
    assert result.template_version_id is None
    # 无 published 版本时 current_version_id 为 NULL
    assert result.current_version_id is None
    # created_at 由 server_default now() 自动填
    assert result.created_at is not None


async def test_orm_insert_item_with_version(async_session: AsyncSession):
    """验收 #7：通过 ORM 可插入 item+item_version 并建立关联."""
    item_id = _uid()
    version_id = f"sha256:{_uid()}"

    item = Item(item_id=item_id, pack_id="subject-math", tier="D")
    # 先 flush 让 item 落库，满足 item_version.item_id NOT NULL FK
    async_session.add(item)
    await async_session.flush()

    version_kwargs = _make_version_kwargs(item_id, status="draft", tier="D")
    version_kwargs["item_version_id"] = version_id
    version = ItemVersion(**version_kwargs)
    async_session.add(version)
    await async_session.commit()

    # 查回 item_version
    result_v = await async_session.get(ItemVersion, version_id)
    assert result_v is not None
    assert result_v.item_id == item_id
    assert result_v.status == "draft"
    # 六大块 JSONB 字段已落库
    assert result_v.objective["kp_set"][0]["code"] == "math.nal.decimal.compare"
    assert result_v.interaction_ref["interaction_id"] == "single_choice"
    assert result_v.content["blocks"][0]["type"] == "text"
    assert result_v.scoring_ref["scorer_id"] == "exact_match"
    assert result_v.error_bindings == []
    assert result_v.lineage["tier"] == "D"
    assert result_v.lineage["pipeline"]["id"] == "manual"
    # draft 状态：published_at / retired_at / gate_certificate_id 均为 NULL
    assert result_v.published_at is None
    assert result_v.retired_at is None
    assert result_v.gate_certificate_id is None
    # draft 状态：rendered_snapshot 可空
    assert result_v.rendered_snapshot is None
    assert result_v.created_at is not None


# ════════════════════════════════════════════════════════════════════
# §8 JSONB 六大块序列化/反序列化往返
# ════════════════════════════════════════════════════════════════════

async def test_jsonb_six_blocks_roundtrip(async_session: AsyncSession):
    """验收 #8：六大块 JSONB 写入→读取→Pydantic 解析一致.

    覆盖：
    - Pydantic 模型构造 → model_dump() → ORM 写入 JSONB
    - expire session 清缓存 → DB 读取 → Pydantic 重新解析
    - 关键字段值与原始构造一致
    """
    # 1) 用 Pydantic 构造六大块
    objective = Objective(
        kp_set=[{"dimension": "kp", "code": "math.nal.decimal.compare"}],
        kp_set_mode="single",
        cognitive_level="understand",
        gradeband="M",
        graph_release="2026.1",
        steps=[{"step_id": "s1", "kp": ["math.nal.decimal.compare"]}],
    )
    interaction_ref = InteractionRef(
        interaction_id="single_choice",
        interaction_params={"options": ["A", "B", "C", "D"]},
    )
    content = Content(
        blocks=[{"type": "text", "value": "比较大小：0.5 __ 0.8"}]
    )
    scoring_ref = ScoringRef(
        scorer_id="exact_match",
        scorer_params={"correct_answer": "B"},
    )
    error_bindings = ErrorBindings.model_validate([
        {"option": "A", "error_type_id": "math.decimal.digits_more_is_larger"},
    ])
    lineage = Lineage(
        tier="C",
        pipeline={"id": "manual", "version": "1.0"},
        signed_by="author1",
        signed_at="2026-01-01T00:00:00Z",
        corpus_refs=[{"corpus_version_id": "sha256:c1", "digest": "sha256:c1"}],
    )

    # 2) 写入 DB（Pydantic → dict → JSONB）
    item_id = _uid()
    version_id = f"sha256:{_uid()}"
    item = Item(item_id=item_id, pack_id="subject-math", tier="C")
    async_session.add(item)
    await async_session.flush()

    version = ItemVersion(
        item_version_id=version_id,
        item_id=item_id,
        status="draft",
        objective=objective.model_dump(),
        interaction_ref=interaction_ref.model_dump(),
        content=content.model_dump(),
        scoring_ref=scoring_ref.model_dump(),
        error_bindings=error_bindings.model_dump(),
        lineage=lineage.model_dump(),
    )
    async_session.add(version)
    await async_session.commit()

    # 3) 清 session 缓存，强制下次 get 从 DB 重读
    async_session.expire_all()

    # 4) 从 DB 读回，用 Pydantic 重新解析
    result = await async_session.get(ItemVersion, version_id)
    assert result is not None

    obj_read = Objective(**result.objective)
    assert obj_read.kp_set[0].code == "math.nal.decimal.compare"
    assert obj_read.kp_set_mode == "single"
    assert obj_read.cognitive_level == "understand"
    assert obj_read.gradeband == "M"
    assert obj_read.graph_release == "2026.1"
    assert obj_read.steps[0].step_id == "s1"
    assert obj_read.steps[0].kp == ["math.nal.decimal.compare"]
    # 嵌套 KpRef 类型保留
    assert isinstance(obj_read.kp_set[0], KpRef)
    assert isinstance(obj_read.steps[0], StepRef)

    ir_read = InteractionRef(**result.interaction_ref)
    assert ir_read.interaction_id == "single_choice"
    assert ir_read.interaction_params["options"] == ["A", "B", "C", "D"]

    c_read = Content(**result.content)
    assert c_read.blocks[0]["type"] == "text"
    assert c_read.blocks[0]["value"] == "比较大小：0.5 __ 0.8"

    sr_read = ScoringRef(**result.scoring_ref)
    assert sr_read.scorer_id == "exact_match"
    assert sr_read.scorer_params["correct_answer"] == "B"

    eb_read = ErrorBindings.model_validate(result.error_bindings)
    assert eb_read.root[0]["option"] == "A"
    assert eb_read.root[0]["error_type_id"] == "math.decimal.digits_more_is_larger"

    lin_read = Lineage(**result.lineage)
    assert lin_read.tier == "C"
    assert lin_read.pipeline.id == "manual"
    assert lin_read.pipeline.version == "1.0"
    assert lin_read.signed_by == "author1"
    assert lin_read.signed_at == "2026-01-01T00:00:00Z"
    assert lin_read.corpus_refs[0].corpus_version_id == "sha256:c1"
    assert isinstance(lin_read.pipeline, Pipeline)
    assert isinstance(lin_read.corpus_refs[0], CorpusRef)


# ════════════════════════════════════════════════════════════════════
# §9 宪法 A5/A7 边界扫描：核心域无学科包 import
# ════════════════════════════════════════════════════════════════════

def test_no_subject_pack_imports_in_models():
    """验收 #9：src/core/models/ 不 import 任何学科包/学段包（宪法 A5/A7）.

    扫描所有 .py 文件，禁止出现 from packs / import packs / from subject_ /
    import subject_ 模式的 import 语句。
    """
    models_dir = Path(__file__).resolve().parent.parent.parent / "src" / "core" / "models"
    assert models_dir.is_dir(), f"目录不存在：{models_dir}"

    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )

    violations: list[str] = []
    for py_file in sorted(models_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(models_dir)))

    assert not violations, (
        f"src/core/models/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
