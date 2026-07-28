"""T-W4-034 扫描件姓名 redaction 单元测试.

覆盖验收标准：
1. ``redact_name(text, name)`` 将文本中的姓名替换为「[姓名]」；支持常见姓名变体
   （无 OCR，纯字符串匹配）。
2. ``make accept TASK=T-W4-034`` 全绿。
3. 不 import 任何学科包/学段包（X6）.
"""
from __future__ import annotations

import pytest

from src.core.compliance.redaction import (
    REDACTED_PLACEHOLDER,
    redact_name,
    redact_names,
)


# ────────────────────────────────────────────────────────────────────
# 1. 基础替换
# ────────────────────────────────────────────────────────────────────

class TestRedactNameBasic:
    """redact_name 基础替换功能."""

    def test_simple_chinese_name(self) -> None:
        """中文姓名精确匹配替换."""
        text = "张三同学答对了这道题"
        result = redact_name(text, "张三")
        assert result == f"{REDACTED_PLACEHOLDER}同学答对了这道题"

    def test_multiple_occurrences(self) -> None:
        """多次出现全部替换."""
        text = "张三的答案是 A，张三做对了"
        result = redact_name(text, "张三")
        assert result == f"{REDACTED_PLACEHOLDER}的答案是 A，{REDACTED_PLACEHOLDER}做对了"

    def test_no_match_unchanged(self) -> None:
        """文本中无姓名时不改变."""
        text = "李四同学答对了"
        result = redact_name(text, "张三")
        assert result == text

    def test_three_char_name(self) -> None:
        """三字姓名替换."""
        text = "诸葛亮写了出师表"
        result = redact_name(text, "诸葛亮")
        assert result == f"{REDACTED_PLACEHOLDER}写了出师表"

    def test_four_char_name(self) -> None:
        """四字姓名替换."""
        text = "欧阳明日同学的表现很好"
        result = redact_name(text, "欧阳明日")
        assert result == f"{REDACTED_PLACEHOLDER}同学的表现很好"


# ────────────────────────────────────────────────────────────────────
# 2. 姓名变体（验收 1：支持常见姓名变体）
# ────────────────────────────────────────────────────────────────────

class TestRedactNameVariants:
    """redact_name 支持常见姓名变体."""

    def test_name_with_single_space(self) -> None:
        """姓名间含空格：「张 三」匹配「张三」."""
        text = "张 三 同学答对了"
        result = redact_name(text, "张三")
        assert result == f"{REDACTED_PLACEHOLDER} 同学答对了"

    def test_name_with_multiple_spaces(self) -> None:
        """姓名间含多空格：「张  三」匹配「张三」."""
        text = "张  三同学答对了"
        result = redact_name(text, "张三")
        assert result == f"{REDACTED_PLACEHOLDER}同学答对了"

    def test_english_case_insensitive(self) -> None:
        """英文姓名大小写不敏感."""
        text = "John Smith answered correctly"
        result = redact_name(text, "john smith")
        assert result == f"{REDACTED_PLACEHOLDER} answered correctly"

    def test_english_uppercase_name(self) -> None:
        """大写英文姓名匹配."""
        text = "JOHN answered correctly"
        result = redact_name(text, "John")
        assert result == f"{REDACTED_PLACEHOLDER} answered correctly"

    def test_english_word_boundary(self) -> None:
        """英文姓名不误匹配子串：John 不匹配 Johnson."""
        text = "Johnson is different from John"
        result = redact_name(text, "John")
        # Johnson 中的 John 不替换（\b 词边界保护），独立的 John 替换
        assert "Johnson" in result
        assert REDACTED_PLACEHOLDER in result
        assert result == f"Johnson is different from {REDACTED_PLACEHOLDER}"


# ────────────────────────────────────────────────────────────────────
# 3. 边界条件
# ────────────────────────────────────────────────────────────────────

class TestRedactNameEdgeCases:
    """redact_name 边界条件."""

    def test_empty_name_no_change(self) -> None:
        """空姓名不改变文本."""
        text = "张三同学"
        assert redact_name(text, "") == text
        assert redact_name(text, "   ") == text

    def test_empty_text(self) -> None:
        """空文本返回空."""
        assert redact_name("", "张三") == ""

    def test_name_not_in_text(self) -> None:
        """姓名不在文本中返回原文."""
        text = "今天天气很好"
        assert redact_name(text, "张三") == text

    def test_name_with_surrounding_text(self) -> None:
        """姓名前后有上下文."""
        text = "请记录张三的成绩"
        result = redact_name(text, "张三")
        assert result == f"请记录{REDACTED_PLACEHOLDER}的成绩"

    def test_type_error_on_non_string_text(self) -> None:
        """text 非 str 抛 TypeError."""
        with pytest.raises(TypeError):
            redact_name(123, "张三")  # type: ignore[arg-type]

    def test_type_error_on_non_string_name(self) -> None:
        """name 非 str 抛 TypeError."""
        with pytest.raises(TypeError):
            redact_name("张三", 123)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# 4. 批量脱敏
# ────────────────────────────────────────────────────────────────────

class TestRedactNames:
    """redact_names 批量脱敏多个姓名."""

    def test_multiple_names(self) -> None:
        """多个姓名依次脱敏."""
        text = "张三和李四一起做题"
        result = redact_names(text, ["张三", "李四"])
        assert result == f"{REDACTED_PLACEHOLDER}和{REDACTED_PLACEHOLDER}一起做题"

    def test_empty_list_no_change(self) -> None:
        """空列表不改变文本."""
        text = "张三同学"
        assert redact_names(text, []) == text

    def test_names_with_empty_entries(self) -> None:
        """列表含空项自动跳过."""
        text = "张三同学"
        result = redact_names(text, ["", "张三", "  "])
        assert result == f"{REDACTED_PLACEHOLDER}同学"

    def test_already_redacted_not_double_replaced(self) -> None:
        """已脱敏的占位符不被二次替换."""
        text = f"{REDACTED_PLACEHOLDER}和张三"
        result = redact_names(text, ["张三"])
        # 占位符 [姓名] 中的「姓名」不会被当作名字再次替换
        assert result.count(REDACTED_PLACEHOLDER) == 2


# ────────────────────────────────────────────────────────────────────
# 5. 学科包隔离（X6）
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """合规层 redaction 不 import 任何学科包/学段包（宪法 A5/X6）."""

    def test_redaction_module_no_subject_pack(self) -> None:
        """redaction 模块源码不引用任何学科包."""
        import inspect
        from src.core.compliance import redaction
        source = inspect.getsource(redaction)
        forbidden = (
            "src.packs",
            "subject_math",
            "subject_chinese",
            "subject_english",
            "gradeband",
            "subject-math",
            "subject-chinese",
            "subject-english",
        )
        for token in forbidden:
            assert token not in source, (
                f"redaction 不得引用学科包/学段包（X6），发现 {token!r}"
            )
