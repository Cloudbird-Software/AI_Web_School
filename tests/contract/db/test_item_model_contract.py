"""契约测试：统一内容模型（specs/contracts/db/item-model.md）。

验收标准（T-W0-004）：结构完整（六块）、状态机与门强制规则存在、内容寻址规则存在。
"""
from pathlib import Path

CONTRACT = Path("specs/contracts/db/item-model.md")

SIX_BLOCKS = ["objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage"]
STATES = ["draft", "quarantined", "published", "retired"]


def text():
    return CONTRACT.read_text(encoding="utf-8")


def test_file_exists_and_tables_defined():
    t = text()
    for table in ("item", "item_version", "item_template", "item_template_version",
                  "material", "material_version", "material_license",
                  "item_group", "corpus_asset", "corpus_version"):
        assert table in t, f"缺表定义 {table}"


def test_six_blocks_present():
    """架构 v2 §2.2：ItemVersion 六块结构。"""
    t = text()
    for block in SIX_BLOCKS:
        assert block in t, f"ItemVersion 缺块 {block}"


def test_state_machine_and_gate_enforcement():
    """D2：状态机四态 + 门证书 DB 级强制。"""
    t = text()
    for state in STATES:
        assert state in t, f"状态机缺状态 {state}"
    assert "gate_certificate_id" in t
    assert "触发器" in t, "缺数据库触发器强制表述"


def test_content_addressing_rule():
    """D3：内容寻址公式与规范化参数。"""
    t = text()
    assert "normalized_params" in t
    assert "locale" in t
    assert "H(" in t or "内容寻址" in t


def test_tier_lineage():
    """A7：四级生产线对等，tier 为谱系字段。"""
    t = text()
    for tier in ("A", "B", "C", "D"):
        assert f"`{tier}`" in t or f"tier" in t
    assert "lineage" in t


def test_append_only_and_retire_not_delete():
    """D1/R-Q-26：只增不改；退役是状态不是删除。"""
    t = text()
    assert "永不" in t
    assert "退役" in t


def test_material_versioned_two_segment():
    """D1 全版本化（ADR-0002 #1）：素材必须是身份+版本两段式，引用指向 material_version_id。"""
    t = text()
    assert "material_version_id" in t
    assert "material_version" in t
    # 素材与 Item 同构的声明必须在场
    assert "material_version 是不可变内容快照" in t or "身份+不可变版本" in t


def test_machine_schemas_present():
    """ADR-0002 #15：objective/lineage 必须有机器可校验 JSON Schema（§5）。"""
    import json
    import re
    t = text()
    blocks = re.findall(r"## 5\..*?```json\n(.*?)```", t, re.S)
    assert len(blocks) >= 2, "§5 至少含 objective 与 lineage 两个 JSON Schema"
    for b in blocks:
        json.loads(b)  # 必须可解析
