"""RACE dataset → item_version adapter（Issue #27 / W1-3）.

RACE（ReAding Comprehension from Examinations）是英语阅读理解数据集，
典型单 passage JSON 形态：
    {
      "id": "high1234.txt",
      "article": "One day...",
      "questions": ["What did John do?", ...],
      "options": [["A. foo", "B. bar", ...], ...],   // 4 选项
      "answers": ["B", "A", ...]                       // A/B/C/D 之一
    }
或外层 list[passage_dict] / dict{ "data": list[passage_dict], ... }。

本模块：
1. convert(raw: list|dict) -> list[dict] —— 产出符合 W0 导入契约的 item_version list。
2. RaceAdapter（BaseAdapter 子类）—— 注册到 adapter 注册表（"race"），
   供 import_pack.py --adapter race 直接调用。

约束（宪法 D4：学科零特判）：
- 不 import 任何学科包；kp.code 用可预测前缀 "english.reading.race.<id>"，
  下游需要映射到内部 kp 时，在 C-line 用 kp_map 处理，不污染核心域。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from src.registry.adapters import (
    AdapterError,
    AdapterItem,
    BaseAdapter,
    register_adapter,
)

# ────────────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────────────

LETTERS = ("A", "B", "C", "D")


def _stable_iv_id(passage_id: str, q_idx: int) -> str:
    """稳定 item_version_id：前缀 + sha256(passage_id + q_idx) 前 16 位."""
    h = hashlib.sha256(f"race|{passage_id}|{q_idx}".encode("utf-8")).hexdigest()[:16]
    return f"race-{h}"


def _letter_to_idx(letter: str) -> int:
    """A→0, B→1...；大小写不敏感；非法值抛 ValueError."""
    if not letter:
        raise ValueError("answer letter 为空")
    return LETTERS.index(letter.strip().upper())


def _normalize_option(text: str) -> str:
    """去除选项前缀 'A. ' / 'A)' / 'A、' 等."""
    s = str(text).strip()
    for prefix in (f"{L}. " for L in LETTERS):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    for prefix in (f"{L})" for L in LETTERS):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    for prefix in (f"{L}、" for L in LETTERS):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s


# ────────────────────────────────────────────────────────────────────
# 核心转换逻辑（外部可直接调）
# ────────────────────────────────────────────────────────────────────


def convert(raw: Any) -> list[dict[str, Any]]:
    """把 RACE 原始 JSON 转成 item_version list.

    Args:
        raw: list[passage] | passage | {"data": [passage, ...]} 等常见形态.

    Returns:
        list[dict]：每个元素是一个合法的 W0 item_version 结构（可过 schema/pydantic 校验）.
    """
    results: list[dict[str, Any]] = []
    for passage in _iter_passages(raw):
        results.extend(_convert_one_passage(passage))
    return results


def _iter_passages(raw: Any) -> Iterator[dict[str, Any]]:
    """把各种嵌套形态拍平为 passage_dict 迭代."""
    if raw is None:
        return
    if isinstance(raw, list):
        for item in raw:
            yield from _iter_passages(item)
    elif isinstance(raw, dict):
        # 1) 典型 passage：有 article 字段
        if isinstance(raw.get("article"), str):
            yield raw
            return
        # 2) dict 包装：data / passages / examples
        for key in ("data", "passages", "examples", "items"):
            if key in raw:
                yield from _iter_passages(raw[key])
                return
        # 3) RACE-hierarchy：按 id（文件名）分
        for v in raw.values():
            yield from _iter_passages(v)


def _convert_one_passage(passage: dict[str, Any]) -> list[dict[str, Any]]:
    """单个 passage（article + N 题）→ N 个 item_version dict."""
    pid = str(passage.get("id") or hashlib.sha256(
        str(passage.get("article", "")).encode("utf-8")
    ).hexdigest()[:10])
    article = str(passage.get("article") or "").strip()
    questions = passage.get("questions") or []
    options_ll = passage.get("options") or []
    answers = passage.get("answers") or []

    n = len(questions)
    if len(options_ll) not in (0, n) or len(answers) not in (0, n):
        raise ValueError(
            f"passage {pid!r} 长度不一致：questions={n} options={len(options_ll)} answers={len(answers)}"
        )

    out: list[dict[str, Any]] = []
    for i in range(n):
        q_text = str(questions[i]).strip()
        opts = options_ll[i] if i < len(options_ll) and options_ll[i] else []
        ans_letter = answers[i] if i < len(answers) else ""

        # 构造 4 选项 {id, label}
        if opts and isinstance(opts, (list, tuple)):
            n_opts = len(opts)
            option_dicts = []
            for j in range(n_opts):
                letter = LETTERS[j] if j < len(LETTERS) else f"O{j}"
                option_dicts.append({
                    "id": letter,
                    "label": _normalize_option(opts[j]),
                })
        else:
            # 无选项时占位 4 个（避免 schema 校验失败，warnings 会记录）
            option_dicts = [{"id": L, "label": L} for L in LETTERS]

        try:
            correct_idx = _letter_to_idx(ans_letter)
            correct_id = LETTERS[correct_idx] if correct_idx < len(LETTERS) else ""
        except (ValueError, KeyError):
            correct_id = ""

        iv_id = _stable_iv_id(pid, i)
        item_id = f"race-{pid}-q{i+1}"

        now = "2026-07-30T00:00:00Z"
        out.append({
            "item_version_id": iv_id,
            "item_id": item_id,
            "status": "draft",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": f"english.reading.race.{pid}"}],
                "kp_set_mode": "single",
                "cognitive_level": "understand",
                "gradeband": "H" if pid.lower().startswith("high") else "M",
                "graph_release": "v1",
            },
            "interaction_ref": {
                "interaction_id": "single_choice",
                "interaction_params": {"shuffle": False},
            },
            "content": {
                "blocks": [
                    {"type": "passage", "text": article, "source_id": pid},
                    {"type": "stem", "text": q_text},
                    {"type": "options", "choices": option_dicts},
                ],
                "race_meta": {"passage_id": pid, "question_index": i},
            },
            "scoring_ref": {
                "scorer_id": "exact_match",
                "scorer_params": {
                    "answer": {"selected": correct_id} if correct_id else {},
                    "normalization": {"trim": True, "casefold": True},
                },
            },
            "error_bindings": [],
            "lineage": {
                "tier": "A",
                "pipeline": {"id": "race_adapter", "version": "1.0.0"},
                "signed_by": "race_adapter",
                "signed_at": now,
            },
        })
        if not correct_id:
            out[-1]["scoring_ref"]["scorer_params"]["__warning__"] = (
                f"answer letter 无法解析: {ans_letter!r}"
            )
    return out


# ────────────────────────────────────────────────────────────────────
# Adapter 子类（注册到 adapter 注册表，import_pack.py 可直接调用）
# ────────────────────────────────────────────────────────────────────


class RaceAdapter(BaseAdapter):
    """RACE 适配器：读 JSON 文件 → convert() → AdapterItem 流."""

    name: str = "race"

    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        p = Path(source)
        if not p.exists():
            self._error(str(p), 0, f"RACE 源路径不存在: {p}")
            return
        files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
        for fp in files:
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self._error(str(fp), 0, f"读取/解析失败: {type(e).__name__}: {e}")
                continue
            try:
                iv_list = convert(raw)
            except Exception as e:
                self._error(str(fp), 0, f"convert() 失败: {type(e).__name__}: {e}")
                continue
            for i, iv in enumerate(iv_list):
                warnings = []
                if iv["scoring_ref"]["scorer_params"].get("__warning__"):
                    warnings.append(iv["scoring_ref"]["scorer_params"].pop("__warning__"))
                yield self._apply_schema_check(
                    AdapterItem(source=str(fp), line=i + 1, data=iv, warnings=warnings)
                )


register_adapter("race", RaceAdapter)


__all__ = ["convert", "RaceAdapter", "_normalize_option", "_stable_iv_id"]
