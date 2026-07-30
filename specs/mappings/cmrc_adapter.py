"""CMRC/DRCD dataset → item_version adapter（Issue #28 / W1-4）.

CMRC 2017 / DRCD 是中文阅读理解数据集，单 passage JSON 典型形态（SQuAD-like）：
    {
      "context": "在一个阳光明媚的早晨……",
      "id": "TRAIN_186",
      "qas": [
        {
          "id": "TRAIN_186_QUERY_0",
          "question": "故事发生在什么时候？",
          "answers": [
            {"text": "早晨", "answer_start": 6},
            {"text": "阳光明媚的早晨", "answer_start": 4}
          ]
        },
        ...
      ]
    }
或外层 { "data": [ {"paragraphs": [ {"context": "...", "qas":[...]} ], ...} }

本模块：
1. convert(raw) -> list[item_version dict] —— CMRC/DRCD 转 W0 契约。
2. CmrcAdapter（注册名 "cmrc"）—— import_pack.py --adapter cmrc 可用。

注意：CMRC/DRCD 是 span-based QA，本模块默认映射到 exact_match + keypoint_hit
（基于答案文本集合），对应 interaction_id=short_answer 或 single_choice（若有
options 字段才视为单选）。学科零特判：kp.code = "chinese.reading.cmrc.<pid>"。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from src.registry.adapters import (
    AdapterItem,
    BaseAdapter,
    register_adapter,
)

# ────────────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────────────


def _stable_iv_id(passage_id: str, qa_id: str) -> str:
    h = hashlib.sha256(f"cmrc|{passage_id}|{qa_id}".encode("utf-8")).hexdigest()[:16]
    return f"cmrc-{h}"


# ────────────────────────────────────────────────────────────────────
# convert
# ────────────────────────────────────────────────────────────────────


def convert(raw: Any) -> list[dict[str, Any]]:
    """把 CMRC/DRCD 原始 JSON 拍平成 item_version list."""
    results: list[dict[str, Any]] = []
    for ctx, qas, pid in _iter_contexts(raw):
        results.extend(_convert_one_context(ctx, qas, pid))
    return results


def _iter_contexts(raw: Any) -> Iterator[tuple[str, list[dict[str, Any]], str]]:
    """拍平 SQuAD 形态：(context_text, qas_list, passage_id) 三元组流."""
    if raw is None:
        return
    if isinstance(raw, list):
        for r in raw:
            yield from _iter_contexts(r)
        return
    if isinstance(raw, dict):
        # 1) 直接是一个 context（context + id + qas）
        if isinstance(raw.get("context"), str) and "qas" in raw:
            ctx = raw["context"]
            qas = raw.get("qas") or []
            pid = str(raw.get("id") or hashlib.sha256(ctx.encode("utf-8")).hexdigest()[:10])
            yield ctx, qas, pid
            return
        # 2) SQuAD/CMRC：{"data": [{"title":"","paragraphs":[{"context":"","qas":[...]}]}]}
        if "data" in raw:
            yield from _iter_contexts(raw["data"])
            return
        if "paragraphs" in raw:
            for para in raw["paragraphs"] or []:
                yield from _iter_contexts(para)
            return
        if "title" in raw and isinstance(raw.get("paragraphs"), list):
            for para in raw["paragraphs"] or []:
                yield from _iter_contexts(para)
            return
        # 3) dict 形式 {key: passage}
        for v in raw.values():
            yield from _iter_contexts(v)


def _convert_one_context(
    ctx: str, qas: list[dict[str, Any]], pid: str
) -> list[dict[str, Any]]:
    ctx = str(ctx).strip()
    out: list[dict[str, Any]] = []
    now = "2026-07-30T00:00:00Z"
    for i, qa in enumerate(qas or []):
        q_text = str(qa.get("question") or "").strip()
        qa_id = str(qa.get("id") or f"{pid}-q{i+1}")
        answers = qa.get("answers") or []
        # 收集不重复的答案文本（CMRC 允许多答案）
        answer_texts: list[str] = []
        for a in answers:
            t = str(a.get("text") or "").strip()
            if t and t not in answer_texts:
                answer_texts.append(t)

        # 如果有 options 字段 → 视为单选（DRCD 某些子集带选项）
        opts = qa.get("options")
        if opts and isinstance(opts, (list, tuple)) and len(opts) >= 2:
            choices = []
            for j, o in enumerate(opts):
                letter = chr(ord("A") + j) if j < 26 else f"O{j}"
                choices.append({"id": letter, "label": str(o).strip()})
            correct_id = ""
            if answer_texts:
                first_answer = answer_texts[0]
                for c in choices:
                    if c["label"] == first_answer or (
                        len(first_answer) >= 2 and first_answer[0].upper() == c["id"]
                    ):
                        correct_id = c["id"]
                        break
            out.append({
                "item_version_id": _stable_iv_id(pid, qa_id),
                "item_id": f"cmrc-{pid}-{qa_id}",
                "status": "draft",
                "objective": {
                    "kp_set": [{"dimension": "kp", "code": f"chinese.reading.cmrc.{pid}"}],
                    "kp_set_mode": "single",
                    "cognitive_level": "understand",
                    "gradeband": "H",
                    "graph_release": "v1",
                },
                "interaction_ref": {
                    "interaction_id": "single_choice",
                    "interaction_params": {"shuffle": False},
                },
                "content": {
                    "blocks": [
                        {"type": "passage", "text": ctx, "source_id": pid},
                        {"type": "stem", "text": q_text},
                        {"type": "options", "choices": choices},
                    ],
                    "cmrc_meta": {"passage_id": pid, "qa_id": qa_id},
                },
                "scoring_ref": {
                    "scorer_id": "exact_match",
                    "scorer_params": {
                        "answer": {"selected": correct_id} if correct_id else {},
                        "normalization": {"trim": True, "fullwidth_to_half": True},
                    },
                },
                "error_bindings": [],
                "lineage": {
                    "tier": "A",
                    "pipeline": {"id": "cmrc_adapter", "version": "1.0.0"},
                    "signed_by": "cmrc_adapter",
                    "signed_at": now,
                },
            })
        else:
            # 简答（span match） → interaction=short_answer, scorer=keypoint_hit
            # keypoints：每个答案是一个关键点，任一命中即得分
            keypoints = []
            for j, at in enumerate(answer_texts):
                keypoints.append({
                    "id": f"ans-{j}",
                    "patterns": [at],
                    "score": 1.0 / max(1, len(answer_texts)),
                })
            out.append({
                "item_version_id": _stable_iv_id(pid, qa_id),
                "item_id": f"cmrc-{pid}-{qa_id}",
                "status": "draft",
                "objective": {
                    "kp_set": [{"dimension": "kp", "code": f"chinese.reading.cmrc.{pid}"}],
                    "kp_set_mode": "single",
                    "cognitive_level": "understand",
                    "gradeband": "H",
                    "graph_release": "v1",
                },
                "interaction_ref": {
                    "interaction_id": "short_answer",
                    "interaction_params": {"max_length": 200},
                },
                "content": {
                    "blocks": [
                        {"type": "passage", "text": ctx, "source_id": pid},
                        {"type": "stem", "text": q_text},
                    ],
                    "cmrc_meta": {
                        "passage_id": pid,
                        "qa_id": qa_id,
                        "reference_answers": answer_texts,
                    },
                },
                "scoring_ref": {
                    "scorer_id": "keypoint_hit",
                    "scorer_params": {
                        "keypoints": keypoints,
                        "min_pass": 0.5 if len(answer_texts) > 0 else 1.0,
                        "normalization": {
                            "trim": True, "fullwidth_to_half": True, "casefold": True
                        },
                    },
                },
                "error_bindings": [],
                "lineage": {
                    "tier": "A",
                    "pipeline": {"id": "cmrc_adapter", "version": "1.0.0"},
                    "signed_by": "cmrc_adapter",
                    "signed_at": now,
                },
            })
    return out


# ────────────────────────────────────────────────────────────────────
# Adapter
# ────────────────────────────────────────────────────────────────────


class CmrcAdapter(BaseAdapter):
    """CMRC/DRCD 适配器（注册名 "cmrc"）."""

    name: str = "cmrc"

    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        p = Path(source)
        if not p.exists():
            self._error(str(p), 0, f"CMRC/DRCD 源路径不存在: {p}")
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
                yield self._apply_schema_check(
                    AdapterItem(source=str(fp), line=i + 1, data=iv)
                )


register_adapter("cmrc", CmrcAdapter)


__all__ = ["convert", "CmrcAdapter"]
