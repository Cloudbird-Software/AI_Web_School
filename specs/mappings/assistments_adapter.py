"""ASSISTments dataset → item_version adapter（Issue #30 / W1-2）.

ASSISTments 是小学数学题库导出格式，常见形态是 CSV 或 JSON list，
每道题一个 record，典型字段：
    {
      "problem_id": "12345",          // 题目 ID
      "skill_id": "skill-541",         // 技能 ID
      "skill_name": "Addition within 20",
      "question_text": "John has 3 apples...",
      "answer_text": "5",              // 标准答案
      "answer_type": "mcq|open_response|fill_in|algebraic",
      "choice_count": 4,               // (mcq 时) 选项数
      "choices": ["4", "5", "6", "7"], // (mcq 时) 选项文本
      "correct_choice_index": 1,       // (mcq 时) 正确选项 0-based
      "grade_level": "3",              // 学段 (3=Grade 3)
    }

本模块：
1. convert(records) -> list[item_version dict]
2. 加载 specs/kp_map/assistments_map.json：skill_id/skill_name → 内部 kp.code
3. AssistmentsAdapter（注册名 "assistments"）供 import_pack.py 调用。

文件约定：
    specs/kp_map/assistments_map.json = {
      "<skill_id_or_name>": "<internal_kp_code>",
      "__default__": "math.generic.unknown_skill"
    }
"""
from __future__ import annotations

import csv
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
# 工具 & KP 映射加载
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KP_MAP_PATH = _PROJECT_ROOT / "specs" / "kp_map" / "assistments_map.json"

DEFAULT_KP_MAP: dict[str, str] = {
    "__default__": "math.generic.unknown_skill",
}


