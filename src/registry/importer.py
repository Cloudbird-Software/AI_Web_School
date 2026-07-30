"""Item 导入管道核心（Issue #26 / W1-1）.

流程：
  Adapter.iter_items() → [AdapterItem 流]
    → schema 校验
    → Pydantic 校验（ItemVersionImport）
    → 注册表交叉校验（interaction_id/scorer_id 存在且兼容）
    → DB 幂等写入（dry-run 跳过）
    → 聚合报告 → JSON 写入 out/import_reports/<ts>.json

幂等策略（宪法 D1 三本账只增不改）：
  1. item_version_id 存在且 item.status 一致 → skip（标记 DUPLICATE_IV）
  2. item_id 存在但 current_version_id ≠ 导入的 item_version_id →
     INSERT 新 item_version 行 + UPDATE item.current_version_id（同一事务，
     DEFERRABLE INITIALLY DEFERRED 允许环外键，见 models/item.py）
  3. item_id 不存在 → INSERT item + INSERT item_version（同一事务）
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

import jsonschema

from src.registry.adapters import (
    AdapterError,
    AdapterItem,
    BaseAdapter,
    get_adapter_cls,
    list_adapters,
)

# ────────────────────────────────────────────────────────────────────
# 导入报告数据类（最终序列化 JSON）
# ────────────────────────────────────────────────────────────────────


@dataclass
class ImportReport:
    """导入报告（out/import_reports/<timestamp>.json）."""

    # --- 元信息 ---
    timestamp: str  # ISO 8601，报告生成时间
    source: str  # CLI --source 值
    adapter: str  # CLI --adapter 值
    mode: Literal["dry-run", "commit"]  # dry-run / commit
    duration_ms: int = 0  # 总耗时（毫秒）
    report_file: str = ""  # 落盘后的报告文件路径（_finalize_report 回填）

    # --- 计数 ---
    total_seen: int = 0  # adapter 产出的 AdapterItem 总数（含未通过校验）
    validation_passed: int = 0  # 通过 schema/Pydantic/注册表 的条数
    validation_failed: int = 0  # 任一校验失败
    db_skipped_duplicate_iv: int = 0  # 幂等：item_version_id 已存在
    db_skipped_duplicate_item: int = 0  # 幂等：item_id 存在且 current_version 已相同
    db_created_item: int = 0  # 新 INSERT item 行
    db_created_iv: int = 0  # 新 INSERT item_version 行
    db_updated_current: int = 0  # UPDATE item.current_version_id
    db_error: int = 0  # DB 写入错误（事务回滚）

    # --- 明细 ---
    created_items: list[dict[str, str]] = field(default_factory=list)
    skipped_duplicates: list[dict[str, str]] = field(default_factory=list)
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    db_errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    adapter_errors: list[dict[str, Any]] = field(default_factory=list)
    # build_paper / downstream consumer 需要：校验通过的 pydantic 或 dict 对象列表
    # （以 pydantic obj 优先；非 pydantic 的 dict 路径保存 raw dict）。
    validation_passed_items: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # pydantic 对象不可 dataclasses.asdict 序列化 → 独立转换
        converted: list[Any] = []
        for it in self.validation_passed_items:
            if hasattr(it, "model_dump"):
                converted.append(it.model_dump(mode="json"))
            elif isinstance(it, dict):
                converted.append(dict(it))
            else:
                converted.append(it)
        d["validation_passed_items"] = converted
        return d


# ────────────────────────────────────────────────────────────────────
# 校验链（schema → pydantic → registry 交叉）
# ────────────────────────────────────────────────────────────────────


def _load_schema_and_pydantic() -> tuple[dict[str, Any], Any, Any]:
    """懒加载 W0 的 schema + pydantic + registries.

    返回 (schema_dict, ItemVersionImport_cls, (interaction_registry, scorer_registry))。
    import 失败抛 RuntimeError（loader 层将其记录到 report.validation_errors）。
    """
    import sys

    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    schema_path = project_root / "specs" / "item_version_import_schema.json"
    if not schema_path.is_file():
        raise RuntimeError(f"W0 schema 不存在: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    from specs.pydantic_item_version import ItemVersionImport
    from src.registry import get_interaction_registry, get_scorer_registry

    ir = get_interaction_registry()
    sr = get_scorer_registry()
    return schema, ItemVersionImport, (ir, sr)


def _validate_one(
    item: AdapterItem,
    schema: dict[str, Any],
    pydantic_cls: Any,
    registries: tuple[Any, Any],
) -> tuple[Any, list[str]]:
    """对单条 AdapterItem 依次执行 schema/Pydantic/registry 校验.

    Returns:
        (pydantic_obj, extra_warnings) —— 校验通过返回 Pydantic 实例；
        任一校验失败抛带明细的 ValueError。
    """
    warnings: list[str] = list(item.warnings)
    errors: list[str] = []
    ir, sr = registries

    # 1) JSON Schema
    try:
        jsonschema.validate(instance=item.data, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(f"JSON Schema: {e.json_path or '<root>'}: {e.message}")

    # 2) Pydantic（即使 schema 失败也尝试，提供更友好的错误）
    pydantic_obj = None
    try:
        pydantic_obj = pydantic_cls.model_validate(item.data)
    except Exception as e:  # pydantic.ValidationError 或其他
        errors.append(f"Pydantic: {e}")

    if pydantic_obj is not None:
        # 3) 注册表交叉（仅 Pydantic 通过后执行）
        iid = pydantic_obj.interaction_ref.interaction_id
        sid = pydantic_obj.scoring_ref.scorer_id
        try:
            it = ir.get_interaction(iid)
            if it.status != "active":
                errors.append(f"注册表: interaction_id={iid} status={it.status}（非 active）")
        except KeyError:
            errors.append(f"注册表: 未注册 interaction_id={iid!r}")
        try:
            sc = sr.get_scorer(sid)
            if sc.status != "active":
                errors.append(f"注册表: scorer_id={sid} status={sc.status}（非 active）")
        except KeyError:
            errors.append(f"注册表: 未注册 scorer_id={sid!r}")
        # 兼容矩阵
        if not errors:
            it = ir.get_interaction(iid)
            if sid not in it.compatible_scorers:
                errors.append(
                    f"注册表: scorer {sid!r} 不在 interaction {iid!r} 的 "
                    f"compatible_scorers={it.compatible_scorers}"
                )

    if errors:
        raise ValueError(" | ".join(errors))
    assert pydantic_obj is not None
    return pydantic_obj, warnings


# ────────────────────────────────────────────────────────────────────
# DB 幂等写入（懒加载：无 DB 环境时 commit 会报错，dry-run 正常）
# ────────────────────────────────────────────────────────────────────


def _try_get_async_session_factory():
    """尝试从 src.api.deps 获取 async session factory；无环境返回 None."""
    try:
        from src.api.deps import get_session_factory

        return get_session_factory()
    except Exception:
        return None


async def _commit_one(
    pydantic_obj: Any,
    report: ImportReport,
) -> None:
    """幂等写入单条 item + item_version.

    逻辑（见模块 docstring）：
      - item_version_id 存在 → skip
      - item_id 存在：
          current_version_id 相同 → skip；
          不同 → INSERT IV + UPDATE item.current_version_id
      - item_id 不存在 → INSERT item + INSERT IV

    无 session factory（无 DB 环境）：直接记 db_error 跳过，不抛异常（dry-run 无影响）。
    """
    import sqlalchemy as sa

    factory = _try_get_async_session_factory()
    if factory is None:
        report.db_error += 1
        report.db_errors.append({
            "item_id": pydantic_obj.item_id,
            "item_version_id": pydantic_obj.item_version_id,
            "reason": "DB session factory 不可用（未配置 POSTGRES_PASSWORD 或依赖缺失）",
        })
        return

    try:
        async with factory() as session:
            iv_id = pydantic_obj.item_version_id
            i_id = pydantic_obj.item_id

            # 1) item_version 存在？
            from sqlalchemy import text

            iv_exists = (await session.execute(
                text("SELECT 1 FROM item_version WHERE item_version_id = :id"),
                {"id": iv_id},
            )).scalar_one_or_none()
            if iv_exists is not None:
                report.db_skipped_duplicate_iv += 1
                report.skipped_duplicates.append({
                    "item_id": i_id,
                    "item_version_id": iv_id,
                    "reason": "DUPLICATE_IV: item_version_id 已存在",
                })
                return

            # 2) item 存在？
            item_row = (await session.execute(
                text("SELECT current_version_id, pack_id, tier FROM item WHERE item_id = :id"),
                {"id": i_id},
            )).fetchone()

            # 构造六大块 JSONB（直接用 Pydantic dict）
            obj_dict = pydantic_obj.model_dump(mode="json")
            # 从 content.csv_meta 取 pack_id/tier 作为 item 列默认（兼容 CSV adapter）
            csv_meta = (obj_dict.get("content") or {}).get("csv_meta") or {}
            pack_id = str(csv_meta.get("pack_id") or "generic-pack")
            tier = str(obj_dict["lineage"]["tier"])
            template_version_id = obj_dict["lineage"].get("template_version_id")

            if item_row is None:
                # --- 新 item ---
                await session.execute(
                    text(
                        "INSERT INTO item (item_id, pack_id, tier, template_version_id, current_version_id) "
                        "VALUES (:iid, :pack, :tier, :tv, :cvid)"
                    ),
                    {
                        "iid": i_id,
                        "pack": pack_id,
                        "tier": tier,
                        "tv": template_version_id,
                        "cvid": iv_id,
                    },
                )
                report.db_created_item += 1
                # INSERT item_version
                await _insert_item_version(session, obj_dict)
                report.db_created_iv += 1
                report.created_items.append({
                    "item_id": i_id,
                    "item_version_id": iv_id,
                    "action": "NEW_ITEM_AND_IV",
                })
            else:
                cur_vid = item_row[0]  # current_version_id
                if cur_vid == iv_id:
                    # 已是当前版本
                    report.db_skipped_duplicate_item += 1
                    report.skipped_duplicates.append({
                        "item_id": i_id,
                        "item_version_id": iv_id,
                        "reason": "DUPLICATE_ITEM: item 已存在且 current_version 已相同",
                    })
                    return
                # --- 新版本：INSERT IV + UPDATE current_version_id ---
                await _insert_item_version(session, obj_dict)
                report.db_created_iv += 1
                await session.execute(
                    text("UPDATE item SET current_version_id = :cvid WHERE item_id = :iid"),
                    {"cvid": iv_id, "iid": i_id},
                )
                report.db_updated_current += 1
                report.created_items.append({
                    "item_id": i_id,
                    "item_version_id": iv_id,
                    "action": "NEW_IV_UPDATE_CURRENT",
                    "previous_current_version_id": cur_vid or "",
                })
            await session.commit()
    except Exception as e:
        try:
            async with factory() as session:  # noqa: F821（factory 已绑定）
                await session.rollback()
        except Exception:
            pass
        report.db_error += 1
        report.db_errors.append({
            "item_id": pydantic_obj.item_id,
            "item_version_id": pydantic_obj.item_version_id,
            "error": f"{type(e).__name__}: {e}",
        })


async def _insert_item_version(session: Any, obj_dict: dict[str, Any]) -> None:
    """执行 INSERT INTO item_version ...（六大块 + 元数据）."""
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO item_version ("
            "  item_version_id, item_id, status, "
            "  objective, interaction_ref, content, scoring_ref, error_bindings, lineage, "
            "  rendered_snapshot, gate_certificate_id, published_at, retired_at"
            ") VALUES ("
            "  :ivid, :iid, :status, "
            "  :objective::jsonb, :iref::jsonb, :content::jsonb, :sref::jsonb, :ebind::jsonb, :lineage::jsonb, "
            "  :rend::jsonb, :gid, :pub::timestamptz, :ret::timestamptz"
            ")"
        ),
        {
            "ivid": obj_dict["item_version_id"],
            "iid": obj_dict["item_id"],
            "status": obj_dict["status"],
            "objective": json.dumps(obj_dict["objective"]),
            "iref": json.dumps(obj_dict["interaction_ref"]),
            "content": json.dumps(obj_dict["content"]),
            "sref": json.dumps(obj_dict["scoring_ref"]),
            "ebind": json.dumps(obj_dict["error_bindings"]),
            "lineage": json.dumps(obj_dict["lineage"]),
            "rend": json.dumps(obj_dict.get("rendered_snapshot")) if obj_dict.get("rendered_snapshot") else None,
            "gid": obj_dict.get("gate_certificate_id"),
            "pub": obj_dict.get("published_at"),
            "ret": obj_dict.get("retired_at"),
        },
    )


# ────────────────────────────────────────────────────────────────────
# 主入口：run_import（CLI 调用）
# ────────────────────────────────────────────────────────────────────


def run_import_sync(
    source: str | Path,
    adapter: str = "json",
    mode: Literal["dry-run", "commit"] = "dry-run",
    report_dir: str | Path | None = None,
    on_progress: Optional[Any] = None,
) -> ImportReport:
    """同步导入入口（测试/简单场景用；CLI 用 asyncio.run 调 async 版本）.

    实际执行是 async 的，内部用 asyncio.run 包一层以提供 sync 接口。
    """
    import asyncio

    return asyncio.run(
        run_import(
            source=source,
            adapter=adapter,
            mode=mode,
            report_dir=report_dir,
            on_progress=on_progress,
        )
    )


async def run_import(
    source: str | Path,
    adapter: str = "json",
    mode: Literal["dry-run", "commit"] = "dry-run",
    report_dir: str | Path | None = None,
    on_progress: Optional[Any] = None,
) -> ImportReport:
    """异步导入主入口（CLI 脚本直接调用）.

    Args:
        source: 源文件/目录路径（CLI --source）。
        adapter: 适配器名（json/csv/...，CLI --adapter）。
        mode: "dry-run" 只校验不落库；"commit" 写入 DB。
        report_dir: 报告输出目录；None 时使用 PROJECT_ROOT/out/import_reports。
        on_progress: 可选回调 `fn(count, total_seen, pydantic_obj_or_None, error_or_None)` ——
            每处理一条后触发，用于 CLI 进度输出。

    Returns:
        ImportReport：已落盘的报告对象。
    """
    t0 = time.monotonic()
    report = ImportReport(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=str(source),
        adapter=adapter,
        mode=mode,
    )

    # 1) 加载适配器
    try:
        adapter_cls = get_adapter_cls(adapter)
    except KeyError as e:
        report.adapter_errors.append({
            "source": str(source),
            "line": 0,
            "message": f"{e}；可用适配器: {list_adapters()}",
        })
        report = _finalize_report(report, t0, report_dir)
        return report

    # 2) 懒加载 schema/pydantic/registries
    try:
        schema, pydantic_cls, registries = _load_schema_and_pydantic()
    except Exception as e:
        report.validation_errors.append({
            "source": str(source),
            "line": 0,
            "item_version_id": "",
            "item_id": "",
            "error": f"依赖加载失败: {type(e).__name__}: {e}",
        })
        report.validation_failed += 1
        report = _finalize_report(report, t0, report_dir)
        return report

    # 3) 构造适配器实例并迭代
    adapter_instance: BaseAdapter = adapter_cls(schema=schema, pydantic_cls=pydantic_cls)
    count = 0
    try:
        for item in adapter_instance.iter_items(Path(source)):
            count += 1
            report.total_seen += 1
            try:
                pydantic_obj, extra_warnings = _validate_one(
                    item, schema, pydantic_cls, registries
                )
                report.validation_passed += 1
                report.validation_passed_items.append(pydantic_obj)
                for w in extra_warnings:
                    report.warnings.append({
                        "source": item.source,
                        "line": item.line,
                        "item_id": pydantic_obj.item_id,
                        "warning": w,
                    })
                # 写入 DB（仅 commit 模式）
                if mode == "commit":
                    await _commit_one(pydantic_obj, report)
                if on_progress:
                    on_progress(count, report.total_seen, pydantic_obj, None)
            except ValueError as e:
                report.validation_failed += 1
                report.validation_errors.append({
                    "source": item.source,
                    "line": item.line,
                    "item_version_id": item.data.get("item_version_id", ""),
                    "item_id": item.data.get("item_id", ""),
                    "error": str(e),
                })
                if on_progress:
                    on_progress(count, report.total_seen, None, str(e))
    except Exception as e:
        report.adapter_errors.append({
            "source": str(source),
            "line": 0,
            "message": f"适配器迭代异常: {type(e).__name__}: {e}",
        })

    # 4) 合并适配器自身记录的错误（如文件不存在）
    for ae in adapter_instance.errors:
        report.adapter_errors.append(asdict(ae))

    # 5) 落盘报告
    report = _finalize_report(report, t0, report_dir)
    return report


def _finalize_report(
    report: ImportReport,
    t0: float,
    report_dir: str | Path | None,
) -> ImportReport:
    report.duration_ms = int((time.monotonic() - t0) * 1000)
    if report_dir is None:
        project_root = Path(__file__).resolve().parents[2]
        report_dir = project_root / "out" / "import_reports"
    report_path = Path(report_dir)
    report_path.mkdir(parents=True, exist_ok=True)
    # 文件名：YYYYmmdd-HHMMSS-<adapter>-<mode>.json（避免 Windows 冒号问题）
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file_name = f"{ts}-{report.adapter}-{report.mode}.json"
    out = report_path / file_name
    # 先回填 report_file 到 report 对象，to_dict() 会自动包含
    report.report_file = str(out.resolve())
    out.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


# ────────────────────────────────────────────────────────────────────
# 公开
# ────────────────────────────────────────────────────────────────────

__all__ = [
    "ImportReport",
    "run_import",
    "run_import_sync",
]
