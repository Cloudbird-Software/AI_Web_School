"""契约测试：交互类型注册表（specs/contracts/registries/interaction.yaml）。

验收标准（T-W0-004）：schema 可解析、必填字段存在。
纪律：本测试只增不改；修改契约必须先改测试预期并走契约变更流程。
"""
from pathlib import Path

import yaml

CONTRACT = Path("specs/contracts/registries/interaction.yaml")
SCORER_CONTRACT = Path("specs/contracts/registries/scorer.yaml")


def load():
    with open(CONTRACT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_parseable():
    data = load()
    assert data["registry"] == "interaction"
    assert data["contract_version"]
    assert data["status"] in ("frozen-candidate", "frozen")


def test_required_top_fields():
    data = load()
    for field in ("registry", "contract_version", "status", "required_fields", "types"):
        assert field in data, f"缺顶层字段 {field}"


def test_every_type_has_required_fields():
    data = load()
    required = set(data["required_fields"])
    for t in data["types"]:
        missing = required - set(t)
        assert not missing, f"交互类型 {t.get('id')} 缺字段 {missing}"


def test_type_count_matches_architecture():
    """架构 v2 §2.3：10 现役 + 2 预留 = 12 种。"""
    data = load()
    active = [t for t in data["types"] if t["status"] == "active"]
    reserved = [t for t in data["types"] if t["status"] == "reserved"]
    assert len(active) == 10, f"现役交互应为 10 种，实际 {len(active)}"
    assert len(reserved) == 2, f"预留交互应为 2 种，实际 {len(reserved)}"
    assert {t["id"] for t in reserved} == {"handwriting_copy", "oral"}


def test_ids_unique():
    data = load()
    ids = [t["id"] for t in data["types"]]
    assert len(ids) == len(set(ids)), "交互类型 id 重复"


def test_response_schemas_are_objects():
    """作答采集 schema 必须是 JSON Schema object 根。"""
    data = load()
    for t in data["types"]:
        schema = t["response_schema"]
        assert schema.get("type") == "object", f"{t['id']} 的 response_schema 根类型必须是 object"
        assert "required" in schema, f"{t['id']} 的 response_schema 缺 required"


def test_compatible_scorers_exist_in_scorer_registry():
    """交叉引用一致性：compatible_scorers 必须在 scorer.yaml 中注册（宪法 D4）。"""
    with open(SCORER_CONTRACT, encoding="utf-8") as f:
        scorer_ids = {s["id"] for s in yaml.safe_load(f)["scorers"]}
    data = load()
    for t in data["types"]:
        for sid in t["compatible_scorers"]:
            assert sid in scorer_ids, f"{t['id']} 引用了未注册评分器 {sid}"


def test_true_false_is_preset_not_type():
    """判断题是单选的参数化预设，不单独注册（架构 v2 §2.3 正文 10 种清单）。"""
    data = load()
    ids = {t["id"] for t in data["types"]}
    assert "true_false" not in ids
    single = next(t for t in data["types"] if t["id"] == "single_choice")
    preset_ids = {p["id"] for p in single.get("presets", [])}
    assert "true_false" in preset_ids
