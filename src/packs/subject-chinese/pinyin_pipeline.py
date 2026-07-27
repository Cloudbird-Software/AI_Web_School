"""T-W2-031 语文字词/拼音字库管线.

提供：
  - get_pinyin(word, context=None)：拼音生成，支持多音字按语境选择
  - load_corpus() / get_char_set() / get_word()：字词库加载与查询

依赖 pypinyin（MIT, license_id=lic-pypinyin-mit）做拼音生成；
字词库真源为本包 corpora/character_word.yaml（来源已登记在
content/sources/registry.yaml：lic-kebiao-chinese-2022 + lic-pypinyin-mit）。

宪法 X6：本包可 import 核心域，但核心域不 import 本包；
语文逻辑只允许出现在 src/packs/subject-chinese/ 下。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pypinyin import Style, pinyin

# ────────────────────────────────────────────────────────────────────
# 字词库 YAML 路径（本包内 corpora/character_word.yaml）
# ────────────────────────────────────────────────────────────────────
_CORPUS_PATH: Path = Path(__file__).resolve().parent / "corpora" / "character_word.yaml"


# ────────────────────────────────────────────────────────────────────
# 多音字语境表（键=多音字，值={语境词: 该语境下读音}）
# 为什么单独维护：pypinyin 内置词库覆盖有限，小学常见多音字需显式标注；
# 后续可由语料库的 polyphone 字段承载，本表为 v1 占位。
# ────────────────────────────────────────────────────────────────────
_POLYPHONE_CONTEXT: dict[str, dict[str, str]] = {
    "行": {"银行": "háng", "行列": "háng", "行走": "xíng", "自行车": "xíng", "行人": "xíng"},
    "长": {"长江": "cháng", "长短": "cháng", "长大": "zhǎng", "班长": "zhǎng", "师长": "zhǎng"},
    "得": {"得到": "dé", "得分": "dé", "得劲": "děi", "跑得快": "de", "写得好": "de"},
    "着": {"看着": "zhe", "想着": "zhe", "着急": "zháo", "着火": "zháo", "睡着": "zháo"},
    "地": {"地方": "dì", "地球": "dì", "慢慢地": "de", "快快地": "de"},
    "的": {"目的": "dì", "我的": "de", "你的": "de", "的确": "dí", "的士": "dí"},
    "了": {"了解": "liǎo", "明了": "liǎo", "走了": "le", "吃了": "le"},
    "还": {"还有": "hái", "还是": "hái", "还书": "huán", "归还": "huán"},
    "重": {"重要": "zhòng", "重大": "zhòng", "重新": "chóng", "重复": "chóng"},
    "少": {"多少": "shǎo", "少数": "shǎo", "少年": "shào", "少女": "shào"},
    "只": {"只有": "zhǐ", "只是": "zhǐ", "一只": "zhī", "两只": "zhī"},
    "为": {"因为": "wèi", "为了": "wèi", "作为": "wéi", "成为": "wéi"},
    "和": {"和平": "hé", "和气": "hé", "和诗": "hè", "和面": "huó"},
    "乐": {"快乐": "lè", "乐园": "lè", "音乐": "yuè", "乐曲": "yuè"},
    "觉": {"睡觉": "jiào", "觉得": "jué", "感觉": "jué"},
}


def get_pinyin(word: str, context: Optional[str] = None) -> str:
    """获取词语拼音，支持多音字按语境选择.

    Args:
        word: 汉字或词语（单字或多字）。
        context: 语境提示字符串。当 word 含多音字时，若 context 命中
            _POLYPHONE_CONTEXT 中的某个语境词，则用该语境读音覆盖该多音字。
            None 或未命中时用 pypinyin 默认读音。

    Returns:
        带声调拼音字符串，多字以空格分隔（如 "tóng xué"）。
        空字符串输入返回空串。

    Raises:
        TypeError: word 不是 str。

    Notes:
        - 非汉字字符（标点/数字/英文）按 pypinyin errors='default' 原样保留。
        - 多音字语境覆盖是字级别的：只替换命中的多音字，其余字用默认读音。
    """
    if not isinstance(word, str):
        raise TypeError(f"word 必须为 str，实际为 {type(word).__name__}")
    if not word:
        return ""

    # 多音字语境覆盖：构建 per-char 读音覆盖表
    override: dict[int, str] = {}  # 字索引 → 读音
    if context:
        for poly, ctx_map in _POLYPHONE_CONTEXT.items():
            if poly in word:
                # 在语境表中找匹配的语境词
                for ctx_key, py in ctx_map.items():
                    if ctx_key in context:
                        # 覆盖 word 中所有该多音字的位置
                        for i, ch in enumerate(word):
                            if ch == poly:
                                override[i] = py
                        break

    # pypinyin 取默认读音，然后按 override 替换
    parts = pinyin(word, style=Style.TONE, errors="default")
    result: list[str] = []
    for i, p in enumerate(parts):
        if i in override:
            result.append(override[i])
        else:
            result.append(p[0])
    return " ".join(result)


# ────────────────────────────────────────────────────────────────────
# 字词库加载与查询
# ────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def load_corpus() -> dict:
    """加载字词库 YAML（带 LRU 缓存，进程内只读一次）.

    Returns:
        dict：含 version / license_id / metadata / characters / words / confusion_pairs。

    Raises:
        FileNotFoundError: YAML 文件不存在。
    """
    if not _CORPUS_PATH.is_file():
        raise FileNotFoundError(f"字词库不存在: {_CORPUS_PATH}")
    with _CORPUS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_char_set(corpus: Optional[dict] = None) -> set[str]:
    """返回字库内所有汉字的集合（用于规范字表校验）.

    Args:
        corpus: 已加载的字词库 dict；None 则调用 load_corpus()。

    Returns:
        set[str]：字库内所有单字集合。
    """
    c = corpus if corpus is not None else load_corpus()
    return {ch["char"] for ch in c.get("characters", [])}


def get_word(word: str, corpus: Optional[dict] = None) -> Optional[dict]:
    """按词字符串查询词库记录.

    Args:
        word: 待查的词语字符串。
        corpus: 已加载的字词库 dict；None 则调用 load_corpus()。

    Returns:
        匹配的词记录 dict（含 id/word/characters/pinyin/gradeband/confusable_ids）；
        未找到返回 None。
    """
    c = corpus if corpus is not None else load_corpus()
    for w in c.get("words", []):
        if w["word"] == word:
            return w
    return None


def get_confusion_pairs(corpus: Optional[dict] = None) -> list[dict]:
    """返回混淆字对列表（形近/音近/形音近）.

    Args:
        corpus: 已加载的字词库 dict；None 则调用 load_corpus()。

    Returns:
        list[dict]：每条含 id/type/char_a/char_b/notes。
    """
    c = corpus if corpus is not None else load_corpus()
    return list(c.get("confusion_pairs", []))


__all__ = [
    "get_pinyin",
    "load_corpus",
    "get_char_set",
    "get_word",
    "get_confusion_pairs",
]
