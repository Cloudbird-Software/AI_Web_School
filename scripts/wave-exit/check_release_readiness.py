#!/usr/bin/env python
"""T-W4-051「小程序发布前就绪清单」核验脚本.

逐项检查 OPC §6.6 的 7 项条件（API 冻结/模拟器/内容就绪/黄金路径/性能/合规/运维），
输出结构化报告与缺失项列表。机器可验部分由本脚本承载，人类最终确认。

OPC §6.6 七项：
1. API v1 冻结（OpenAPI），契约测试常绿；
2. 学生模拟器跑通全部 C 端链路（OpenAPI 参考客户端 + consumer-driven 契约测试）；
3. 内容就绪：三科首批（3–4 年级）图谱/语料/母题/素材入库量达标；
4. 黄金路径 30 题型 + 黄金数据集 + nightly 重放全绿连续 2 周；
5. 性能达标（在线组卷 p95<2s、批改 10s 级）有压测报告；
6. 合规项机器可验部分全过（无排名查询路径/PII 不出库/时长保护触发）；
7. 运维就绪：备份恢复演练、监控告警、成本仪表盘。

用法:
    python scripts/wave-exit/check_release_readiness.py
    python scripts/wave-exit/check_release_readiness.py --json   # JSON 输出

退出码：0=全部机器可验项通过；1=有缺失项（供人类决策）。

设计要点：
- 不实际执行测试套件（执行由 w4.sh 承载），只检查"就绪证据"存在性；
- 内容入库量（§6.6.3）与 nightly 连续 2 周（§6.6.4）需 DB/CI 历史，本脚本做
  占位检查并标注「需人类确认」；
- 不 import 任何学科包/学段包（宪法 A5/X6）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class CheckItem:
    """单项检查结果."""
    item_id: str
    title: str
    status: str = "PENDING"  # PASS / PARTIAL / FAIL / PENDING
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)


def _exists(rel: str) -> bool:
    return (_PROJECT_ROOT / rel).exists()


def _count_pytest_items(test_path: str) -> int:
    """数某个测试目录/文件的 test 函数数（粗略，按 def test_ 计数）."""
    p = _PROJECT_ROOT / test_path
    if not p.exists():
        return 0
    count = 0
    files = [p] if p.is_file() else list(p.rglob("test_*.py"))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        count += len(re.findall(r"^\s*(?:async\s+)?def\s+test_", text, re.MULTILINE))
    return count


# ────────────────────────────────────────────────────────────────────
# 七项检查
# ────────────────────────────────────────────────────────────────────

def check_api_frozen() -> CheckItem:
    """§6.6.1 API v1 冻结：openapi-v1.yaml 存在 + FROZEN.txt 登记 + 契约测试."""
    item = CheckItem("1", "API v1 冻结（OpenAPI + 契约测试常绿）")
    candidates = [
        "specs/contracts/api/openapi-v1.yaml",
        "specs/contracts/openapi-v1.yaml",
    ]
    found_yaml = next((c for c in candidates if _exists(c)), None)
    if found_yaml:
        item.evidence.append(f"OpenAPI 规范存在：{found_yaml}")
    else:
        item.missing.append("openapi-v1.yaml 未找到（尝试路径：" + " / ".join(candidates) + "）")

    frozen_txt = _PROJECT_ROOT / "specs/contracts/FROZEN.txt"
    if frozen_txt.exists():
        content = frozen_txt.read_text(encoding="utf-8")
        if "openapi" in content.lower() or "api" in content.lower():
            item.evidence.append("FROZEN.txt 含 API 契约登记")
        else:
            item.missing.append("FROZEN.txt 未登记 API 契约")
    else:
        item.missing.append("specs/contracts/FROZEN.txt 不存在")

    if _exists("tests/contract/test_api_frozen.py"):
        item.evidence.append("契约冻结测试存在：tests/contract/test_api_frozen.py")
    else:
        item.missing.append("tests/contract/test_api_frozen.py 不存在")

    if _exists("scripts/ci/check_openapi_diff.py"):
        item.evidence.append("契约 diff 脚本存在：scripts/ci/check_openapi_diff.py")
    else:
        item.missing.append("scripts/ci/check_openapi_diff.py 不存在")

    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_simulator() -> CheckItem:
    """§6.6.2 学生模拟器全链路：练习/诊断/复习 e2e 场景存在."""
    item = CheckItem("2", "学生模拟器全链路（OpenAPI 参考客户端）")
    required = [
        "tests/simulator/test_practice_e2e.py",
        "tests/simulator/test_diagnosis_review_e2e.py",
        "tests/unit/test_simulator_client.py",
    ]
    for r in required:
        if _exists(r):
            item.evidence.append(f"场景测试存在：{r}")
        else:
            item.missing.append(r)
    # 微信端特有能力清单（OPC §6.6.2 标注为「已知未验证项」，需人类确认移交）
    item.needs_human.append("微信端特有能力（扫码/小程序音频/包体渲染/微信登录）需小程序设计阶段确认")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_content_ready() -> CheckItem:
    """§6.6.3 内容就绪：三科首批语料/母题/素材目录存在（入库量需人类/DB 确认）."""
    item = CheckItem("3", "内容就绪（三科首批 3–4 年级图谱/语料/母题/素材）")
    # 三科学科包目录
    packs = [
        ("数学", "src/packs/subject-math"),
        ("语文", "src/packs/subject-chinese"),
        ("英语", "src/packs/subject-english"),
    ]
    for name, path in packs:
        if _exists(path):
            item.evidence.append(f"{name}学科包存在：{path}")
        else:
            item.missing.append(f"{name}学科包缺失：{path}")
    # 语料/模板目录占位检查
    corpus_dirs = [
        "src/packs/subject-english/corpora",
        "src/packs/subject-math/corpora",
        "src/packs/subject-chinese/corpora",
    ]
    for d in corpus_dirs:
        p = _PROJECT_ROOT / d
        if p.is_dir() and any(p.iterdir()):
            item.evidence.append(f"语料目录非空：{d}")
        else:
            item.missing.append(f"语料目录空或不存在：{d}")
    # 入库量需 DB 查询 + 人类确认（§7.3 数量目标）
    item.needs_human.append("三科首批入库量达标（§7.3：图谱/错误类型/语料/母题数量）需 DB 统计 + 教研确认")
    item.needs_human.append("周更卷三科可产需运营确认")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_golden_path() -> CheckItem:
    """§6.6.4 黄金路径 30 题型 + 黄金数据集 + nightly 连续 2 周."""
    item = CheckItem("4", "黄金路径 30 题型 + 黄金数据集 + nightly 重放")
    if _exists("tests/golden-path"):
        n = _count_pytest_items("tests/golden-path")
        item.evidence.append(f"黄金路径测试目录存在，test 函数数 ≈ {n}")
        if n < 30:
            item.missing.append(f"黄金路径 test 函数数 {n} < 30（题型覆盖不足）")
    else:
        item.missing.append("tests/golden-path 不存在")
    if _exists("tests/golden"):
        item.evidence.append("黄金数据集目录存在：tests/golden")
    else:
        item.missing.append("tests/golden 不存在")
    # nightly 重放连续 2 周：需 CI 历史，机器可验占位
    item.needs_human.append("nightly 重放全绿连续 2 周需 CI 历史记录确认")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_performance() -> CheckItem:
    """§6.6.5 性能达标：压测报告存在且含 p95/批改指标."""
    item = CheckItem("5", "性能达标（组卷 p95<2s、批改 10s 级）")
    reports = [
        "tests/performance/report_assembly.md",
        "tests/performance/report_grading.md",
    ]
    for r in reports:
        p = _PROJECT_ROOT / r
        if p.exists():
            text = p.read_text(encoding="utf-8")
            item.evidence.append(f"压测报告存在：{r}")
            # 检查是否含 p95 / 延迟指标
            if "p95" in text.lower() or "p95" in text:
                item.evidence.append(f"{r} 含 p95 指标")
            else:
                item.missing.append(f"{r} 缺 p95 指标")
        else:
            item.missing.append(f"压测报告不存在：{r}")
    # 压测脚本存在
    perf_tests = [
        "tests/performance/test_assembly_latency.py",
        "tests/performance/test_grading_latency.py",
    ]
    for t in perf_tests:
        if _exists(t):
            item.evidence.append(f"压测脚本存在：{t}")
        else:
            item.missing.append(t)
    item.needs_human.append("p95<2s / 批改 10s 级达标需最新压测运行结果确认")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_compliance() -> CheckItem:
    """§6.6.6 合规项：无排名查询/PII 保险库/时长保护测试存在."""
    item = CheckItem("6", "合规项机器可验（无排名/PII 不出库/时长保护）")
    required = [
        ("无排名查询静态实证", "tests/contract/test_no_ranking_query.py"),
        ("无排名查询扫描脚本", "scripts/ci/check_no_ranking.py"),
        ("PII 保险库测试", "tests/unit/test_pii_vault.py"),
        ("家长授权测试", "tests/unit/test_parental_consent.py"),
        ("时长保护测试", "tests/unit/test_duration_guard.py"),
        ("姓名 redaction 测试", "tests/unit/test_redaction.py"),
    ]
    for name, path in required:
        if _exists(path):
            item.evidence.append(f"{name}：{path}")
        else:
            item.missing.append(f"{name}缺失：{path}")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def check_ops() -> CheckItem:
    """§6.6.7 运维就绪：备份/监控/成本仪表盘."""
    item = CheckItem("7", "运维就绪（备份恢复/监控告警/成本仪表盘）")
    required = [
        ("备份脚本", "scripts/ops/backup.sh"),
        ("备份校验", "scripts/ops/backup_verify.py"),
        ("成本仪表盘测试", "tests/unit/test_cost_dashboard.py"),
    ]
    for name, path in required:
        if _exists(path):
            item.evidence.append(f"{name}：{path}")
        else:
            item.missing.append(f"{name}缺失：{path}")
    # 监控端点（健康检查）——检查 API 路由是否存在
    health_candidates = [
        "src/api/routers/health.py",
        "src/api/routers/monitoring.py",
        "src/api/routers/ops.py",
    ]
    found_health = next((c for c in health_candidates if _exists(c)), None)
    if found_health:
        item.evidence.append(f"监控/健康端点路由存在：{found_health}")
    else:
        # 容错：检查 main.py 是否注册健康路由
        main_py = _PROJECT_ROOT / "src/api/main.py"
        if main_py.exists() and "health" in main_py.read_text(encoding="utf-8").lower():
            item.evidence.append("main.py 含 health 路由注册")
        else:
            item.missing.append("监控/健康端点路由未找到")
    item.needs_human.append("备份恢复演练记录需人工执行并归档")
    item.needs_human.append("监控告警规则需运维确认接入")
    item.status = "PASS" if not item.missing else "PARTIAL"
    return item


def run_all_checks() -> list[CheckItem]:
    """运行全部 7 项检查."""
    return [
        check_api_frozen(),
        check_simulator(),
        check_content_ready(),
        check_golden_path(),
        check_performance(),
        check_compliance(),
        check_ops(),
    ]


def format_report(items: list[CheckItem]) -> str:
    """格式化为人类可读报告."""
    lines = ["== 小程序发布前就绪清单核验（OPC §6.6）==", ""]
    pass_n = sum(1 for it in items if it.status == "PASS")
    partial_n = sum(1 for it in items if it.status == "PARTIAL")
    fail_n = sum(1 for it in items if it.status == "FAIL")
    for it in items:
        icon = {"PASS": "✅", "PARTIAL": "⚠️", "FAIL": "❌"}[it.status]
        lines.append(f"{icon} {it.item_id}. {it.title} [{it.status}]")
        for e in it.evidence:
            lines.append(f"   证据：{e}")
        for m in it.missing:
            lines.append(f"   缺失：{m}")
        for h in it.needs_human:
            lines.append(f"   待人工确认：{h}")
    lines.append("")
    lines.append(f"摘要：PASS {pass_n} / PARTIAL {partial_n} / FAIL {fail_n}")
    all_missing = [m for it in items for m in it.missing]
    if all_missing:
        lines.append("")
        lines.append("缺失项（供人类决策）：")
        for m in all_missing:
            lines.append(f"  - {m}")
    all_human = [h for it in items for h in it.needs_human]
    if all_human:
        lines.append("")
        lines.append("待人工确认项：")
        for h in all_human:
            lines.append(f"  - {h}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="小程序发布前就绪清单核验")
    parser.add_argument("--json", action="store_true", help="JSON 输出（供程序消费）")
    args = parser.parse_args()

    items = run_all_checks()
    if args.json:
        print(json.dumps([asdict(it) for it in items], ensure_ascii=False, indent=2))
    else:
        print(format_report(items))

    # 退出码：有 missing 即非零（供人类决策）
    has_missing = any(it.missing for it in items)
    return 1 if has_missing else 0


if __name__ == "__main__":
    sys.exit(main())
