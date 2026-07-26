"""契约测试：双注册表双向闭合（ADR-0002 #2）。

纪律：交互类型的 compatible_scorers 必须在 scorer.yaml 注册（单向）；
且 scorer 的 input_contract 点名的每个交互 id，其 compatible_scorers 必须包含该 scorer（双向）。
本测试替代人肉全量核对——断裂即红。
"""
from pathlib import Path

import yaml

INTERACTION = Path("specs/contracts/registries/interaction.yaml")
SCORER = Path("specs/contracts/registries/scorer.yaml")


def load():
    with open(INTERACTION, encoding="utf-8") as f:
        interactions = {t["id"]: t for t in yaml.safe_load(f)["types"]}
    with open(SCORER, encoding="utf-8") as f:
        scorers = {s["id"]: s for s in yaml.safe_load(f)["scorers"]}
    return interactions, scorers


def test_interaction_to_scorer_closed():
    """interaction.compatible_scorers 全部在 scorer.yaml 注册。"""
    interactions, scorers = load()
    for t in interactions.values():
        for sid in t["compatible_scorers"]:
            assert sid in scorers, f"{t['id']} 引用未注册评分器 {sid}"


def test_scorer_to_interaction_closed():
    """scorer.input_contract 点名的交互 id，必须反向声明兼容该 scorer。"""
    interactions, scorers = load()
    for s in scorers.values():
        text = s["input_contract"]
        mentioned = [iid for iid in interactions if iid in text]
        for iid in mentioned:
            assert s["id"] in interactions[iid]["compatible_scorers"], (
                f"双向断裂：{s['id']}.input_contract 点名 {iid}，"
                f"但 {iid}.compatible_scorers 不含 {s['id']}"
            )


def test_stepwise_process_substep_constraint_documented():
    """分步子步骤交互收敛（首年三选一）必须在契约文本中注明。"""
    interactions, _ = load()
    desc = str(interactions["stepwise_process"]["response_schema"]["properties"]["steps"]["items"]["properties"]["response"]["description"])
    assert "single_choice" in desc and "numeric_blank" in desc
    assert "有意收敛" in desc or "评审开放" in desc
