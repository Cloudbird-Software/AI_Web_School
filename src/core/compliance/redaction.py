"""T-W4-034 扫描件姓名 redaction 占位（宪法 D7 / ADR §4.8）.

落地 ADR §4.8「扫描件姓名 redaction 后再入模型」与宪法 D7 PII 隔离：
- 学生作答扫描件 / 文本中出现的姓名须在进入 LLM / TTS 前替换为占位符「[姓名]」。
- 本模块为**纯文本替换**，不做 OCR（OCR 姓名检测属 non_goal）。
- 支持常见姓名变体：全名、姓名间含空格、英文大小写不敏感。

为什么用占位符而非删除：
- 保留语义结构（「[姓名]答对了」比「答对了」更完整），LLM 可正确理解上下文。
- 占位符「[姓名]」明确标记脱敏位置，审计时可追溯脱敏覆盖范围。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""  # noqa: D400
from __future__ import annotations

import re
import unicodedata

# 占位符：姓名脱敏后的统一标记
REDACTED_PLACEHOLDER = "[姓名]"


def _is_cjk(char: str) -> bool:
    r"""判断字符是否为 CJK 统一表意文字（中文/日文汉字/韩文汉字）.

    为什么需要区分 CJK：CJK 文字没有 regex \b 词边界概念（\b 依赖 \w 与 \W
    的交界，CJK 字符在 Python re 中属于 \w），对 CJK 姓名使用 \b 会漏匹配；
    对拉丁字母姓名使用 \b 可避免子串误匹配（如 "John" 误匹配 "Johnson"）。
    """
    try:
        name = unicodedata.name(char)
    except ValueError:
        return False
    return any(
        block in name
        for block in ("CJK", "HIRAGANA", "KATAKANA", "HANGUL")
    )


def _build_pattern(name: str) -> str:
    """构建姓名匹配正则：字符间允许任意空白（含零空白）.

    为什么允许字符间空白：扫描件 OCR / 手工录入常在姓名间插入空格
    （如「张 三」「李  四」），脱敏须覆盖这些变体。

    对纯拉丁字母姓名附加 \\b 词边界，避免「John」误匹配「Johnson」；
    CJK 姓名不加词边界（CJK 无词边界语义，加了反而漏匹配）。
    """
    chars = list(name)
    has_cjk = any(_is_cjk(c) for c in chars)
    # 字符间允许任意空白（含零空白）
    pattern = r"\s*".join(re.escape(c) for c in chars)
    if not has_cjk:
        pattern = r"\b" + pattern + r"\b"
    return pattern


def redact_name(text: str, name: str) -> str:
    """将文本中出现的指定姓名替换为「[姓名]」占位符.

    纯字符串匹配（不做 OCR），支持常见姓名变体：
    - 全名精确匹配：「张三」→「[姓名]」
    - 姓名间含空格：「张 三」「张  三」→「[姓名]」
    - 英文大小写不敏感：「john」「JOHN」「John」→「[姓名]」
    - 多次出现全部替换

    Args:
        text: 待脱敏的原文（扫描件文本 / LLM prompt 等）。
        name: 要脱敏的姓名明文。

    Returns:
        脱敏后的文本（所有 name 出现处替换为 ``[姓名]``）。

    Raises:
        TypeError: text 或 name 非 str。
    """
    if not isinstance(text, str):
        raise TypeError(f"text 必须是 str，收到 {type(text).__name__}")
    if not isinstance(name, str):
        raise TypeError(f"name 必须是 str，收到 {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        return text
    pattern = _build_pattern(stripped)
    return re.sub(pattern, REDACTED_PLACEHOLDER, text, flags=re.IGNORECASE)


def redact_names(text: str, names: list[str]) -> str:
    """批量脱敏多个姓名（按 names 顺序逐个替换）.

    为什么逐个替换而非合并为一个正则：不同姓名的匹配模式不同
    （CJK 无词边界、拉丁有词边界），逐个替换可分别构建正确模式。

    Args:
        text: 待脱敏的原文。
        names: 要脱敏的姓名列表（空/空白项自动跳过）。

    Returns:
        脱敏后的文本。
    """
    result = text
    for n in names:
        result = redact_name(result, n)
    return result


__all__ = [
    "REDACTED_PLACEHOLDER",
    "redact_name",
    "redact_names",
]
