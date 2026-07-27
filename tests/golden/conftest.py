"""黄金数据集 fixture 与加载器（T-W2-022）.

提供 ``golden_case`` 参数化 fixture：自动发现 ``tests/golden/items/**/*.yaml``
下的所有黄金用例，每条用例经 schema 校验后作为 ``GoldenCase`` 暴露给测试。

加载器对两类错误给出可定位的诊断信息：
  - YAML 语法错误：报告文件路径 + 行号（来自 yaml.YAMLError.problem_mark）；
  - 结构/字段错误：报告文件路径 + 字段路径（来自 pydantic.ValidationError.loc）。

为什么用 pydantic 而非 jsonschema 做运行时校验：
  - pydantic 已在依赖中（DSL schema 与 ORM 模型共用），不引入新依赖（X8）；
  - ``schema.yaml`` 仍是黄金数据集的 JSON Schema 契约文档（人类审查与
    外部工具校验入口），运行时校验由 pydantic 模型承载，二者字段保持对齐。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ────────────────────────────────────────────────────────────────────
# 路径常量
# ────────────────────────────────────────────────────────────────────
# tests/golden/conftest.py → tests/golden/ 下的 items/ 子目录
_GOLDEN_DIR: Path = Path(__file__).resolve().parent
_ITEMS_DIR: Path = _GOLDEN_DIR / "items"
_SCHEMA_FILE: Path = _GOLDEN_DIR / "schema.yaml"

# sha256: + 64 hex 小写；与 schema.yaml 的 pattern 一致
_SHA256_PATTERN: str = r"^sha256:[0-9a-f]{64}$"


# ────────────────────────────────────────────────────────────────────
# 加载错误（携带文件路径 + 行号/字段路径，便于定位）
# ────────────────────────────────────────────────────────────────────
class GoldenCaseLoadError(ValueError):
    """黄金用例加载错误.

    Attributes:
        path: 出错的 YAML 文件路径.
        line: 出错行号（1-based；0 表示非行级错误，如字段校验失败）.
        field_path: 出错字段路径（pydantic 错误用；YAML 语法错误为空串）.
        detail: 人类可读错误描述.
    """

    def __init__(
        self,
        path: Path,
        line: int,
        detail: str,
        field_path: str = "",
    ) -> None:
        self.path = path
        self.line = line
        self.field_path = field_path
        self.detail = detail
        loc = f" 行 {line}" if line > 0 else ""
        fld = f" 字段 [{field_path}]" if field_path else ""
        super().__init__(f"{path}{loc}{fld}: {detail}")


# ────────────────────────────────────────────────────────────────────
# Pydantic 模型（与 schema.yaml 字段对齐）
# ────────────────────────────────────────────────────────────────────
class _TemplateVersion(BaseModel):
    """母题版本（item_template_version 的序列化形态）."""

    model_config = ConfigDict(extra="forbid")

    template_version_id: str = Field(..., pattern=_SHA256_PATTERN)
    template_id: str = Field(..., min_length=1)
    dsl_version: str
    # spec 是六大块（objective/slots/variation_axes/presentation/
    # answer_program/distractor_rules），结构校验由
    # src.core.instantiation.dsl.schema.ItemTemplateSpec 承载；
    # 本模型只断言 spec 是 dict，避免与 DSL schema 双重维护。
    spec: dict[str, Any]


class GoldenCase(BaseModel):
    """黄金数据集单条用例.

    字段对齐 ``tests/golden/schema.yaml`` 的 JSON Schema 契约.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1)
    description: str
    template_version: _TemplateVersion
    params: dict[str, Any] = Field(..., min_length=1)
    pack_digest: str = Field(..., pattern=_SHA256_PATTERN)
    interaction_id: str = Field(..., min_length=1)
    scorer_id: str = Field(..., min_length=1)
    scorer_params: dict[str, Any]
    locale: str
    corpus_digests: list[str]
    seed: int = Field(..., ge=0)
    expected_item_version_id: str = Field(..., pattern=_SHA256_PATTERN)
    expected_content_snapshot: dict[str, Any]

    @field_validator("corpus_digests")
    @classmethod
    def _validate_corpus_digests(cls, v: list[str]) -> list[str]:
        """每个 corpus_digest 必须是 sha256: 形态（进入公式一）."""
        import re

        for d in v:
            if not re.fullmatch(_SHA256_PATTERN, d):
                raise ValueError(f"corpus_digest 非法：{d!r}")
        return v


