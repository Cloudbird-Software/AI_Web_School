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

# BUG-C2修复：常见三字姓名黑名单，用于避免2字短名误匹配3字长名前缀。
# 例：「张三」不匹配「张三丰」，但「张三」可匹配「张三同学」（「张三同」不在此列表中）。
# 列表来源于常见中文名，启发式覆盖常见情况；生产可扩展为完整姓名词库。
_COMMON_THREE_CHAR_NAMES: frozenset[str] = frozenset([
    "张三丰", "司马懿", "司马昭", "诸葛亮", "达尔文", "爱迪生",
    "拿破仑", "莫扎特", "贝多芬", "达芬奇", "米开朗", "高尔基",
    "托尔斯泰", "莎士比亚", "爱因斯坦", "马克思", "恩格斯", "列宁",
    "斯大林", "毛泽东", "周恩来", "刘少奇", "朱德", "邓小平",
    "刘德华", "张学友", "郭富城", "黎明", "周杰伦", "蔡依林",
    "李小龙", "李连杰", "甄子丹", "成龙", "洪金宝", "元彪",
    "张国荣", "梅艳芳", "谭咏麟", "陈奕迅", "林俊杰", "王力宏",
    "邓紫棋", "李荣浩", "薛之谦", "毛不易", "华晨宇", "吴亦凡",
    "李易峰", "鹿晗", "吴亦凡", "杨洋", "赵丽颖", "杨幂",
    "刘诗诗", "刘亦菲", "高圆圆", "范冰冰", "李冰冰", "赵薇",
    "林心如", "苏有朋", "陈志朋", "吴奇隆", "金城武", "林志玲",
    "贾玲", "沈腾", "马丽", "艾伦", "常远", "王宁",
    "徐峥", "黄渤", "王宝强", "陈思诚", "刘昊然", "吴京",
    "易烊千玺", "王俊凯", "王源", "张艺兴", "黄子韬", "吴亦凡",
])


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


def _is_cjk_in_range(char: str) -> bool:
    """判断字符是否在 CJK 统一表意文字基本区 \u4e00-\u9fff 范围内."""
    if len(char) != 1:
        return False
    cp = ord(char)
    return 0x4E00 <= cp <= 0x9FFF


def _build_pattern(name: str) -> tuple[str, bool]:
    """构建姓名匹配正则（返回模式和是否为纯CJK长姓名标记）.

    字符间允许任意空白（含零空白）：扫描件 OCR / 手工录入常在姓名间插入空格
    （如「张 三」「李  四」），脱敏须覆盖这些变体。

    边界判定（BUG-C2修复）：
    - 整姓名全部是CJK字符且长度>=2时：正则层面不加CJK lookaround边界（否则
      「请记录张三的成绩」中「张三」因前后CJK字符漏匹配），由 redact_name
      回调通过 _COMMON_THREE_CHAR_NAMES 黑名单双向检查是否为3字姓名的
      前缀/后缀部分（如「张三」不匹配「张三丰」、「三丰」不匹配「张三丰」）。
    - 非CJK姓名：附加 \\b 词边界，避免「John」误匹配「Johnson」。

    Returns:
        (正则字符串, 是否为纯CJK且长度>=2的姓名)
    """
    chars = list(name)
    all_cjk = len(chars) >= 2 and all(_is_cjk(c) for c in chars)
    any_cjk = any(_is_cjk(c) for c in chars)
    # 字符间允许任意空白（含零空白）
    pattern = r"\s*".join(re.escape(c) for c in chars)
    if not any_cjk:
        # 纯拉丁字母/数字：\b词边界
        pattern = r"\b" + pattern + r"\b"
    return pattern, all_cjk


def _extract_matched_name_chars(match_text: str, orig_name: str) -> str:
    """从匹配文本（可能含字符间空白）中还原纯字符序列."""
    return re.sub(r"\s+", "", match_text)


def redact_name(text: str, name: str) -> str:
    """将文本中出现的指定姓名替换为「[姓名]」占位符.

    纯字符串匹配（不做 OCR），支持常见姓名变体：
    - 全名精确匹配：「张三」→「[姓名]」
    - 姓名间含空格：「张 三」「张  三」→「[姓名]」
    - 英文大小写不敏感：「john」「JOHN」「John」→「[姓名]」
    - 多次出现全部替换
    - BUG-C2修复：2字CJK姓名不误匹配常见3字姓名前缀/后缀
      （如「张三」→「张三丰」、「三丰」→「张三丰」）。

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
    pattern, is_all_cjk_long = _build_pattern(stripped)
    stripped_chars = re.sub(r"\s+", "", stripped)
    name_len = len(stripped_chars)

    def _replace(m: re.Match[str]) -> str:
        # BUG-C2 启发式：2字纯CJK姓名匹配后，双向检查紧邻CJK字符是否
        # 与匹配字符组成常见3字姓名；若是则跳过（不误匹配长名前缀/后缀）。
        if is_all_cjk_long and name_len == 2:
            matched_clean = _extract_matched_name_chars(m.group(0), stripped)
            # 1) 后向：匹配末尾后紧邻非空白CJK字符
            end = m.end()
            nxt = end
            while nxt < len(text) and text[nxt].isspace():
                nxt += 1
            if nxt < len(text):
                nxt_char = text[nxt]
                if _is_cjk_in_range(nxt_char):
                    three_candidate = matched_clean + nxt_char
                    if three_candidate in _COMMON_THREE_CHAR_NAMES:
                        return m.group(0)
            # 2) 前向：匹配开头前紧邻非空白CJK字符
            start = m.start()
            prv = start - 1
            while prv >= 0 and text[prv].isspace():
                prv -= 1
            if prv >= 0:
                prv_char = text[prv]
                if _is_cjk_in_range(prv_char):
                    three_candidate = prv_char + matched_clean
                    if three_candidate in _COMMON_THREE_CHAR_NAMES:
                        return m.group(0)
        return REDACTED_PLACEHOLDER

    return re.sub(pattern, _replace, text, flags=re.IGNORECASE)


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
