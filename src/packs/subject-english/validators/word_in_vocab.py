"""英语学科验证器：词表等级校验（W3 S7）.

校验 ItemVersion 的「目标词」出自本学科包课标词表
（src/packs/subject-english/corpora/curriculum_words.json）。
目的：词表外单词（超纲词/拼写错误词）禁止混入已发布题目
（架构 v2 §4.3 英语包验证器：课标词表等级校验——词表外词必须标注或替换）。

契约（与 T-W2-009 / char_in_corpus 一致）：
  - validate(artifact_ref, ctx) -> ValidatorResult
  - 通过 register_validator 注册到本学科包 pack_id='subject-english'
  - 阻断项（blocking=True）：词表外词 → verdict='fail'，编排器短路

等级语义：词表当前仅收录二级词（5-6 年级，gradeband H）；
校验「词在词表内」即含等级校验（不在二级表 = 超纲或错词）。
一级/预备级词表后续波次补充后，本验证器升级为按目标等级区间判定。

宪法 X6：本模块属于学科包，可 import 核心域；核心域不 import 本模块。
为什么直接读 JSON 而非走 corpus_loader：词表只需「单词集合+等级」，
直接文件 IO 最简（与 char_in_corpus 读 YAML 同构）。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

# 课标词表 JSON 路径（本包内 corpora/curriculum_words.json）
_VOCAB_PATH: Path = (
    Path(__file__).resolve().parent.parent / "corpora" / "curriculum_words.json"
)


@lru_cache(maxsize=1)
def _load_vocab() -> dict[str, dict[str, Any]]:
    """加载课标词表，返回 {小写单词: 词条目} 映射（进程内只读一次）.

    Raises:
        FileNotFoundError: 词表 JSON 不存在。
    """
    if not _VOCAB_PATH.is_file():
        raise FileNotFoundError(f"课标词表不存在: {_VOCAB_PATH}")
    with _VOCAB_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return {w["word"].lower(): w for w in data.get("words", [])}


class WordInVocabValidator(Validator):
    """英语词表等级校验：题目目标词必须在课标词表内.

    ctx 字段（extra='allow'）：
    - artifact_payload: dict——ItemVersion 内容快照（含 lineage.params.normalized.word）。
    - target_word: str——目标词（可空；为空时从 lineage.params.normalized.word 取）。

    verdict 规则：
    - fail：目标词不在课标词表（词表外词/拼写错误词）。
    - pass：词在词表内（evidence 含 level/theme/pos）。
    - review：未提供目标词且无法从 payload 推断（转人工）。
    """

    validator_id = "word_in_vocab"
    version = "1.0.0+subject-english"
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

        vocab = _load_vocab()
        entry = vocab.get(str(target_word).lower())

        if entry is None:
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": "目标词不在课标词表内（词表外词/拼写错误词）",
                    "target_word": target_word,
                    "vocab_size": len(vocab),
                    "vocab_level": "二级",
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "target_word": target_word,
                "level": entry.get("level"),
                "theme": entry.get("theme"),
                "pos": entry.get("pos"),
                "vocab_size": len(vocab),
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# ────────────────────────────────────────────────────────────────────
# 注册：pack_id='subject-english'（模块导入即注册，与 generic.py 同约定）
# ────────────────────────────────────────────────────────────────────
register_validator("subject-english", WordInVocabValidator)


__all__ = ["WordInVocabValidator"]
