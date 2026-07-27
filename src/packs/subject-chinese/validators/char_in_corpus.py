"""语文学科验证器：字必须在字库内（T-W2-032）.

校验 ItemVersion.content 中的「目标词」全部汉字均出自本学科包字库
（src/packs/subject-chinese/corpora/character_word.yaml）。
目的：禁止库外字（生僻字/错字）混入已发布题目（任务卡 T-W2-032 §3）。

契约（与 T-W2-007 / T-W2-009 一致）：
  - validate(artifact_ref, ctx) -> ValidatorResult
  - 通过 register_validator 注册到本学科包 pack_id='subject-chinese'
  - 阻断项（blocking=True）：库外字 → verdict='fail'，编排器短路

宪法 X6：本模块属于学科包，可 import 核心域；核心域不 import 本模块。
宪法 D4：本验证器是学科包扩展，注册到 pack_id='subject-chinese'，与平台
通用验证器（schema/license/duplicate_placeholder）并存于策略链。

为什么直接读 YAML 而非 import pinyin_pipeline：本学科包目录名含连字符
（subject-chinese），无法作为 Python 包名导入；与 subject-math 的
variable-types/functions.py 同样使用 importlib 或直接文件 IO 加载同目录资源。
本验证器只需字库汉字集合，直接读 YAML 最简。
"""
from __future__ import annotations

import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

# 字库 YAML 路径（本包内 corpora/character_word.yaml）
_CORPUS_PATH: Path = (
    Path(__file__).resolve().parent.parent / "corpora" / "character_word.yaml"
)


# ────────────────────────────────────────────────────────────────────
# 汉字识别：Unicode CJK 统一表意文字基本区 + 扩展 A 区（小学覆盖足够）
# 为什么不依赖 pypinyin 的汉字判断：pypinyin errors='default' 对非汉字原样保留，
# 我们需要明确知道哪些字符是「汉字」才能做「库外字」判定。
# ────────────────────────────────────────────────────────────────────


def _is_hanzi(ch: str) -> bool:
    """判断单字符是否为汉字（CJK 统一表意文字基本区 + 扩展 A 区）.

    覆盖范围：
      - U+4E00..U+9FFF：CJK 统一表意文字（基本区，覆盖小学全部用字）
      - U+3400..U+4DBF：CJK 扩展 A 区（部分生僻字）

    Notes:
        扩展 B-F 区（U+20000+）小学不会出现，故意不覆盖以免误判。
    """
    if not isinstance(ch, str) or len(ch) != 1:
        return False
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF)


def _extract_hanzi(text: str) -> list[str]:
    """从字符串中提取全部汉字（保留重复，便于报告具体位置）."""
    return [c for c in text if _is_hanzi(c)]


# ────────────────────────────────────────────────────────────────────
# 字库加载（带 LRU 缓存，进程内只读一次）
# ────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_char_set() -> frozenset[str]:
    """加载字库 YAML，返回全部汉字的不可变集合.

    Raises:
        FileNotFoundError: 字库 YAML 不存在。
    """
    if not _CORPUS_PATH.is_file():
        raise FileNotFoundError(f"字词库不存在: {_CORPUS_PATH}")
    with _CORPUS_PATH.open(encoding="utf-8") as f:
        corpus = yaml.safe_load(f)
    return frozenset(ch["char"] for ch in corpus.get("characters", []))


# ────────────────────────────────────────────────────────────────────
# 字规范验证器
# ────────────────────────────────────────────────────────────────────


class CharInCorpusValidator(Validator):
    """语文字规范验证器：题目目标词的汉字必须在字库内.

    ctx 字段（extra='allow'）：
    - artifact_payload: dict——ItemVersion 内容快照（含 lineage.params.normalized.word）。
    - target_word: str——目标词（可空；为空时从 lineage.params.normalized.word 取）。

    verdict 规则：
    - fail：目标词含字库外汉字（列举外字）。
    - pass：全部汉字在字库内。
    - review：未提供目标词且无法从 payload 推断（转人工）。
    """

    validator_id = "char_in_corpus"
    version = "1.0.0+subject-chinese"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload: dict[str, Any] | None = ctx.artifact_payload
        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None，无法提取目标词"},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 取目标词：优先 ctx.target_word；其次 lineage.params.normalized.word
        target_word: str | None = getattr(ctx, "target_word", None)
        if not target_word:
            # 从 lineage.params.normalized.word 取（instantiate 产物的标准位置）
            lineage = payload.get("lineage") or {}
            params = (lineage.get("params") or {}) if isinstance(lineage, dict) else {}
            normalized = params.get("normalized") or {}
            target_word = normalized.get("word")

        if not target_word:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "未提供 target_word 且无法从 lineage 推断"},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 字库汉字集合
        char_set = _load_char_set()

        # 提取目标词中的全部汉字，逐字校验
        hanzi_list = _extract_hanzi(target_word)
        out_of_corpus: list[str] = []
        for ch in hanzi_list:
            if ch not in char_set:
                out_of_corpus.append(ch)

        if out_of_corpus:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "目标词含字库外汉字",
                    "target_word": target_word,
                    "out_of_corpus_chars": out_of_corpus,
                    "char_corpus_size": len(char_set),
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "target_word": target_word,
                "hanzi_count": len(hanzi_list),
                "char_corpus_size": len(char_set),
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# ────────────────────────────────────────────────────────────────────
# 注册：pack_id='subject-chinese'
# ────────────────────────────────────────────────────────────────────
# 为什么无条件 register：与 T-W2-009 generic.py 一致——模块导入即注册，
# 编排器按策略链查找时可通过 get_validator('subject-chinese', 'char_in_corpus') 取到。
register_validator("subject-chinese", CharInCorpusValidator)


__all__ = ["CharInCorpusValidator"]
