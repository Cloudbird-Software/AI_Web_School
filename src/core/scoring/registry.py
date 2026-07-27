"""W3-S4 评分器插件统一契约与注册表.

落地 specs/contracts/registries/scorer.yaml 的 unified_contract：
  score(response, item_version, params) -> ScoreResult
  输出五要素：dimension_scores / error_inferences / confidence / evidence /
  scorer_version。

设计镜像 src/core/gate/validator.py（T-W2-007 验证器插件框架）：
- 注册制：register_scorer(pack_id, scorer) / get_scorer(scorer_id, pack_id)，
  按 pack_id 分桶——平台通用评分器 pack_id='platform'，学科包评分器
  pack_id='subject-math' 等；查找时学科桶未命中回退 platform。
- 宪法 D4：作答结构与评分结构只能来自平台注册表（scorer.yaml）；本模块是
  评分器**实现**的运行时入口，scorer_id 必须在 scorer.yaml 注册（由
  tests/contract/registries 双向闭合强制）。
- 宪法 A5/X6：核心域零学科特判——本模块不 import 任何学科包/学段包；
  学科评分器（如 math_equivalence）由学科包侧调用 register_scorer 注入。

为什么注册接受鸭子类型而非强制 Scorer 子类：W2 已有的
subject-math/scorers/math_equivalence.py 以模块级 score() + 句柄类实现
统一契约（scorer_id/version/score 三要素齐备），强制继承会要求改动已冻结
的 W2 成果；鸭子类型检查（hasattr 三要素）让新旧实现都能注册。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# ────────────────────────────────────────────────────────────────────
# 统一评分结果（scorer.yaml unified_contract.output_schema）
# ────────────────────────────────────────────────────────────────────

class ScoreResult(BaseModel):
    """评分器统一返回契约（scorer.yaml §output_schema 五要素）.

    - dimension_scores: { dimension_id: score }；客观题单维度 correct: 0|1，
      开放题多维度（R-Q-07）。
    - error_inferences: 错误类型推断（每条含 error_type_id/confidence/rule_version）。
    - confidence: { scoring: 0~1 }——置信度四层分离纪律：本字段只承载评分层，
      识别层（拍照链路）与推断层（error_inferences[].confidence）各自独立记录。
    - evidence: 评分证据（命中点/步骤判定/量规逐维理由），供审计与教研抽检。
    - scorer_version: 评分器版本（重判时据此写平行 score_run，R-D-05）。
    """

    model_config = ConfigDict(extra="forbid")

    dimension_scores: dict[str, float]
    error_inferences: list[dict[str, Any]] = Field(default_factory=list)
    confidence: dict[str, float]
    evidence: dict[str, Any] = Field(default_factory=dict)
    scorer_version: str


# ────────────────────────────────────────────────────────────────────
# 评分器协议与抽象基类
# ────────────────────────────────────────────────────────────────────

@runtime_checkable
class ScorerLike(Protocol):
    """评分器鸭子类型协议（注册时的最低要求）."""

    scorer_id: str
    version: str

    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """执行评分，返回具 ScoreResult 五要素属性的对象."""
        ...


class Scorer(ABC):
    """评分器抽象基类（平台通用评分器与新实现使用）.

    子类须声明类属性 scorer_id / version（与 scorer.yaml 注册 id 一致）。
    """

    scorer_id: ClassVar[str]
    version: ClassVar[str]
    deterministic: ClassVar[bool] = True

    @abstractmethod
    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        """评分主入口（scorer.yaml unified_contract.signature）."""
        ...


# ────────────────────────────────────────────────────────────────────
# 注册表（按 pack_id 分桶，platform 回退）
# ────────────────────────────────────────────────────────────────────

_SCORER_REGISTRY: dict[tuple[str, str], ScorerLike] = {}


def register_scorer(pack_id: str, scorer: ScorerLike) -> None:
    """注册评分器实例.

    Args:
        pack_id: 学科包 id（'platform' 或 'subject-math' 等）。
        scorer: 满足 ScorerLike 协议的对象（scorer_id/version/score 三要素）。

    Raises:
        TypeError: 缺三要素之一。
    """
    sid = getattr(scorer, "scorer_id", None)
    ver = getattr(scorer, "version", None)
    if not sid or not ver or not callable(getattr(scorer, "score", None)):
        raise TypeError(
            f"{scorer!r} 不满足评分器协议（需 scorer_id/version/score 三要素）"
        )
    _SCORER_REGISTRY[(pack_id, sid)] = scorer


def get_scorer(scorer_id: str, pack_id: str | None = None) -> ScorerLike:
    """取评分器实例：先查学科桶，未命中回退 platform 桶.

    Args:
        scorer_id: scorer.yaml 注册的评分器 id。
        pack_id: 题目所属学科包 id；None 时只查 platform 桶。

    Raises:
        KeyError: 未注册（含学科包评分器未加载的情形——学科包侧须先
            import 其 scorers 模块触发 register_scorer）。
    """
    if pack_id is not None:
        scorer = _SCORER_REGISTRY.get((pack_id, scorer_id))
        if scorer is not None:
            return scorer
    scorer = _SCORER_REGISTRY.get(("platform", scorer_id))
    if scorer is None:
        raise KeyError(
            f"评分器 {scorer_id!r} 未注册（pack_id={pack_id!r}；"
            "学科包评分器须先由学科包侧 import 注册）"
        )
    return scorer


def list_scorers(pack_id: str) -> list[str]:
    """列出某 pack 桶下已注册的 scorer_id（调试/测试用）."""
    return [sid for (pid, sid) in _SCORER_REGISTRY if pid == pack_id]


def reset_scorer_registry() -> None:
    """清空注册表（测试隔离用）."""
    _SCORER_REGISTRY.clear()


__all__ = [
    "ScoreResult",
    "Scorer",
    "ScorerLike",
    "get_scorer",
    "list_scorers",
    "register_scorer",
    "reset_scorer_registry",
]