# ────────────────────────────────────────────────────────────────────
# 发现与加载
# ────────────────────────────────────────────────────────────────────
def discover_golden_case_paths() -> list[Path]:
    """枚举 tests/golden/items/ 下所有 *.yaml 黄金用例文件.

    Returns:
        排序后的 Path 列表（排序保证跨平台收集顺序一致，便于回归比对）.
        items/ 不存在时返回空列表（W2b 初期 T-W2-023/024 尚未产出时）.
    """
    if not _ITEMS_DIR.is_dir():
        return []
    return sorted(_ITEMS_DIR.rglob("*.yaml"))


def load_golden_case(path: Path) -> GoldenCase:
    """加载并校验单个黄金用例 YAML.

    Args:
        path: YAML 文件路径.

    Returns:
        GoldenCase: 校验后的用例模型.

    Raises:
        GoldenCaseLoadError: YAML 语法错误或字段校验失败，含文件路径与
            行号/字段路径定位信息.

    Notes:
        - YAML 语法错误：报告行号（yaml.YAMLError.problem_mark.line + 1）；
        - 字段校验错误：报告字段路径（pydantic ValidationError.loc）；
        - 顶层非 mapping：报告文件级错误.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        # problem_mark.line 是 0-based；+1 转人类可读的 1-based
        line = (e.problem_mark.line + 1) if getattr(e, "problem_mark", None) else 0
        raise GoldenCaseLoadError(
            path, line, f"YAML 语法错误：{e.problem}" if hasattr(e, "problem") else f"YAML 语法错误：{e}"
        ) from e

    if not isinstance(raw, dict):
        raise GoldenCaseLoadError(
            path, 0, f"顶层必须为 mapping，实际为 {type(raw).__name__}"
        )

    try:
        return GoldenCase.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        # pydantic v2 的 ValidationError：errors() 返回字段错误列表
        from pydantic import ValidationError

        if isinstance(e, ValidationError):
            errs = e.errors()
            if errs:
                first = errs[0]
                loc = ".".join(str(x) for x in first.get("loc", []))
                msg = first.get("msg", "校验失败")
                raise GoldenCaseLoadError(
                    path, 0, f"{msg}", field_path=loc
                ) from e
        raise GoldenCaseLoadError(path, 0, f"校验失败：{e}") from e


# ────────────────────────────────────────────────────────────────────
# 参数化 fixture：每个黄金用例一个 case
# ────────────────────────────────────────────────────────────────────
# 为什么在模块级计算 params：pytest 在 collection 阶段读取 fixture params，
# 必须 import 时即可枚举。items/ 不存在时返回空列表，使用 golden_case 的
# 测试将因无参数而被 pytest 跳过（T-W2-023/024 产出样本后自然激活）。
_GOLDEN_CASE_PATHS: list[Path] = discover_golden_case_paths()
_GOLDEN_CASE_IDS: list[str] = [p.stem for p in _GOLDEN_CASE_PATHS]


@pytest.fixture(params=_GOLDEN_CASE_PATHS, ids=_GOLDEN_CASE_IDS)
def golden_case(request: pytest.FixtureRequest) -> GoldenCase:
    """参数化 fixture：返回当前黄金用例（已加载并校验）.

    每个 tests/golden/items/**/*.yaml 产出一个参数化 case；
    case_id 取文件名 stem（去扩展名），便于测试输出定位失败用例.
    """
    return load_golden_case(request.param)


__all__ = [
    "GoldenCase",
    "GoldenCaseLoadError",
    "discover_golden_case_paths",
    "load_golden_case",
]
