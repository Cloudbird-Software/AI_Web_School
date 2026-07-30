"""W3-S4 平台通用确定性评分器：exact_match / keypoint_hit / stepwise_rubric.

实现 specs/contracts/registries/scorer.yaml 三个 active 评分器契约
（math_equivalence 为学科包评分器，见 subject-math/scorers/）：

- exact_match（精确匹配）：单选/多选/文本与数值填空/匹配/排序/作图操作；
  支持多空分项计分（partial_credit）与文本规范化（normalization）。
- keypoint_hit（关键点命中）：简答/文本填空的关键词/正则规则判定；
  正则方言锁定 Python re 子集（契约：禁后向引用/原子组/条件断言，保证
  跨版本重放行为一致，R-D-05）。
- stepwise_rubric（分步给分）：综合题拆有序步骤，逐步调子评分器汇总
  （R-Q-15；步骤级判分与知识点归因随 scorer_params 版本化）。

宪法 D4：本模块只**实现** scorer.yaml 已注册的结构，不私造新评分结构；
学科包只能复用与参数化（scorer_params 随 item_version 版本化），不得私造。

选项→错误类型映射不在本模块：选择题的错误推断证据是「选某项」（作答行为），
由 src/core/scoring/service.py 统一从 item_version.error_bindings 装配，
保证四种评分器的推断装配口径一致。
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Mapping

from src.core.scoring.registry import (
    ScoreResult,
    Scorer,
    _SCORER_REGISTRY,
    register_scorer,
)


# ────────────────────────────────────────────────────────────────────
# 公共工具
# ────────────────────────────────────────────────────────────────────

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict 或对象取属性（兼容 ORM/Pydantic/dict 三态，同 render 层惯例）."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _interaction_id_of(item_version: Any) -> str | None:
    """从 item_version 取 interaction_id（取不到返回 None，由调用方兜底）."""
    ref = _get(item_version, "interaction_ref") or {}
    return _get(ref, "interaction_id")


# 全角→半角翻译表（数字/大小写字母/常用符号），normalization.fullwidth_to_half 用
_FULLWIDTH_TABLE = {
    ord(chr(0xFF10 + i)): str(i) for i in range(10)
} | {
    ord(chr(0xFF21 + i)): chr(ord("A") + i) for i in range(26)
} | {
    ord(chr(0xFF41 + i)): chr(ord("a") + i) for i in range(26)
}

_FULLWIDTH_PUNCT_TABLE = {
    ord("＋"): "+",
    ord("－"): "-",
    ord("×"): "x",
    ord("÷"): "÷",
    ord("＝"): "=",
    ord("％"): "%",
    ord("／"): "/",
    ord("．"): ".",
    ord("，"): ",",
    ord("。"): "。",
    ord("；"): ";",
    ord("："): ":",
    ord("！"): "!",
    ord("？"): "?",
    ord("（"): "(",
    ord("）"): ")",
    ord("［"): "[",
    ord("］"): "]",
    ord("｛"): "{",
    ord("｝"): "}",
    ord("＜"): "<",
    ord("＞"): ">",
    ord("＠"): "@",
    ord("＃"): "#",
    ord("＆"): "&",
    ord("＊"): "*",
    ord("＿"): "_",
    ord("｜"): "|",
    ord("～"): "~",
    ord("￥"): "¥",
    ord("＂"): '"',
    ord("＇"): "'",
}

_FULLWIDTH_TABLE |= _FULLWIDTH_PUNCT_TABLE


def normalize_text(s: str, normalization: Mapping[str, Any] | None) -> str:
    """文本规范化（scorer.yaml exact_match.params_schema.normalization）.

    支持键：
    - strip（默认 True）：去首尾空白 + 折叠内部连续空白为单空格；
    - casefold：大小写折叠；
    - fullwidth_to_half：全角数字/字母转半角。
    """
    normalization = normalization or {}
    out = str(s)
    if normalization.get("fullwidth_to_half"):
        out = out.translate(_FULLWIDTH_TABLE)
    if normalization.get("strip", True):
        out = " ".join(out.split())
    if normalization.get("casefold"):
        out = out.casefold()
    return out


def _texts_equal(a: Any, b: Any, normalization: Mapping[str, Any] | None) -> bool:
    """规范化后比较两个标量（统一 str() 化，兼容 int answer 与 str 作答）."""
    return normalize_text(str(a), normalization) == normalize_text(str(b), normalization)


# ────────────────────────────────────────────────────────────────────
# exact_match 精确匹配
# ────────────────────────────────────────────────────────────────────

class ExactMatchScorer(Scorer):
    """exact_match：与标准答案精确比对，支持多空分项计分（scorer.yaml §57）.

    answer 形态（随交互类型）：
    - 标量：single_choice 的 option_id / 单空答案；
    - list：multi_choice 的正确选项集合（无序）或 ordering 的标准序列（有序，
      由 interaction_id=ordering 或 params.ordered=true 判定）；
    - dict：text_blank/numeric_blank 的 {blank_id: 答案}、matching 的
      {left_id: right_id}、drawing_operation 的 {element_id: state}。

    partial_credit：{"per_item": true} 时多部分题按命中比例给分
    （缺省全对才得分，契约原文）。
    """

    scorer_id: ClassVar[str] = "exact_match"
    version: ClassVar[str] = "1.0.0+platform"
    deterministic: ClassVar[bool] = True

    # 作答提取：interaction_id → (response 取值函数)
    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        params = params or {}
        answer = params.get("answer")
        if answer is None:
            # 参数缺失是配置错误：评分置信度置 0（无法判定≠判错）
            return ScoreResult(
                dimension_scores={"correct": 0.0},
                error_inferences=[],
                confidence={"scoring": 0.0},
                evidence={"reason": "scorer_params 缺 answer"},
                scorer_version=self.version,
            )

        normalization = params.get("normalization")
        per_item = bool((params.get("partial_credit") or {}).get("per_item"))
        interaction_id = _interaction_id_of(item_version)

        if isinstance(answer, Mapping):
            judgements = self._judge_mapping(
                answer, response, interaction_id, normalization
            )
        elif isinstance(answer, (list, tuple)):
            ordered = interaction_id == "ordering" or bool(params.get("ordered"))
            judgements = self._judge_sequence(
                list(answer), response, ordered, normalization
            )
        else:
            judgements = self._judge_scalar(answer, response, normalization)

        total = len(judgements)
        hits = sum(1 for j in judgements if j["ok"])
        if per_item and total > 0:
            correct = hits / total
        else:
            correct = 1.0 if total > 0 and hits == total else 0.0

        dimension_scores: dict[str, float] = {"correct": correct}
        if per_item:
            for j in judgements:
                dimension_scores[f"part:{j['part']}"] = 1.0 if j["ok"] else 0.0

        return ScoreResult(
            dimension_scores=dimension_scores,
            error_inferences=[],
            confidence={"scoring": 1.0},  # 确定性评分器
            evidence={
                "interaction_id": interaction_id,
                "judgements": judgements,
                "per_item": per_item,
            },
            scorer_version=self.version,
        )

    def _judge_scalar(
        self,
        answer: Any,
        response: Any,
        normalization: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """标量答案：single_choice.selected / 单值作答."""
        actual = _get(response, "selected", _get(response, "answer", _get(response, "value")))
        ok = actual is not None and _texts_equal(actual, answer, normalization)
        return [{
            "part": "answer",
            "expected": answer,
            "actual": actual,
            "ok": ok,
        }]

    def _judge_sequence(
        self,
        answer: list[Any],
        response: Any,
        ordered: bool,
        normalization: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """list 答案：multi_choice 集合比对（无序）/ ordering 序列比对（有序）."""
        actual = _get(response, "selected", _get(response, "sequence")) or []
        actual_list = [str(x) for x in actual]
        if ordered:
            judgements = []
            for i, exp in enumerate(answer):
                act = actual_list[i] if i < len(actual_list) else None
                judgements.append({
                    "part": f"pos{i + 1}",
                    "expected": exp,
                    "actual": act,
                    "ok": act is not None and _texts_equal(act, exp, normalization),
                })
            # 多出的元素视为错误位置（不影响标准位判定，仅记录）
            if len(actual_list) > len(answer):
                judgements.append({
                    "part": "extra",
                    "expected": None,
                    "actual": actual_list[len(answer):],
                    "ok": False,
                })
            return judgements
        # 无序集合：每个期望元素判定是否被选中；多选的错误项记一条
        actual_set = {normalize_text(x, normalization) for x in actual_list}
        judgements = []
        for exp in answer:
            judgements.append({
                "part": str(exp),
                "expected": exp,
                "actual": normalize_text(str(exp), normalization) in actual_set,
                "ok": normalize_text(str(exp), normalization) in actual_set,
            })
        extra = actual_set - {normalize_text(str(e), normalization) for e in answer}
        for x in sorted(extra):
            judgements.append({"part": str(x), "expected": None, "actual": x, "ok": False})
        return judgements

    def _judge_mapping(
        self,
        answer: Mapping[str, Any],
        response: Any,
        interaction_id: str | None,
        normalization: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """dict 答案：blanks（逐空）/ pairs（匹配）/ elements（作图状态集）."""
        if interaction_id == "matching" or _get(response, "pairs") is not None:
            pairs = _get(response, "pairs") or []
            actual = {str(_get(p, "left_id")): _get(p, "right_id") for p in pairs}
        elif interaction_id == "drawing_operation" or _get(response, "elements") is not None:
            elements = _get(response, "elements") or []
            actual = {str(_get(e, "element_id")): _get(e, "state") for e in elements}
        else:
            # text_blank / numeric_blank：blanks {blank_id: str | {value, unit?}}
            blanks = _get(response, "blanks") or {}
            actual = {}
            for bid, val in blanks.items():
                actual[str(bid)] = _get(val, "value") if isinstance(val, Mapping) else val

        judgements = []
        for key, exp in answer.items():
            exp_val = _get(exp, "value") if isinstance(exp, Mapping) else exp
            act = actual.get(str(key))
            judgements.append({
                "part": str(key),
                "expected": exp_val,
                "actual": act,
                "ok": act is not None and _texts_equal(act, exp_val, normalization),
            })
        return judgements


# ────────────────────────────────────────────────────────────────────
# keypoint_hit 关键点命中
# ────────────────────────────────────────────────────────────────────

# 未命中关键点→错误类型推断的默认置信度。
# 为什么 <1.0：规则判定是确定性的（scoring 层置信度 1.0），但「未命中某关键点
# → 某种错误理解」是证据非因果的推断（架构 v2 §4.5），推断层置信度须如实 <1。
DEFAULT_KEYPOINT_INFER_CONFIDENCE = 0.8


class KeypointHitScorer(Scorer):
    """keypoint_hit：关键词/要点 + 规则判定（scorer.yaml §111）.

    params.keypoints[*]：{id, patterns, score, error_type_id?, confidence?}
    - patterns 元素：普通字符串=子串命中（规范化后）；'re:' 前缀=正则命中
      （Python re 子集，契约锁定方言）。
    - error_type_id：该关键点未命中时可推断的错误类型（可空）。
    params.min_pass：通过分数线（可空；缺省=全部关键点命中才算对）。
    """

    scorer_id: ClassVar[str] = "keypoint_hit"
    version: ClassVar[str] = "1.0.0+platform"
    deterministic: ClassVar[bool] = True

    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        params = params or {}
        keypoints = params.get("keypoints") or []
        if not keypoints:
            return ScoreResult(
                dimension_scores={"correct": 0.0},
                error_inferences=[],
                confidence={"scoring": 0.0},
                evidence={"reason": "scorer_params 缺 keypoints"},
                scorer_version=self.version,
            )

        normalization = params.get("normalization")
        text = self._extract_text(response, normalization)

        dimension_scores: dict[str, float] = {}
        error_inferences: list[dict[str, Any]] = []
        kp_detail: list[dict[str, Any]] = []
        total = 0.0
        for kp in keypoints:
            patterns = kp.get("patterns") or []
            matched = self._match_any(text, patterns, normalization)
            hit = matched is not None
            score_val = float(kp.get("score", 0.0)) if hit else 0.0
            dimension_scores[f"kp:{kp['id']}"] = score_val
            total += score_val
            kp_detail.append({
                "id": kp["id"],
                "hit": hit,
                "matched_pattern": matched,
                "score": score_val,
            })
            if not hit and kp.get("error_type_id"):
                error_inferences.append({
                    "error_type_id": kp["error_type_id"],
                    "confidence": float(
                        kp.get("confidence", DEFAULT_KEYPOINT_INFER_CONFIDENCE)
                    ),
                    "rule_version": self.version,
                    "evidence": {"missed_keypoint": kp["id"]},
                })

        dimension_scores["total"] = total
        min_pass = params.get("min_pass")
        if min_pass is None:
            correct = 1.0 if all(d["hit"] for d in kp_detail) else 0.0
        else:
            correct = 1.0 if total >= float(min_pass) else 0.0
        dimension_scores["correct"] = correct

        return ScoreResult(
            dimension_scores=dimension_scores,
            error_inferences=error_inferences,
            confidence={"scoring": 1.0},
            evidence={
                "keypoints": kp_detail,
                "min_pass": min_pass,
            },
            scorer_version=self.version,
        )

    def _extract_text(
        self, response: Any, normalization: Mapping[str, Any] | None
    ) -> str:
        """提取作答文本：short_answer.text / text_blank.blanks 拼接 / 裸字符串."""
        if isinstance(response, str):
            return normalize_text(response, normalization)
        text = _get(response, "text")
        if text is not None:
            return normalize_text(str(text), normalization)
        blanks = _get(response, "blanks") or {}
        parts = []
        for val in blanks.values():
            parts.append(str(_get(val, "value") if isinstance(val, Mapping) else val))
        return normalize_text(" ".join(parts), normalization)

    def _match_any(
        self,
        text: str,
        patterns: list[str],
        normalization: Mapping[str, Any] | None,
    ) -> str | None:
        """逐模式命中判定；返回首个命中的 pattern（未命中 None）."""
        for pat in patterns:
            if pat.startswith("re:"):
                # 契约锁定正则方言：Python re 子集（禁实现相关特性由
                # 关键点表版本化评审保证，此处仅执行 re.search）
                if re.search(pat[3:], text):
                    return pat
            else:
                if normalize_text(pat, normalization) in text:
                    return pat
        return None


# ────────────────────────────────────────────────────────────────────
# stepwise_rubric 分步给分
# ────────────────────────────────────────────────────────────────────

class StepwiseRubricScorer(Scorer):
    """stepwise_rubric：结构化步骤 rubric，逐步独立判分汇总（scorer.yaml §88）.

    params.steps[*]：{step_id, scorer, scorer_params, max_score, kp?}
    - scorer：步骤级子评分器 id（本注册表现役确定性评分器）；
    - 步骤分 = 子评分器 dimension_scores['correct'](0~1) × max_score。

    子评分器查找：platform 桶优先，其次学科桶（get_scorer_any）——步骤级
    scorer id 是注册表全局 id（如 exact_match / math_equivalence），
    步骤定义本身随 item_version 版本化，学科语义由步骤参数承载。
    """

    scorer_id: ClassVar[str] = "stepwise_rubric"
    version: ClassVar[str] = "1.0.0+platform"
    deterministic: ClassVar[bool] = True

    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        params = params or {}
        steps = params.get("steps") or []
        if not steps:
            return ScoreResult(
                dimension_scores={"correct": 0.0},
                error_inferences=[],
                confidence={"scoring": 0.0},
                evidence={"reason": "scorer_params 缺 steps"},
                scorer_version=self.version,
            )

        # response.steps: [{step_id, response}] → {step_id: response}
        raw_steps = _get(response, "steps") or []
        answers = {str(_get(s, "step_id")): _get(s, "response") for s in raw_steps}

        dimension_scores: dict[str, float] = {}
        error_inferences: list[dict[str, Any]] = []
        step_detail: list[dict[str, Any]] = []
        total = 0.0
        max_total = 0.0
        min_conf = 1.0
        for step in steps:
            step_id = str(step["step_id"])
            max_score = float(step.get("max_score", 0.0))
            max_total += max_score
            sub = _get_scorer_any(str(step["scorer"]))
            sub_resp = answers.get(step_id)
            if sub_resp is None:
                # 缺步作答：0 分，scoring 置信度不降级（确定缺失）
                sub_result = ScoreResult(
                    dimension_scores={"correct": 0.0},
                    error_inferences=[{
                        "error_type_id": "missing_step",
                        "confidence": 1.0,
                        "rule_version": self.version,
                        "evidence": {"step_id": step_id},
                    }],
                    confidence={"scoring": 1.0},
                    evidence={"reason": "该步未作答"},
                    scorer_version=sub.version,
                )
            else:
                sub_result = sub.score(sub_resp, item_version, step.get("scorer_params") or {})
            sub_correct = float(sub_result.dimension_scores.get("correct", 0.0))
            points = sub_correct * max_score
            total += points
            dimension_scores[f"step:{step_id}"] = points
            min_conf = min(min_conf, float(sub_result.confidence.get("scoring", 1.0)))
            for inf in sub_result.error_inferences:
                entry = dict(inf)
                ev = dict(entry.get("evidence") or {})
                ev.setdefault("step_id", step_id)
                entry["evidence"] = ev
                error_inferences.append(entry)
            step_detail.append({
                "step_id": step_id,
                "scorer": step["scorer"],
                "max_score": max_score,
                "sub_correct": sub_correct,
                "points": points,
                "kp": step.get("kp"),
            })

        dimension_scores["total"] = total
        dimension_scores["correct"] = total / max_total if max_total > 0 else 0.0

        return ScoreResult(
            dimension_scores=dimension_scores,
            error_inferences=error_inferences,
            # 分步评分置信度 = 各步子评分置信度的最小值（串联取弱环）
            confidence={"scoring": min_conf},
            evidence={"steps": step_detail, "max_total": max_total},
            scorer_version=self.version,
        )


def _get_scorer_any(scorer_id: str):
    """platform 桶优先、其次各学科桶取评分器（步骤级子评分器查找）.

    为什么允许跨桶查找：stepwise 步骤引用的子评分器是注册表全局 id
    （exact_match 在 platform 桶，math_equivalence 在 subject-math 桶）；
    步骤定义随 item_version 版本化，不涉及运行时学科特判。
    """
    from src.core.scoring.registry import get_scorer

    try:
        return get_scorer(scorer_id)  # platform 桶
    except KeyError:
        pass
    for (pid, sid), scorer in sorted(_SCORER_REGISTRY.items()):
        if sid == scorer_id:
            return scorer
    raise KeyError(f"步骤级子评分器 {scorer_id!r} 未注册")


# ────────────────────────────────────────────────────────────────────
# 注册（import 即生效，与 gate validators/generic.py 同模式）
# ────────────────────────────────────────────────────────────────────

register_scorer("platform", ExactMatchScorer())
register_scorer("platform", KeypointHitScorer())
register_scorer("platform", StepwiseRubricScorer())


__all__ = [
    "DEFAULT_KEYPOINT_INFER_CONFIDENCE",
    "ExactMatchScorer",
    "KeypointHitScorer",
    "StepwiseRubricScorer",
    "normalize_text",
]
