"""T-W4-037 注音覆盖组件（低段全文注音 → <ruby> 标签）.

为什么独立成组件：注音是低段专属能力，与 RenderIR 的文本块语义正交；
中/高段调用方完全不引用本组件（不注入注音层）。

设计要点：
- **纯函数 + 数据驱动**：phonetic 注解由调用方提供（学段包/学科包按字典
  生成，核心不感知拼音来源）；本组件只负责把注解应用到文本生成 <ruby>。
- **HTML 安全**：文本与拼音均经 html.escape，杜绝 XSS。
- **核心域零特判（A5）**：本组件不 import 学科包/学段包；拼音字典通过
  ``phonetic_map`` 参数注入。

输出示例（input "小鸟飞翔", map={"小":"xiǎo","鸟":"nǐao","飞":"fēi","翔":"xiáng"}）::

    <ruby>小<rp>(</rp><rt>xiǎo</rt><rp>)</rp></ruby>...

未在 map 中的字符原样输出（不强制全文注音；调用方决定覆盖范围）。
"""
from __future__ import annotations

import html
from typing import Mapping, Optional


def apply_phonetic_to_text(
    text: str,
    phonetic_map: Optional[Mapping[str, str]] = None,
) -> str:
    """为文本逐字应用注音，输出 <ruby> HTML 片段.

    Args:
        text: 待注音的纯文本（已是渲染最终态，变量替换后）。
        phonetic_map: {字符: 拼音} 映射；None 或空 → 原样返回（不注音）。
            调用方决定覆盖范围（full=全文 / out_of_syllabus=仅超纲字）。

    Returns:
        HTML 片段：在 map 中的字符包裹 <ruby>...<rt>拼音</rt></ruby>；
        未在 map 中的字符原样经 html.escape 输出。

    Notes:
        拼音中的声调符号（如 ǎ ē ī ō ū ǖ）是 Unicode 字符，HTML 直接支持，
        无需额外编码。``<rp>`` 提供不支持 ruby 的浏览器回退显示。
    """
    if not phonetic_map:
        # 无注音字典：仅做 HTML 转义（保持调用方安全契约）
        return html.escape(text, quote=True)

    out: list[str] = []
    for ch in text:
        pinyin = phonetic_map.get(ch)
        if pinyin is None:
            out.append(html.escape(ch, quote=True))
        else:
            # <ruby>字符<rp>(</rp><rt>拼音</rt><rp>)</rp></ruby>
            out.append(
                f"<ruby>{html.escape(ch, quote=True)}"
                f"<rp>(</rp><rt>{html.escape(pinyin, quote=True)}</rt>"
                f"<rp>)</rp></ruby>"
            )
    return "".join(out)


def has_phonetic_coverage(text: str, phonetic_map: Mapping[str, str]) -> bool:
    """检查文本是否至少有一个字符被注音（用于断言注音是否生效）.

    用于适配层断言：低段全文注音模式下，非空文本至少应有注音。
    """
    if not text or not phonetic_map:
        return False
    return any(ch in phonetic_map for ch in text)


__all__ = ["apply_phonetic_to_text", "has_phonetic_coverage"]
