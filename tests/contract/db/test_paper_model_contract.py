"""契约测试：卷追溯模型（specs/contracts/db/paper-model.md）。

W3 S9-③：冻结契约补写——paper/paper_item 结构、码制规则、只增不改、
卷面短码印刷规则均在契约文本中声明。
"""
from pathlib import Path

CONTRACT = Path("specs/contracts/db/paper-model.md")

PAPER_COLUMNS = [
    "paper_id", "paper_code", "paper_spec_id", "paper_title",
    "gradeband", "subject_pack_id", "weekly_batch_id",
    "kp_snapshot_ref", "seed", "rendered_snapshot_path",
    "created_at", "created_by",
]
PAPER_ITEM_COLUMNS = [
    "paper_item_id", "paper_id", "item_version_id",
    "placement_token", "item_number", "item_short_code", "created_at",
]


def text():
    return CONTRACT.read_text(encoding="utf-8")


def test_file_exists_and_tables_defined():
    t = text()
    assert "paper" in t and "paper_item" in t


def test_paper_columns_documented():
    t = text()
    for col in PAPER_COLUMNS:
        assert col in t, f"paper 缺列 {col}"


def test_paper_item_columns_documented():
    t = text()
    for col in PAPER_ITEM_COLUMNS:
        assert col in t, f"paper_item 缺列 {col}"


def test_append_only_rule():
    """D1 风格：只增不改 + 触发器物理强制."""
    t = text()
    assert "只增不改" in t
    assert "触发器" in t


def test_code_schemes_documented():
    """卷码/QR/题短码规则（Luhn + base32 + 不含实例明文）."""
    t = text()
    assert "Luhn" in t
    assert "paper_spec_id" in t
    assert "item_short_code" in t
    assert "不含" in t and "明文" in t


def test_trace_chain_documented():
    """追溯链：短码 → paper_item → item_version → gate_certificate."""
    t = text()
    assert "gate_certificate" in t
    assert "item_version" in t


def test_print_short_code_rule():
    """W3 S9-①：placement_token 与 item_short_code 印于卷面."""
    t = text()
    assert "placement_token" in t
    assert "卷面" in t
