"""T-W2-021 任务卡遥测：JSONL 记录、读取、聚合（无第三方依赖）。

任务卡 deliverables 列出路径为 ``tools/opc/telemetry.py``，但 ``tools/opc`` 当前是
一个文件（CLI 入口），且 ``tests/unit/test_opc.py`` 用 ``SourceFileLoader`` 直接
加载它——若把 ``tools/opc`` 改为目录，旧测试的 loader 会失败（无法加载目录）。
本卡 owner_module=tools/，禁止修改 tests/，故将遥测模块放在 ``tools/telemetry.py``
作为 ``tools/opc`` 的兄弟文件。``tools/opc dashboard`` 仍读 ``events-*.jsonl``，
无需修改即可与本模块写入的文件兼容。

文件命名：``events-YYYYMM.jsonl``（连字符，与 .agent/telemetry/README.md 与
``tools/opc`` 的 glob ``events-*.jsonl`` 一致）。任务卡文字写作 ``events_YYYYMM.jsonl``
（下划线），与 README/dashboard 既有约定不一致；以 README + dashboard 为准，
本模块用连字符命名。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT: Path = Path(__file__).resolve().parent.parent
TELEMETRY_DIR: Path = ROOT / ".agent" / "telemetry"


def _resolve_tele_dir(tele_dir: Path | str | None) -> Path:
    """把 None / str / Path 统一为 Path，便于测试注入 tmp_path。"""
    if tele_dir is None:
        return TELEMETRY_DIR
    return Path(tele_dir)


def event_file_path(
    when: datetime | None = None,
    tele_dir: Path | str | None = None,
) -> Path:
    """返回 ``events-YYYYMM.jsonl`` 的路径（不创建文件）。

    参数：
        when: 时间戳；默认 ``datetime.now(timezone.utc)``。
        tele_dir: 遥测目录；默认 ``TELEMETRY_DIR``（项目根 .agent/telemetry/）。
    """
    tele = _resolve_tele_dir(tele_dir)
    when = when or datetime.now(timezone.utc)
    return tele / f"events-{when.strftime('%Y%m')}.jsonl"


def record(
    event: dict[str, Any],
    *,
    tele_dir: Path | str | None = None,
    when: datetime | None = None,
) -> Path:
    """以 JSONL 追加一行事件到月度文件 ``events-YYYYMM.jsonl``。

    为什么用追加而非 read-modify-write：JSONL 是 append-only 事件流，
    追加是原子操作（POSIX 下小写入），多进程并发安全度足够；read-modify-write
    需要锁，且违反三本账只增不改原则。

    参数：
        event: 任务事件字典（应符合 task_event.schema.json）。
        tele_dir: 遥测目录（测试注入用）。
        when: 时间戳（测试注入用）。

    返回：写入的文件路径。
    """
    path = event_file_path(when=when, tele_dir=tele_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys=True 让相同事件产生稳定字节序列，便于 golden 对比
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def read_events(tele_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """读取所有 ``events-*.jsonl`` 文件，按文件名升序合并事件。

    为什么按文件名而非按时间排序：文件名 ``events-YYYYMM`` 已天然按时间有序，
    且无网络/系统调用，纯字符串比较稳定。
    """
    tele = _resolve_tele_dir(tele_dir)
    if not tele.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(tele.glob("events-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    return rows


def _first_pass(r: dict[str, Any]) -> bool:
    """一次通过判定：merged=true 且 attempts=1。

    为什么要求 merged=true：未合入的任务不算"完成"，attempts=1 仅代表本次尝试
    没有重试；与"一次通过率"语义一致。
    """
    return bool(r.get("merged")) and int(r.get("attempts", 1)) == 1


def aggregate(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """聚合吞吐 / 一次通过率 / 单位成本 / 模型×任务类型通过率。

    返回字典字段：
        total: 事件总数
        merged: 已合入数
        total_cost_cny: 累计成本（人民币元）
        unit_cost_cny: 单位合入成本 = total_cost_cny / max(merged, 1)
        first_pass_rate: 一次通过率 = first_pass_count / max(merged, 1)
        by_model_task_type: {(model, task_type): {n, pass, pass_rate, demote}}
            demote=True 当 n>=20 且 pass_rate<0.6（与 dashboard 阈值一致）
        misreport_count: misreport=true 的事件数

    为什么 demote 阈值取 n>=20：样本量过小（<20）时通过率波动大，
    OPC 路由纪律要求 n>=20 才生效。
    """
    rows_list = list(rows)
    if not rows_list:
        return {
            "total": 0,
            "merged": 0,
            "total_cost_cny": 0.0,
            "unit_cost_cny": 0.0,
            "first_pass_rate": 0.0,
            "by_model_task_type": {},
            "misreport_count": 0,
        }
    merged_rows = [r for r in rows_list if r.get("merged")]
    merged_count = len(merged_rows)
    total_cost = sum(float(r.get("cost_cny", 0)) for r in rows_list)
    first_pass_count = sum(1 for r in merged_rows if _first_pass(r))

    by_mt: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows_list:
        key = (str(r.get("model", "?")), str(r.get("task_type", "?")))
        by_mt.setdefault(key, []).append(r)

    by_mt_summary: dict[str, dict[str, Any]] = {}
    for (model, task_type), rs in by_mt.items():
        ok = sum(1 for r in rs if r.get("verifier_verdict") == "PASS")
        n = len(rs)
        pass_rate = ok / n if n else 0.0
        by_mt_summary[f"{model}|{task_type}"] = {
            "n": n,
            "pass": ok,
            "pass_rate": pass_rate,
            "demote": n >= 20 and pass_rate < 0.6,
        }

    misreport_count = sum(1 for r in rows_list if r.get("misreport"))

    return {
        "total": len(rows_list),
        "merged": merged_count,
        "total_cost_cny": total_cost,
        "unit_cost_cny": total_cost / merged_count if merged_count else 0.0,
        "first_pass_rate": (
            first_pass_count / merged_count if merged_count else 0.0
        ),
        "by_model_task_type": by_mt_summary,
        "misreport_count": misreport_count,
    }


def render_markdown(
    rows: Iterable[dict[str, Any]] | None = None,
    *,
    when: datetime | None = None,
) -> str:
    """渲染 markdown 产能报告（吞吐 / 一次通过率 / 单位成本）。

    与 ``tools/opc dashboard`` 输出兼容，但本函数返回完整 markdown 字符串，
    便于 ``tests/unit/test_telemetry.py`` 断言；dashboard 走 print 输出。

    参数：
        rows: 事件列表；默认从默认遥测目录读取。
        when: 报告日期；默认当前 UTC。
    """
    rows_list = list(rows) if rows is not None else read_events()
    when = when or datetime.now(timezone.utc)
    if not rows_list:
        return f"# 产能报告 {when.date().isoformat()}\n\n暂无遥测数据\n"

    agg = aggregate(rows_list)
    lines = [
        f"# 产能报告 {when.date().isoformat()}",
        "",
        "## 总览",
        f"- 任务总数: {agg['total']}",
        f"- 已合入: {agg['merged']}",
        f"- 累计成本: ¥{agg['total_cost_cny']:.2f}",
        f"- 单位合入成本: ¥{agg['unit_cost_cny']:.2f}",
        f"- 一次通过率: {agg['first_pass_rate']:.1%}",
        f"- 虚报事件: {agg['misreport_count']}",
        "",
        "## 模型×任务类型通过率",
        "| 模型 | 任务类型 | n | 通过 | 通过率 | 降权 |",
        "|---|---|---|---|---|---|",
    ]
    for key, v in sorted(agg["by_model_task_type"].items()):
        model, task_type = key.split("|", 1)
        demote = "⛔" if v["demote"] else ""
        lines.append(
            f"| {model} | {task_type} | {v['n']} | {v['pass']} | "
            f"{v['pass_rate']:.0%} | {demote} |"
        )
    return "\n".join(lines) + "\n"
