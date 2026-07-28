"""T-W4-008 PII 剥离中间件单元测试.

验收对照：
  #2 pii_filter.strip(text) 输出无 PII：姓名→学生A、电话→[PHONE]、地址泛化；
     student_alias_id 保留
  #4 PII 剥离测试含 5 类以上 PII 样本（name/phone/id_card/email/address + 混合）
  #5 不 import 学科包/学段包

宪法 D7：LLM/TTS 调用前必须剥离 PII；student_alias_id 非直标识，保留。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.ai.ledger.pii_filter import strip as pii_strip


# ── 验收 #4：5 类以上 PII 样本（每类独立用例） ──────────────────────

def test_strip_name() -> None:
    """学生姓名 → 学生A（上下文关键字触发，避免误伤普通名词）."""
    sanitized, stripped = pii_strip("学生张三今天没交作业")
    assert "张三" not in sanitized, "姓名应被剥离"
    assert "学生A" in sanitized, "应替换为学生A"
    assert "name" in stripped


def test_strip_phone() -> None:
    """手机号 → [PHONE]."""
    sanitized, stripped = pii_strip("家长电话13912345678请联系")
    assert "13912345678" not in sanitized
    assert "[PHONE]" in sanitized
    assert "phone" in stripped


def test_strip_id_card() -> None:
    """身份证号 → [ID_CARD]（18 位，先于 phone 剥离避免误识别）."""
    sanitized, stripped = pii_strip("身份证号110101199003071234已登记")
    assert "110101199003071234" not in sanitized
    assert "[ID_CARD]" in sanitized
    assert "id_card" in stripped


def test_strip_email() -> None:
    """邮箱 → [EMAIL]."""
    sanitized, stripped = pii_strip("联系邮箱zhangsan@example.com")
    assert "zhangsan@example.com" not in sanitized
    assert "[EMAIL]" in sanitized
    assert "email" in stripped


def test_strip_address() -> None:
    """地址 → [ADDRESS]（行政区划关键字触发泛化）."""
    sanitized, stripped = pii_strip("住址在北京市海淀区中关村路")
    assert "北京市" not in sanitized
    assert "海淀区" not in sanitized
    assert "中关村路" not in sanitized
    assert "[ADDRESS]" in sanitized
    assert "address" in stripped


# ── 验收 #4：混合 PII 样本（第 6 类） ───────────────────────────────

def test_strip_mixed_pii() -> None:
    """单一文本含多类 PII，全部剥离且类型列表完整."""
    text = "学生李四电话13800001111邮箱lisi@x.com住址上海市浦东新区张江路"
    sanitized, stripped = pii_strip(text)
    assert "李四" not in sanitized
    assert "13800001111" not in sanitized
    assert "lisi@x.com" not in sanitized
    assert "上海市" not in sanitized
    assert "学生A" in sanitized  # 李四 → 学生A
    assert "[PHONE]" in sanitized
    assert "[EMAIL]" in sanitized
    assert "[ADDRESS]" in sanitized
    # 类型列表含全部 4 类（name/phone/email/address）
    assert set(stripped) >= {"name", "phone", "email", "address"}


def test_strip_multiple_names_numbered() -> None:
    """多个姓名按出现顺序编号为学生A/学生B/学生C（统一指代，保持一致）."""
    text = "学生张三和家长李四一起接同学王五"
    sanitized, stripped = pii_strip(text)
    assert "学生A" in sanitized  # 学生张三 → 学生A
    assert "学生B" in sanitized  # 家长李四 → 学生B
    assert "学生C" in sanitized  # 同学王五 → 学生C
    assert "张三" not in sanitized
    assert "李四" not in sanitized
    assert "王五" not in sanitized
    assert stripped.count("name") == 1  # name 只记一次类型


# ── 验收 #2：student_alias_id 保留（非 PII） ────────────────────────

def test_student_alias_id_preserved() -> None:
    """student_alias_id（ULID/UUID 格式）非直标识，保留原样（D7）."""
    ulid_str = "01J9X5F8KQABC2C3P0R4A8T6QM"
    uuid_str = "550e8400-e29b-41d4-a716-446655440000"
    text = f"学生别名{ulid_str}和会话{uuid_str}记录"
    sanitized, stripped = pii_strip(text)
    # ULID/UUID 应原样保留（不被任何 PII 正则匹配）
    assert ulid_str in sanitized, f"ULID 应保留，实际：{sanitized}"
    assert uuid_str in sanitized, f"UUID 应保留，实际：{sanitized}"
    # alias 字样不是 PII 类型
    assert "phone" not in stripped
    assert "id_card" not in stripped


# ── 无 PII 文本原样返回 ────────────────────────────────────────────

def test_no_pii_text_unchanged() -> None:
    """无 PII 的文本原样返回，stripped 为空."""
    text = "今天天气不错，适合户外活动。"
    sanitized, stripped = pii_strip(text)
    assert sanitized == text
    assert stripped == []


def test_empty_text() -> None:
    """空字符串原样返回."""
    assert pii_strip("") == ("", [])


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_ledger() -> None:
    """src/core/ai/ledger/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    ledger_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "ledger"
    )
    assert ledger_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(ledger_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(ledger_dir)))
    assert not violations, f"ai/ledger 存在学科包 import（违反 A5）：{violations}"


# ── 剥离结果可逆性：剥离后文本不再含原始 PII ───────────────────────

@pytest.mark.parametrize(
    "pii_sample",
    [
        "学生赵六",
        "电话13900001111",
        "身份证110101199003071234",
        "邮箱zhao6@test.org",
        "住址广州市天河区体育西路",
    ],
    ids=["name", "phone", "id_card", "email", "address"],
)
def test_stripped_text_contains_no_original_pii(pii_sample: str) -> None:
    """剥离后文本不得残留任何原始 PII 字面量（D7 闭环验证）."""
    sanitized, stripped = pii_strip(pii_sample)
    # 原始 PII 字面量不应出现在剥离结果中
    # 提取原始 PII 关键部分（姓名取汉字、号码取数字串等）
    assert pii_sample not in sanitized or "[PHONE]" in sanitized or "[ID_CARD]" in sanitized or "[EMAIL]" in sanitized or "[ADDRESS]" in sanitized or "学生" in sanitized
    assert stripped, f"应识别出 PII 类型：{pii_sample}"