def _stable_iv_id(problem_id: Any, skill_id: Any = "") -> str:
    h = hashlib.sha256(
        f"assist|{problem_id}|{skill_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"ast-{h}"


def _grade_to_gradeband(grade: Any) -> str:
    """3/4 → L（低段对应 1-2 实含 3-4 兼容映射，按项目 GradeBandPack 规则）；
    5/6 → M；7+ → H。None/非法 → M。
    """
    try:
        g = int(str(grade).strip())
    except Exception:
        return "M"
    if g <= 2:
        return "L"
    if g <= 4:
        return "M"
    return "H"


def load_kp_map(path: Path | None = None) -> dict[str, str]:
    """加载 ASSISTments skill → kp.code 映射；缺失时返回默认."""
    mp = dict(DEFAULT_KP_MAP)
    p = path or DEFAULT_KP_MAP_PATH
    if not p.is_file():
        return mp
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    mp[k] = v
    except Exception:
        pass
    return mp


def _resolve_kp(rec: dict[str, Any], kp_map: dict[str, str]) -> str:
    """优先用 skill_id 查，其次 skill_name，都没有 → __default__."""
    for key in ("skill_id", "skill_name"):
        v = str(rec.get(key) or "").strip()
        if v and v in kp_map:
            return kp_map[v]
    return kp_map.get("__default__", DEFAULT_KP_MAP["__default__"])


# ────────────────────────────────────────────────────────────────────
# convert
# ────────────────────────────────────────────────────────────────────


def convert(records: Any, kp_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """把 ASSISTments records（list[dict] 或 CSV 行迭代器或 JSON 嵌套）拍平."""
    if kp_map is None:
        kp_map = load_kp_map()
    out: list[dict[str, Any]] = []
    for rec in _iter_records(records):
        iv = _convert_one(rec, kp_map)
        if iv is not None:
            out.append(iv)
    return out


def _iter_records(records: Any) -> Iterator[dict[str, Any]]:
    if records is None:
        return
    if isinstance(records, dict):
        # 可能嵌套 { "data": [records], ... }
        for key in ("data", "records", "items", "problems"):
            if key in records:
                yield from _iter_records(records[key])
                return
        # 单条 record
        if any(k in records for k in ("problem_id", "question_text", "answer_text")):
            yield records
        return
    if isinstance(records, (list, tuple)):
        for r in records:
            yield from _iter_records(r)


def _convert_one(rec: dict[str, Any], kp_map: dict[str, str]) -> dict[str, Any] | None:
    pid = str(rec.get("problem_id") or rec.get("id") or "").strip()
    if not pid:
        return None
    q_text = str(rec.get("question_text") or rec.get("body") or "").strip()
    if not q_text:
        return None

    skill_id = str(rec.get("skill_id") or "").strip()
    skill_name = str(rec.get("skill_name") or "").strip()
    kp_code = _resolve_kp({"skill_id": skill_id, "skill_name": skill_name}, kp_map)
    answer_type = (str(rec.get("answer_type") or "fill_in")).lower()
    grade = rec.get("grade_level") or rec.get("grade") or "4"
    gradeband = _grade_to_gradeband(grade)

    now = "2026-07-30T00:00:00Z"
    iv_id = _stable_iv_id(pid, skill_id)
    item_id = f"ast-{pid}"

    # 判断 mcq 与否：mcq / multi_choice / answer_type='mcq'
    is_mcq = (
        answer_type in {"mcq", "multiple_choice", "single_choice"}
        or bool(rec.get("choices"))
        or (isinstance(rec.get("correct_choice_index"), int) and rec.get("choice_count"))
    )

    if is_mcq:
        choices_raw = rec.get("choices") or []
        if not choices_raw and rec.get("choice_count"):
            n = int(rec.get("choice_count") or 0)
            choices_raw = [str(rec.get(f"choice_{i}") or chr(65 + i)) for i in range(n)]
        # normalize choices
        choices_list: list[dict[str, str]] = []
        for j, c in enumerate(choices_raw):
            letter = chr(ord("A") + j) if j < 26 else f"O{j}"
            choices_list.append({"id": letter, "label": str(c).strip()})
        correct_idx = rec.get("correct_choice_index")
        correct_id = ""
        if isinstance(correct_idx, int) and 0 <= correct_idx < len(choices_list):
            correct_id = choices_list[correct_idx]["id"]
        elif rec.get("answer_text"):
            a = str(rec["answer_text"]).strip()
            for c in choices_list:
                if c["label"] == a:
                    correct_id = c["id"]
                    break
        return {
            "item_version_id": iv_id,
            "item_id": item_id,
            "status": "draft",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": kp_code}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": gradeband,
                "graph_release": "v1",
            },
            "interaction_ref": {
                "interaction_id": "single_choice",
                "interaction_params": {"shuffle": True},
            },
            "content": {
                "blocks": [
                    {"type": "stem", "text": q_text},
                    {"type": "options", "choices": choices_list},
                ],
                "assistments_meta": {
                    "problem_id": pid,
                    "skill_id": skill_id,
                    "skill_name": skill_name,
                    "answer_type": answer_type,
                },
            },
            "scoring_ref": {
                "scorer_id": "exact_match",
                "scorer_params": {
                    "answer": {"selected": correct_id} if correct_id else {},
                    "normalization": {"trim": True, "casefold": True, "fullwidth_to_half": True},
                },
            },
            "error_bindings": [],
            "lineage": {
                "tier": "A",
                "pipeline": {"id": "assistments_adapter", "version": "1.0.0"},
                "signed_by": "assistments_adapter",
                "signed_at": now,
            },
        }

    # 填空/开放：numeric_blank（数字）或 text_blank
    answer_text = str(rec.get("answer_text") or "").strip()
    is_numeric = bool(answer_text) and (
        answer_text.replace(".", "", 1).replace("-", "", 1).replace("/", "", 1).isdigit()
    )
    if is_numeric:
        interaction_id = "numeric_blank"
        scorer_id = "math_equivalence"
        scorer_params: dict[str, Any] = {
            "answer_expr": answer_text,
            "equivalence_rules": ["fraction_reduce", "unit_convert", "decimal_tolerance"],
            "tolerance": "0.0001",
        }
    else:
        interaction_id = "text_blank"
        scorer_id = "exact_match"
        scorer_params = {
            "answer": {"blanks": {"blank_1": answer_text}},
            "normalization": {"trim": True, "casefold": True, "fullwidth_to_half": True},
        }

    return {
        "item_version_id": iv_id,
        "item_id": item_id,
        "status": "draft",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": kp_code}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": gradeband,
            "graph_release": "v1",
        },
        "interaction_ref": {
            "interaction_id": interaction_id,
            "interaction_params": {"blank_count": 1},
        },
        "content": {
            "blocks": [{"type": "stem", "text": q_text}],
            "assistments_meta": {
                "problem_id": pid,
                "skill_id": skill_id,
                "skill_name": skill_name,
                "answer_type": answer_type,
            },
        },
        "scoring_ref": {
            "scorer_id": scorer_id,
            "scorer_params": scorer_params,
        },
        "error_bindings": [],
        "lineage": {
            "tier": "A",
            "pipeline": {"id": "assistments_adapter", "version": "1.0.0"},
            "signed_by": "assistments_adapter",
            "signed_at": now,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Adapter
# ────────────────────────────────────────────────────────────────────


class AssistmentsAdapter(BaseAdapter):
    """ASSISTments 适配器（注册名 "assistments"），支持 JSON 和 CSV 源."""

    name: str = "assistments"

    def __init__(
        self,
        schema: dict[str, Any] | None = None,
        pydantic_cls: Any | None = None,
        kp_map: dict[str, str] | None = None,
        kp_map_path: Path | None = None,
    ) -> None:
        super().__init__(schema=schema, pydantic_cls=pydantic_cls)
        self.kp_map = kp_map if kp_map is not None else load_kp_map(kp_map_path)

    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        p = Path(source)
        if not p.exists():
            self._error(str(p), 0, f"ASSISTments 源路径不存在: {p}")
            return
        files = sorted(p.rglob("*")) if p.is_dir() else [p]
        for fp in files:
            if fp.suffix.lower() in {".json"}:
                yield from self._iter_json(fp)
            elif fp.suffix.lower() in {".csv"}:
                yield from self._iter_csv(fp)
            # 忽略其他后缀（避免误读 README 等）

    def _iter_json(self, fp: Path) -> Iterator[AdapterItem]:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._error(str(fp), 0, f"JSON 读取/解析失败: {type(e).__name__}: {e}")
            return
        try:
            iv_list = convert(raw, kp_map=self.kp_map)
        except Exception as e:
            self._error(str(fp), 0, f"convert() 失败: {type(e).__name__}: {e}")
            return
        for i, iv in enumerate(iv_list):
            yield self._apply_schema_check(
                AdapterItem(source=str(fp), line=i + 1, data=iv)
            )

    def _iter_csv(self, fp: Path) -> Iterator[AdapterItem]:
        try:
            with fp.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except OSError as e:
            self._error(str(fp), 0, f"CSV 读取失败: {type(e).__name__}: {e}")
            return
        try:
            iv_list = convert(rows, kp_map=self.kp_map)
        except Exception as e:
            self._error(str(fp), 0, f"convert() 失败: {type(e).__name__}: {e}")
            return
        for i, iv in enumerate(iv_list):
            yield self._apply_schema_check(
                AdapterItem(source=str(fp), line=i + 2, data=iv)  # +2: header + 1-indexed
            )


register_adapter("assistments", AssistmentsAdapter)


__all__ = [
    "convert",
    "AssistmentsAdapter",
    "load_kp_map",
    "_grade_to_gradeband",
    "_stable_iv_id",
]
