"""适配器子模块：将各种源格式转换为 ItemVersionImport 流.

设计（Issue #26 / W1-1）：
- 插件化：Adapter 基类 + register_adapter(name, cls) 注册表；
  新增数据集格式（ASSISTments/RACE/CMRC 等）只需新增 Adapter 子类，不改 loader 核心。
- 双适配器随附：JSON（校验 W0 导入 schema）、CSV（简单列映射 → item_version）。
- 幂等前置：适配器输出的 item_version_id 在写入层去重（见 loader.import_items）。

宪法 D4：核心域不 import 学科包；Adapter 是「格式转契约」的纯数据处理，
不含任何学科特判——学科语义在 pack_id 与 kp_set.code 中体现。
"""
from __future__ import annotations

import csv
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import jsonschema

# ────────────────────────────────────────────────────────────────────
# 导入上下文
# ────────────────────────────────────────────────────────────────────


@dataclass
class AdapterItem:
    """适配器输出的单条记录.

    Attributes:
        source: 源文件路径（或描述）
        line: 源文件行号（CSV 用）或索引（JSON 数组用），0 表示未提供
        data: ItemVersionImport 结构的 dict（尚未通过 Pydantic 校验）
        warnings: 转换阶段的非致命警告（如字段截断、默认值填充）
    """

    source: str
    line: int
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass
class AdapterError:
    """适配器阶段的致命/非致命错误（不中断整体处理，写入报告）."""

    source: str
    line: int
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────
# 适配器注册表
# ────────────────────────────────────────────────────────────────────

_ADAPTER_REGISTRY: dict[str, type["BaseAdapter"]] = {}


