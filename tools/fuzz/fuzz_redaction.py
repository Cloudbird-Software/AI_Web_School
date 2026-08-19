"""T-W0-010 atheris fuzz harness：PII 姓名 redaction 鲁棒性（宪法 D7）.

为什么 fuzz redaction：redact_name 是 PII 进入 LLM/TTS 前的最后一道纯代码
防线（fail-closed 链路的组成部分），任何崩溃（抛异常）或漏脱敏（输出仍含
姓名连续子串）都是合规事故。atheris 覆盖引导 fuzz 用随机变体持续探测
OCR 空格插入 / 大小写 / CJK-Latin 混排等边角。

运行方式（本地，CI 不执行本文件；W5-R 起 Go 原生 fuzz 进 gate）：
    uv pip install atheris && python tools/fuzz/fuzz_redaction.py -max_total_time=60

不变式（独立于实现正则推导，仅依据公开语义）：
1. 任意输入不抛异常（redaction 对任意字符串必须给出确定输出）；
2. 空白姓名 → 原样返回；
3. CJK 姓名在原文中连续出现 → 输出不再含该连续子串（漏脱敏即断言失败）；
4. 姓名在"去空白+小写"的宽松意义下都不存在 → 输出必须与原文一致（不误伤）；
5. 输出若被改写，必含「[姓名]」占位符（改写只允许来自脱敏替换）。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

try:
    import atheris
except ImportError as _exc:  # pragma: no cover - 环境保护
    raise SystemExit(
        "atheris 未安装。本地 fuzz 请先：uv pip install atheris（不进锁定文件）"
    ) from _exc

# 仓库根加入 sys.path（工具脚本直接运行时 src 包可导入）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

with atheris.instrument_imports():
    from src.core.compliance.redaction import (  # noqa: E402
        REDACTED_PLACEHOLDER,
        redact_name,
    )


def _is_pure_cjk(name: str) -> bool:
    """姓名是否全部由 CJK 表意文字构成（该类姓名无词边界语义）."""
    for ch in name:
        try:
            block = unicodedata.name(ch)
        except ValueError:
            return False
        if "CJK" not in block and "HIRAGANA" not in block and "KATAKANA" not in block:
            return False
    return bool(name)


def TestOneInput(data: bytes) -> None:  # noqa: N802 — atheris 约定命名
    fdp = atheris.FuzzedDataProvider(data)
    # Unicode 感知消费：redaction 处理的是学生文本，不是任意字节
    name = fdp.ConsumeUnicode(12)
    text = fdp.ConsumeUnicode(200)

    # 不变式 1：任意输入不抛异常
    result = redact_name(text, name)

    stripped = name.strip()
    # 不变式 2：空白姓名原样返回
    if not stripped:
        assert result == text, "空白姓名不得改写文本"
        return

    # 不变式 3：CJK 姓名连续出现必须被脱敏（CJK 无词边界，连续即命中）
    if _is_pure_cjk(stripped) and stripped in text:
        assert stripped not in result, f"漏脱敏：{stripped!r} 仍连续出现在输出"

    # 不变式 4：宽松意义（去空白+小写）下不存在 → 不误伤
    loose_text = re.sub(r"\s+", "", text).lower()
    loose_name = re.sub(r"\s+", "", stripped).lower()
    if loose_name not in loose_text:
        assert result == text, f"误伤：{stripped!r} 改写了不含姓名的文本"

    # 不变式 5：任何改写都来自占位符替换
    if result != text:
        assert REDACTED_PLACEHOLDER in result, "改写却不产生占位符，来源不明"


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
