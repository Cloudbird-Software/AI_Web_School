"""T-W4-049 英语单选 option_value 口径统一（字母↔释义）.

修复 W3 遗留：英语单选题选项值口径不一致——
- 评分器（exact_match）按字母匹配（scorer_params.answer = "A".."D"）；
- 但 error_bindings.option_value 历史装配为释义文本（vocab_single_choice.yaml
  distractor_rules.expression 指向 d1/d2/d3 释义）。
- 复习排程/错题归因按 option_value 比对 selected 时，字母口径与释义口径
  无法对齐，错题归因链路断开。

统一口径（本模块）：
- 存储层 / 评分层 / 错误绑定层：option_value 统一为标准字母 A/B/C/D；
- 展示层：按学段配置，低段（L）可显示释义，中高段（M/H）显示字母。

宪法 X6：英语逻辑只在 src/packs/subject-english/ 下；核心域零特判。
宪法 D4：交互与评分器只复用注册表，本模块不改评分器契约。

向后兼容：normalize_option 接受任意输入（字母/释义/混合），靠选项映射表
反查字母；既有数据（option_value=释义）经 normalize 后可与新数据（字母）
统一处理。展示层变化不影响评分结果（评分器只消费归一化后的字母）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

# 标准选项字母（单选四选项；多选亦复用此集合）
STANDARD_LETTERS: tuple[str, ...] = ("A", "B", "C", "D")

# 学段→展示模式默认值（低段释义优先，中高段字母优先）
_GRADEBAND_DEFAULT_MODE: dict[str, str] = {
    "L": "meaning",
    "M": "letter",
    "H": "letter",
}


def _normalize_letter(value: Any) -> Optional[str]:
    """单字符字母归一：'a'/'A' → 'A'；非标准字母返回 None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) != 1:
        return None
    upper = s.upper()
    return upper if upper in STANDARD_LETTERS else None


def _coerce_options(options: Any) -> dict[str, str]:
    """把多种 options 输入形态归一为 {letter: meaning} 字典.

    接受：
    - dict {letter: meaning}（原样保留）；
    - list[{letter, meaning}] / list[(letter, meaning)] / list[[letter, meaning]]；
    - list[dict] 含 option_value/letter + label/meaning 等键名变体。
    """
    if options is None:
        return {}
    out: dict[str, str] = {}
    if isinstance(options, Mapping):
        for k, v in options.items():
            letter = _normalize_letter(k)
            if letter is not None and isinstance(v, str):
                out[letter] = v
        return out
    if isinstance(options, Iterable):
        for item in options:
            if isinstance(item, Mapping):
                letter = _normalize_letter(item.get("letter") or item.get("id"))
                meaning = item.get("meaning") or item.get("label") or item.get("text")
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                letter = _normalize_letter(item[0])
                meaning = item[1]
            else:
                letter, meaning = None, None
            if letter is not None and isinstance(meaning, str):
                out[letter] = meaning
    return out


def normalize_option(value: Any, *, options: Any = None) -> Optional[str]:
    """将任意输入（字母/释义/混合）统一归一化为标准字母 A/B/C/D.

    Args:
        value: 输入选项值。可能是：
            - 字母 'A'/'a'（单字符）→ 直接归一为大写字母；
            - 释义文本（如 "春天"）→ 用 options 反查字母；
            - 其他类型 → 返回 None。
        options: 题目选项映射，用于释义→字母反查。接受 dict / list[dict] /
            list[tuple] 等形态（见 _coerce_options）。

    Returns:
        标准字母 'A'/'B'/'C'/'D'；无法归一化返回 None。

    设计要点：
    - 字母优先：若 value 本身是合法字母，直接返回（不查 options）；
    - 释义反查：value 是字符串且非字母时，在 options 里找 meaning=value
      的条目，返回其 letter；
    - 大小写/空白容错：' a ' → 'A'。
    """
    # 1. 字母直接归一
    letter = _normalize_letter(value)
    if letter is not None:
        return letter
    # 2. 释义反查（仅字符串输入有意义）
    if not isinstance(value, str):
        return None
    coerced = _coerce_options(options)
    for opt_letter, opt_meaning in coerced.items():
        if opt_meaning == value:
            return opt_letter
    # 3. 容错：释义可能含前后空白
    v = value.strip()
    if v != value:
        for opt_letter, opt_meaning in coerced.items():
            if opt_meaning == v:
                return opt_letter
    return None


def display_option(
    letter: Any,
    *,
    grade_band: str,
    mode: str = "auto",
    meaning: Optional[str] = None,
) -> str:
    """按展示模式返回字母或释义.

    Args:
        letter: 标准字母（A/B/C/D）；非合法字母抛 ValueError。
        grade_band: 学段 'L'/'M'/'H'。
        mode: 展示模式：
            - 'letter'：始终返回字母；
            - 'meaning'：返回释义（需提供 meaning，否则回退字母）；
            - 'auto'：按学段默认——L 段释义（低段识字量小，释义更友好），
              M/H 段字母（中高段标准化答题卡用字母）。
        meaning: 该字母对应的释义文本（mode=meaning/auto 时可能需要）。

    Returns:
        展示文本（字母或释义）。

    Raises:
        ValueError: letter 非标准字母 / grade_band 非法 / mode 非法。
    """
    norm = _normalize_letter(letter)
    if norm is None:
        raise ValueError(f"letter 非标准字母：{letter!r}")
    if grade_band not in _GRADEBAND_DEFAULT_MODE:
        raise ValueError(f"grade_band 必须 ∈ {sorted(_GRADEBAND_DEFAULT_MODE)}")
    if mode not in ("letter", "meaning", "auto"):
        raise ValueError(f"mode 必须 ∈ ['letter','meaning','auto']，实际 {mode!r}")

    effective = mode
    if mode == "auto":
        effective = _GRADEBAND_DEFAULT_MODE[grade_band]

    if effective == "letter":
        return norm
    # effective == "meaning"
    if meaning is None:
        # 释义缺失：回退字母（展示层降级，不阻断渲染）
        return norm
    return meaning


def normalize_error_bindings(
    error_bindings: list[dict[str, Any]],
    options: Any,
) -> list[dict[str, Any]]:
    """把 error_bindings 的 option_value 从释义归一化为字母.

    给英语包 A 线装配用：vocab_single_choice 模板的 distractor_rules.expression
    指向释义（d1/d2/d3），引擎装配出的 error_bindings.option_value 是释义文本。
    本函数把 option_value 统一为字母，使错误归因链路与评分器字母口径对齐。

    Args:
        error_bindings: 原始错误绑定列表（每条含 option_value 等）。
        options: 题目选项映射（同 normalize_option）。

    Returns:
        新列表（不修改输入）；option_value 已归一为字母。无法归一的条目
        保留原 option_value（向后兼容，不丢数据）。
    """
    coerced = _coerce_options(options)
    out: list[dict[str, Any]] = []
    for binding in error_bindings:
        new_binding = dict(binding)
        ov = binding.get("option_value")
        norm = normalize_option(ov, options=coerced)
        if norm is not None:
            new_binding["option_value"] = norm
        out.append(new_binding)
    return out


__all__ = [
    "STANDARD_LETTERS",
    "normalize_option",
    "display_option",
    "normalize_error_bindings",
]