def register_adapter(name: str, cls: type["BaseAdapter"]) -> None:
    """注册适配器.

    Args:
        name: 适配器名（CLI --adapter 参数用），如 'json' / 'csv' / 'assistments'。
        cls: BaseAdapter 的子类。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("adapter name 必须是非空字符串")
    if not issubclass(cls, BaseAdapter):
        raise TypeError(f"{cls!r} 不是 BaseAdapter 子类")
    _ADAPTER_REGISTRY[name] = cls


def get_adapter_cls(name: str) -> type["BaseAdapter"]:
    """按名取适配器类；未注册抛 KeyError."""
    if name not in _ADAPTER_REGISTRY:
        raise KeyError(
            f"未注册的适配器: {name!r}；已注册: {sorted(_ADAPTER_REGISTRY)}"
        )
    return _ADAPTER_REGISTRY[name]


def list_adapters() -> list[str]:
    """列出已注册的适配器名（CLI --help 用）."""
    return sorted(_ADAPTER_REGISTRY)


# ────────────────────────────────────────────────────────────────────
# 基类
# ────────────────────────────────────────────────────────────────────


class BaseAdapter(ABC):
    """适配器抽象基类.

    子类职责：
    1. 接受一个或多个源文件路径；
    2. 产出 Iterator[AdapterItem]（每条 item_version 记录）；
    3. 转换错误以 AdapterError 形式通过 errors 回调抛出（或聚合到 self.errors 后返回）。
    """

    name: ClassVar[str] = "base"  # type: ignore[name-defined]  # 由子类覆盖

    def __init__(
        self,
        schema: dict[str, Any] | None = None,
        pydantic_cls: Any | None = None,
    ) -> None:
        """初始化适配器.

        Args:
            schema: 可选的 JSON Schema（用于 _apply_schema_check 快速预检）；
                None 时由 loader 层统一校验。
            pydantic_cls: 可选的 Pydantic 模型（同上）；None 时由 loader 层校验。
        """
        self.schema = schema
        self.pydantic_cls = pydantic_cls
        self.errors: list[AdapterError] = []

    @abstractmethod
    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        """迭代读取 source 并产出 AdapterItem.

        Args:
            source: 源文件路径（单个 JSON 文件，或包含多个 JSON 的目录，或 CSV 文件）。

        Yields:
            AdapterItem：每条是一个待导入的 item_version dict.
        """
        ...

    def _error(self, source: str, line: int, message: str, **detail: Any) -> None:
        """记录错误（不抛异常，调用方通过 self.errors 汇总到报告）."""
        self.errors.append(
            AdapterError(source=source, line=line, message=message, detail=dict(detail))
        )

    def _apply_schema_check(self, item: AdapterItem) -> AdapterItem:
        """如有 schema，对 item.data 做快速 JSON Schema 校验，失败写 warnings."""
        if self.schema is None:
            return item
        try:
            jsonschema.validate(instance=item.data, schema=self.schema)
        except jsonschema.ValidationError as e:
            item.warnings.append(f"schema 校验失败（loader 层会报错）：{e.message}")
        return item


# 提前引用：ClassVar 位于 typing
from typing import ClassVar  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# JSON Adapter
# ────────────────────────────────────────────────────────────────────


class JsonAdapter(BaseAdapter):
    """JSON 适配器：读取一个或多个 JSON 文件为 ItemVersionImport.

    支持两种形态：
    1. 单个 JSON 文件顶层是 list[item_version]（批量导入）；
    2. 单个 JSON 文件顶层是单个 item_version（单文件导入）；
    3. source 是目录：递归读取 *.json，按（2）或（1）处理。
    """

    name: ClassVar[str] = "json"

    def _read_one_file(self, path: Path) -> Iterator[AdapterItem]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            self._error(str(path), 0, f"读取文件失败: {e}")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._error(str(path), e.lineno or 0, f"JSON 解析失败: {e.msg}",
                        pos=e.pos, char=e.colno)
            return

        if isinstance(data, list):
            for i, obj in enumerate(data):
                if not isinstance(obj, dict):
                    self._error(str(path), 0,
                                f"数组第 {i} 个元素不是 object，跳过")
                    continue
                item = AdapterItem(source=str(path), line=i + 1, data=obj)
                yield self._apply_schema_check(item)
        elif isinstance(data, dict):
            item = AdapterItem(source=str(path), line=1, data=data)
            yield self._apply_schema_check(item)
        else:
            self._error(str(path), 0, f"JSON 顶层既不是 object 也不是 array: {type(data).__name__}")

    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        p = Path(source)
        if not p.exists():
            self._error(str(p), 0, f"源路径不存在: {p}")
            return
        if p.is_dir():
            # 目录：按文件名排序稳定迭代（幂等友好）
            for f in sorted(p.rglob("*.json")):
                if f.is_file():
                    yield from self._read_one_file(f)
        else:
            yield from self._read_one_file(p)


register_adapter("json", JsonAdapter)


# ────────────────────────────────────────────────────────────────────
# CSV Adapter
# ────────────────────────────────────────────────────────────────────

# Issue #26 约定的简单 CSV 列（所有列均为字符串；options_json/answer_json 为 JSON 序列化字符串）
CSV_COLUMNS: tuple[str, ...] = (
    "item_id",           # 题目逻辑 ID（跨版本不变）
    "pack_id",           # 学科/学段包 id（如 'subject-math-L'）
    "interaction_id",    # 交互类型 id（注册表中存在）
    "question_text",     # 题干文本（→ content.blocks[stem]）
    "options_json",      # 选项 JSON: [{"id":"A","label":"4"}, ...]（MCQ 用，可空）
    "answer_json",       # 答案 JSON: 结构随交互类型（MCQ: {"selected":"B"}；填空: {"blanks":{"b1":"苹果"}}）
    "gradeband",         # L/M/H（对应 objective.gradeband）
    "subject",           # 学科标识（math/chinese/english，用于构造默认 kp/code）
    "scorer_id",         # 评分器 id（可空，默认按交互类型猜）
    "kp_code",           # 知识点编码（可空，默认按 subject 生成占位）
)

# 默认值（CSV 列缺失时使用）
_DEFAULT_GRAPH_RELEASE = "v1"
_DEFAULT_PIPELINE_ID = "csv_import"
_DEFAULT_PIPELINE_VERSION = "v1"
_DEFAULT_SIGNED_BY = "csv_adapter"


def _content_id_hash(pack_id: str, item_id: str, seed: str = "") -> str:
    """为 CSV 导入的题目生成稳定 item_version_id（内容寻址占位）.

    Issue #26 Notes：导入 CSV 无天然哈希，用 pack_id+item_id+内容摘要 生成稳定 ID；
    正式发布时由 gate 服务按 §3 公式重新计算真正的内容寻址哈希。
    """
    h = hashlib.sha256()
    h.update(f"csv|{pack_id}|{item_id}|{seed}".encode("utf-8"))
    return "csv-" + h.hexdigest()[:16]


def _guess_scorer(interaction_id: str) -> str:
    """按交互类型猜默认评分器 id（CSV 列 scorer_id 为空时使用）."""
    mapping = {
        "single_choice": "exact_match",
        "multi_choice": "exact_match",
        "text_blank": "exact_match",
        "numeric_blank": "exact_match",
        "matching": "exact_match",
        "ordering": "exact_match",
        "drawing_operation": "exact_match",
        "short_answer": "keypoint_hit",
        "writing": "ai_rubric",
        "stepwise_process": "stepwise_rubric",
    }
    return mapping.get(interaction_id, "exact_match")


class CsvAdapter(BaseAdapter):
    """CSV 适配器：按约定列映射到 ItemVersionImport 结构.

    列定义见 CSV_COLUMNS 常量；options_json 与 answer_json 需为合法 JSON 字符串。
    可选列缺失时填默认值，记录 warnings。
    """

    name: ClassVar[str] = "csv"

    def iter_items(self, source: Path | str) -> Iterator[AdapterItem]:
        p = Path(source)
        if not p.is_file():
            self._error(str(p), 0, f"CSV 源文件不存在或非文件: {p}")
            return

        try:
            f = p.open("r", encoding="utf-8", newline="")
        except OSError as e:
            self._error(str(p), 0, f"打开文件失败: {e}")
            return

        with f:
            reader = csv.DictReader(f)
            # 检查必要列（缺列报错）
            missing_required = {"item_id", "question_text", "answer_json", "interaction_id"} - set(reader.fieldnames or [])
            if missing_required:
                self._error(str(p), 0,
                            f"CSV 缺少必填列: {sorted(missing_required)}；"
                            f"实际列: {reader.fieldnames}")
                return

            for lineno, row in enumerate(reader, start=2):  # header 是第 1 行
                warnings: list[str] = []
                try:
                    data = self._row_to_item_version(row, warnings)
                except Exception as e:
                    self._error(str(p), lineno, f"行转换失败: {type(e).__name__}: {e}",
                                row_summary={k: row.get(k) for k in ("item_id", "interaction_id")})
                    continue
                item = AdapterItem(source=str(p), line=lineno, data=data, warnings=warnings)
                yield self._apply_schema_check(item)

    def _row_to_item_version(
        self, row: dict[str, str], warnings: list[str]
    ) -> dict[str, Any]:
        """把一行 CSV 映射为 ItemVersionImport dict.

        失败抛异常（调用方捕获记为 AdapterError）。
        """
        item_id = row["item_id"].strip()
        if not item_id:
            raise ValueError("item_id 为空")

        pack_id = (row.get("pack_id") or "generic-pack").strip() or "generic-pack"
        interaction_id = row["interaction_id"].strip()
        question_text = row["question_text"]
        gradeband_raw = (row.get("gradeband") or "L").strip().upper()
        if gradeband_raw not in {"L", "M", "H"}:
            warnings.append(f"gradeband={gradeband_raw!r} 非法，回退为 'L'")
            gradeband = "L"
        else:
            gradeband = gradeband_raw
        subject = (row.get("subject") or "generic").strip().lower() or "generic"
        kp_code = (row.get("kp_code") or f"{subject}.generic.placeholder").strip()

        # options_json（可空）
        options = None
        if row.get("options_json") and row["options_json"].strip():
            try:
                options = json.loads(row["options_json"])
            except json.JSONDecodeError as e:
                raise ValueError(f"options_json 非法: {e.msg}") from e

        # answer_json（必填）
        try:
            answer = json.loads(row["answer_json"])
        except json.JSONDecodeError as e:
            raise ValueError(f"answer_json 非法: {e.msg}") from e

        # scorer_id（可空 → 猜）
        scorer_id = (row.get("scorer_id") or _guess_scorer(interaction_id)).strip()
        if not scorer_id:
            scorer_id = _guess_scorer(interaction_id)

        # 组装 content.blocks
        blocks: list[dict[str, Any]] = [{"type": "stem", "text": question_text}]
        if options is not None:
            blocks.append({"type": "options", "choices": options})

        # 生成稳定 item_version_id（内容寻址占位）
        item_version_id = _content_id_hash(pack_id, item_id, question_text[:80])

        # 组装六大块
        now_iso = "2026-07-30T00:00:00Z"  # 占位；提交层由 DB 写 created_at，lineage.signed_at 用占位避免时钟依赖
        return {
            "item_version_id": item_version_id,
            "item_id": item_id,
            "status": "draft",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": kp_code}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": gradeband,
                "graph_release": _DEFAULT_GRAPH_RELEASE,
            },
            "interaction_ref": {
                "interaction_id": interaction_id,
                "interaction_params": {"shuffle": True} if interaction_id
                in {"single_choice", "multi_choice"} else {},
            },
            "content": {
                "blocks": blocks,
                "csv_meta": {"pack_id": pack_id, "subject": subject},
            },
            "scoring_ref": {
                "scorer_id": scorer_id,
                "scorer_params": {
                    "answer": answer,
                    "partial_credit": None,
                    "normalization": {"trim": True},
                },
            },
            "error_bindings": [],
            "lineage": {
                "tier": "A",
                "pipeline": {"id": _DEFAULT_PIPELINE_ID, "version": _DEFAULT_PIPELINE_VERSION},
                "signed_by": _DEFAULT_SIGNED_BY,
                "signed_at": now_iso,
            },
        }


register_adapter("csv", CsvAdapter)


# ────────────────────────────────────────────────────────────────────
# 公开 API
# ────────────────────────────────────────────────────────────────────

__all__ = [
    # 数据类
    "AdapterItem",
    "AdapterError",
    # 注册表
    "register_adapter",
    "get_adapter_cls",
    "list_adapters",
    # 基类
    "BaseAdapter",
    # 具体适配器
    "JsonAdapter",
    "CsvAdapter",
    "CSV_COLUMNS",
]
