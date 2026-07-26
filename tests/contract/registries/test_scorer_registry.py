"""契约测试：评分器注册表（specs/contracts/registries/scorer.yaml）。

验收标准（T-W0-004）：schema 可解析、必填字段存在、统一评分契约完整。
"""
from pathlib import Path

import yaml

CONTRACT = Path("specs/contracts/registries/scorer.yaml")


def load():
    with open(CONTRACT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_parseable():
    data = load()
    assert data["registry"] == "scorer"
    assert data["contract_version"]
    assert data["status"] in ("frozen-candidate", "frozen")


def test_required_top_fields():
    data = load()
    for field in ("registry", "contract_version", "status", "unified_contract", "required_fields", "scorers"):
        assert field in data, f"缺顶层字段 {field}"


def test_unified_output_contract_complete():
    """统一契约输出五要素（架构 v2 §2.3）：维度分/错误推断/置信度/证据/评分器版本。"""
    data = load()
    props = data["unified_contract"]["output_schema"]["properties"]
    for key in ("dimension_scores", "error_inferences", "confidence", "evidence", "scorer_version"):
        assert key in props, f"统一输出契约缺 {key}"
    required = data["unified_contract"]["output_schema"]["required"]
    for key in ("dimension_scores", "error_inferences", "confidence", "evidence", "scorer_version"):
        assert key in required


def test_confidence_layering_rule():
    """置信度四层分离：scoring 必填、recognition 独立（架构 v2 §4.5）。"""
    data = load()
    conf = data["unified_contract"]["output_schema"]["properties"]["confidence"]["properties"]
    assert "scoring" in conf
    assert "recognition" in conf


def test_every_scorer_has_required_fields():
    data = load()
    required = set(data["required_fields"])
    for s in data["scorers"]:
        missing = required - set(s)
        assert not missing, f"评分器 {s.get('id')} 缺字段 {missing}"


def test_scorer_count_matches_architecture():
    """架构 v2 §2.3：6 现役 + 1 预留 = 7 种。"""
    data = load()
    active = [s for s in data["scorers"] if s["status"] == "active"]
    reserved = [s for s in data["scorers"] if s["status"] == "reserved"]
    assert len(active) == 6, f"现役评分器应为 6 种，实际 {len(active)}"
    assert len(reserved) == 1, f"预留评分器应为 1 种，实际 {len(reserved)}"
    assert reserved[0]["id"] == "asr_oral"


def test_ids_unique():
    data = load()
    ids = [s["id"] for s in data["scorers"]]
    assert len(ids) == len(set(ids)), "评分器 id 重复"


def test_deterministic_flag_present_and_typed():
    """确定性标志是重判可复现性（R-D-05）的前提，必须为布尔。"""
    data = load()
    for s in data["scorers"]:
        assert isinstance(s["deterministic"], bool), f"{s['id']} 的 deterministic 必须为布尔"
    # 现役确定性评分器：exact_match / math_equivalence / stepwise_rubric / keypoint_hit
    det = {s["id"] for s in data["scorers"] if s["deterministic"]}
    assert {"exact_match", "math_equivalence", "stepwise_rubric", "keypoint_hit"} <= det
