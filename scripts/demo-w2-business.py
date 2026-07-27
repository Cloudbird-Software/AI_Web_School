#!/usr/bin/env python3
"""W2 业务端到端演示（E2E-1 现场版）：母题 DSL → 实例化 → 校验门 → 签发入库 → 组卷 → PDF → 追溯 → 作答阻断.

真实走通的业务链路（全部用 W1/W2 现有实现，无 mock）：
  a. 用 T-W2-001 母题 DSL 定义 3 个真实三年级数学母题
     （两位数乘法 / 小数比较大小 / 单位换算），DSL Linter 校验；
  b. 用 T-W2-004 确定性实例化引擎产出实例（每题 3-4 个，验证同输入同 id）；
  c. 过校验门（T-W2-010 编排器：通用验证器链 schema/duplicate_placeholder
     + 数学双实现验算 dual_check，SymPy 独立重算）；
  d. 签发入库（门证书 + T-W2-043 issue_item_version，status→published，
     publication 签发账落库）；
  e. 组卷（T-W2-038 周更批处理：10 题「三年级数学周练卷」，确定性选题）；
  f. 渲染真实 PDF（T-W2-035：试卷 + 解析册两份，含卷码 QR 与每题短码，
     输出到 out/ 目录），paper/paper_item 追溯行落库；
  g. 追溯演示：取卷上一题短码，打印完整追溯链
     （题短码→paper_item→item_version→lineage 谱系→门证书→签发人）；
  h. 作答与阻断演示：record_event 写 3 条作答事件，随后对该事件 UPDATE
     必须被 append-only 触发器（迁移 0003）在 DB 层拒绝。

用法（需 db 容器运行、.env 含 POSTGRES_*；PDF 后端 Edge 或 playwright）：
    python scripts/demo-w2-business.py
退出码 0 = 全链路通过（结尾打印 W2 BUSINESS E2E PASS）；非 0 = 失败。

幂等性：item_version 用内容寻址（公式一），重复执行同一母题+参数得到同一 id；
已 published 的版本跳过重复签发（状态机无重签），门/证书/卷/事件均为只增账，
重复执行只会追加新行，不破坏 D1。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# ── 让脚本能 import 项目 src（与 tests/conftest.py 同处理）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 UTF-8 输出兜底（题面含中文/○/× 符号）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ulid  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from src.core.content.publication import issue_item_version  # noqa: E402
from src.core.content.writer import publish_item_version  # noqa: E402
from src.core.events.writer import record_event  # noqa: E402
from src.core.gate.orchestrator import run_gate  # noqa: E402
from src.core.gate.policy.loader import ChainEntry, GatePolicy, ValidatorStep  # noqa: E402
from src.core.gate.validator import GateContext  # noqa: E402
import src.core.gate.validators.generic  # noqa: E402,F401 —— import 即注册 platform 通用验证器
from src.core.instantiation.dsl.linter import lint  # noqa: E402
from src.core.instantiation.engine import ENGINE_DIGEST, instantiate  # noqa: E402
from src.core.instantiation.expr import evaluate  # noqa: E402
from src.core.models.item_template import ItemTemplate  # noqa: E402
from src.core.models.item_template_version import ItemTemplateVersion  # noqa: E402
from src.core.models.item_version import ItemVersion  # noqa: E402
from src.core.models.paper import Paper  # noqa: E402
from src.core.models.paper_item import PaperItem  # noqa: E402
from src.core.render.pdf_exporter import _find_edge  # noqa: E402
from src.core.render.trace_codes import (  # noqa: E402
    build_trace_chain,
    verify_item_short_code,
)
from src.core.render.weekly_batch import (  # noqa: E402
    WeeklyConstraints,
    WeeklyScope,
    run as weekly_batch_run,
)

PACK_ID = "subject-math"
# pack_digest：与 tests/golden 数学包约定一致（sha256("subject-math")）
PACK_DIGEST = "sha256:" + hashlib.sha256(PACK_ID.encode("utf-8")).hexdigest()
ISSUER = "demo-w2-operator"
OUT_DIR = PROJECT_ROOT / "out"


# ────────────────────────────────────────────────────────────────────
# 环境：.env 加载 + 异步引擎
# ────────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """从项目根 .env 加载配置（覆盖系统环境变量，与 tests/conftest.py 一致）."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _build_async_dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "muti_dev")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def _load_math_validators() -> None:
    """以 importlib 加载数学包验证器（subject-math 目录含连字符，无法普通 import）.

    模块加载时自注册（register_validator('subject-math', DualCheckValidator)）。
    """
    path = (
        PROJECT_ROOT / "src" / "packs" / "subject-math" / "validators" / "dual_check.py"
    )
    spec = importlib.util.spec_from_file_location("subject_math_dual_check", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


# ────────────────────────────────────────────────────────────────────
# a. 母题 DSL（T-W2-001）：3 个三年级数学母题
# ────────────────────────────────────────────────────────────────────

def _objective(kp_code: str) -> dict[str, Any]:
    return {
        "kp_set": [{"dimension": "kp", "code": kp_code}],
        "kp_set_mode": "single",
        "cognitive_level": "apply",
        "gradeband": "L",
        "graph_release": "2026.1",
    }


def _template_version_id(template_id: str, spec: dict[str, Any]) -> str:
    """template_version_id = sha256(canonical(template_id, dsl_version, spec))（与黄金生成器同式）."""
    payload = json.dumps(
        {"template_id": template_id, "dsl_version": "1", "spec": spec},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_templates() -> list[dict[str, Any]]:
    """定义 3 个母题：返回 [{template_version, instances: [{params, answer}]}]."""
    templates: list[dict[str, Any]] = []

    # ① 两位数乘法（numeric_blank）
    spec_mul = {
        "objective": _objective("math.nal.int.mul"),
        "slots": {
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
        },
        "variation_axes": {"axes": []},
        "presentation": {
            "blocks": [{"kind": "text", "template": "{a} × {b} = （  ）"}]
        },
        "answer_program": {"expression": "a * b", "returns": "number"},
        "distractor_rules": {"rules": []},
    }
    templates.append({
        "name": "两位数乘法",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "template_id": "tpl-demo-mul-2digit",
        "spec": spec_mul,
        "instances": [
            {"params": {"a": 23, "b": 14}, "answer": "322"},
            {"params": {"a": 36, "b": 12}, "answer": "432"},
            {"params": {"a": 45, "b": 21}, "answer": "945"},
            {"params": {"a": 28, "b": 34}, "answer": "952"},
        ],
    })

    # ② 小数比较大小（text_blank：填 >、< 或 =）
    spec_cmp = {
        "objective": _objective("math.nal.decimal.compare"),
        "slots": {
            "a": {"type": "decimal", "difficulty_relevant": True},
            "b": {"type": "decimal", "difficulty_relevant": True},
        },
        "variation_axes": {"axes": []},
        "presentation": {
            "blocks": [
                {"kind": "text", "template": "在 ○ 里填 >、< 或 =：{a} ○ {b}"}
            ]
        },
        "answer_program": {"expression": "a - b", "returns": "number"},
        "distractor_rules": {"rules": []},
    }
    templates.append({
        "name": "小数比较大小",
        "interaction_id": "text_blank",
        "scorer_id": "exact_match",
        "template_id": "tpl-demo-decimal-compare",
        "spec": spec_cmp,
        "instances": [
            {"params": {"a": "0.7", "b": "0.45"}, "answer": ">"},
            {"params": {"a": "3.25", "b": "3.5"}, "answer": "<"},
            # 6.80 与 6.8 规范化后同值 → 等号（小数性质）
            {"params": {"a": "6.8", "b": "6.80"}, "answer": "="},
        ],
    })

    # ③ 单位换算（千克→克，numeric_blank）
    spec_unit = {
        "objective": _objective("math.meas.unit.convert"),
        "slots": {
            "kg": {"type": "int", "difficulty_relevant": True},
        },
        "variation_axes": {"axes": []},
        "presentation": {
            "blocks": [{"kind": "text", "template": "{kg} 千克 = （  ） 克"}]
        },
        "answer_program": {"expression": "kg * 1000", "returns": "number"},
        "distractor_rules": {"rules": []},
    }
    templates.append({
        "name": "单位换算（千克→克）",
        "interaction_id": "numeric_blank",
        "scorer_id": "exact_match",
        "template_id": "tpl-demo-unit-kg-g",
        "spec": spec_unit,
        "instances": [
            {"params": {"kg": 2}, "answer": "2000"},
            {"params": {"kg": 5}, "answer": "5000"},
            {"params": {"kg": 8}, "answer": "8000"},
        ],
    })

    # 组装 template_version（含内容寻址 template_version_id）
    for t in templates:
        t["template_version"] = {
            "template_version_id": _template_version_id(t["template_id"], t["spec"]),
            "template_id": t["template_id"],
            "dsl_version": "1",
            "spec": t["spec"],
        }
    return templates


# ────────────────────────────────────────────────────────────────────
# 门策略：通用链（schema + duplicate_placeholder）+ 数学双实现验算
# ────────────────────────────────────────────────────────────────────
# 为什么不用 policy.default.yaml 的 subject-math 链：默认链含 license 验证器
# （面向 material/corpus 的许可校验；item 无 license_id 必 fail），且默认策略
# 的学科验证器位留空（注释明示「由数学包注册后追加」）。本演示按架构 v2 §4.3
# 组合 item 链：schema（结构，阻断）→ dual_check（数学验算，阻断）
# → duplicate_placeholder（查重提示，非阻断）。

def build_demo_policy() -> GatePolicy:
    return GatePolicy(
        policy_version="gate-policy-demo-w2",
        status="frozen-candidate",
        description="W2 业务演示链：schema + dual_check + duplicate_placeholder",
        chains=[
            ChainEntry(
                pack_id=PACK_ID,
                artifact_type="item",
                validators=[
                    ValidatorStep(validator_id="schema", blocking=True),
                    ValidatorStep(validator_id="dual_check", blocking=True),
                    ValidatorStep(validator_id="duplicate_placeholder", blocking=False),
                ],
            )
        ],
    )


def _engine_answer(spec: dict[str, Any], params: dict[str, Any]) -> Any:
    """按槽类型构造求值 env，用引擎求值器算正解（供 dual_check 独立比对）.

    返回值转为 JSON 兼容类型（Decimal→str）：payload 要进查重验证器的
    canonical JSON，Decimal 不可 JSON 序列化；dual_check 的
    _engine_answer_to_sympy 对 str 走 Rational 精确路径，语义不变。
    """
    env: dict[str, Any] = {}
    for name, value in params.items():
        slot_type = spec["slots"][name]["type"]
        env[name] = Decimal(str(value)) if slot_type == "decimal" else value
    answer = evaluate(spec["answer_program"]["expression"], env=env)
    if isinstance(answer, Decimal):
        return str(answer)
    return answer


# ────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────

async def main() -> int:
    _load_dotenv()
    engine = create_async_engine(_build_async_dsn(), echo=False, pool_pre_ping=True)
    _load_math_validators()
    policy = build_demo_policy()

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # 门失败路径的 FK 占位（编排器要求；幂等）
            await session.execute(
                text(
                    "INSERT INTO gate_certificate"
                    " (cert_id, artifact_ref, cert_type, policy_version, issued_by)"
                    " VALUES ('cert:none', 'placeholder-for-failed-run', 'publish',"
                    " 'no-policy', 'system')"
                    " ON CONFLICT (cert_id) DO NOTHING"
                )
            )
            await session.commit()

            # ── a. DSL 定义 + Linter ──────────────────────────────
            print("=" * 70)
            print("步骤 a：母题 DSL 定义（T-W2-001）+ Linter 校验")
            print("=" * 70)
            templates = build_templates()
            for t in templates:
                result = lint(t["spec"])
                if not result.valid:
                    raise RuntimeError(
                        f"DSL Linter 未通过（{t['name']}）："
                        f"{[e.message for e in result.errors]}"
                    )
                print(
                    f"  ✅ {t['name']}：template_id={t['template_id']} "
                    f"tvid={t['template_version']['template_version_id'][:24]}… "
                    f"交互={t['interaction_id']} Linter PASS"
                )

            # 母题草稿落库（item_template + item_template_version，status=draft）：
            # item.template_version_id FK 要求母题版本必须先存在（契约 §2.3）。
            # 幂等：同 template_id / template_version_id 已存在则复用（D1 只增不改）。
            for t in templates:
                tv = t["template_version"]
                if await session.get(ItemTemplate, tv["template_id"]) is None:
                    session.add(
                        ItemTemplate(
                            template_id=tv["template_id"],
                            pack_id=PACK_ID,
                            current_version_id=None,
                        )
                    )
                    await session.flush()
                if await session.get(
                    ItemTemplateVersion, tv["template_version_id"]
                ) is None:
                    session.add(
                        ItemTemplateVersion(
                            template_version_id=tv["template_version_id"],
                            template_id=tv["template_id"],
                            dsl_version=tv["dsl_version"],
                            spec=t["spec"],
                            status="draft",
                        )
                    )
            await session.commit()
            print("  ✅ 3 个母题草稿已落库 item_template_version（status=draft）")

            # ── b. 实例化（T-W2-004）+ 确定性验证 ──────────────────
            print()
            print("=" * 70)
            print("步骤 b：确定性实例化（T-W2-004，每题 3-4 个实例）")
            print("=" * 70)
            instantiated: list[dict[str, Any]] = []  # {tpl, params, answer, result}
            for t in templates:
                print(f"  母题「{t['name']}」（{t['interaction_id']}）：")
                for inst in t["instances"]:
                    params = inst["params"]
                    scorer_params = {"answer": inst["answer"]}
                    result = instantiate(
                        t["template_version"],
                        params,
                        pack_digest=PACK_DIGEST,
                        interaction_id=t["interaction_id"],
                        scorer_id=t["scorer_id"],
                        scorer_params=scorer_params,
                        locale="zh-CN",
                        corpus_digests=[],
                        seed=0,
                    )
                    # 确定性：同输入二次实例化，id 与 content 必须逐字节一致（D3）
                    again = instantiate(
                        t["template_version"],
                        params,
                        pack_digest=PACK_DIGEST,
                        interaction_id=t["interaction_id"],
                        scorer_id=t["scorer_id"],
                        scorer_params=scorer_params,
                        locale="zh-CN",
                        corpus_digests=[],
                        seed=0,
                    )
                    assert result.item_version_id == again.item_version_id, (
                        f"确定性破坏：同参数两次实例化 id 不同（{t['name']} {params}）"
                    )
                    assert result.content == again.content
                    stem = result.content["blocks"][0]["rendered"]
                    print(
                        f"    - {stem}  答案={inst['answer']}  "
                        f"iv={result.item_version_id[:28]}…（二次实例化 id 一致 ✓）"
                    )
                    instantiated.append({
                        "tpl": t, "params": params, "result": result,
                    })
            total = len(instantiated)
            print(f"  ✅ 共 {total} 个实例，全部通过确定性校验")

            # ── c+d. 校验门 + 签发入库 ────────────────────────────
            print()
            print("=" * 70)
            print("步骤 c+d：校验门（T-W2-010）→ 签发入库 published（T-W2-043）")
            print("=" * 70)
            published_pool: list[dict[str, Any]] = []
            for rec in instantiated:
                t, params, result = rec["tpl"], rec["params"], rec["result"]
                iv_id = result.item_version_id

                # 幂等：已存在的版本跳过重复 INSERT（内容寻址同 id）
                existing = await session.get(ItemVersion, iv_id)
                if existing is None:
                    vd = result.model_dump()
                    vd.update({
                        "pack_id": PACK_ID,
                        "tier": "A",
                        "template_version_id": t["template_version"]["template_version_id"],
                        "template_version_digest": t["template_version"]["template_version_id"],
                        "normalized_params": result.lineage["params"]["normalized"],
                        "pack_digest": PACK_DIGEST,
                        "engine_digest": ENGINE_DIGEST,
                        "corpus_digests": [],
                        "locale": "zh-CN",
                    })
                    ids = await publish_item_version(
                        item_id=None, version_data=vd,
                        gate_certificate_id=None, db=session,
                    )
                    assert ids["item_version_id"] == iv_id, (
                        "内容寻址不一致：writer 计算的 item_version_id ≠ 引擎值"
                    )
                    version = await session.get(ItemVersion, iv_id)
                else:
                    version = existing

                # 门编排：schema → dual_check → duplicate_placeholder
                payload = {
                    "objective": result.objective,
                    "interaction_ref": result.interaction_ref,
                    "content": result.content,
                    "scoring_ref": result.scoring_ref,
                    "error_bindings": result.error_bindings,
                    "lineage": result.lineage,
                    # dual_check 验算载荷：母题 spec + 参数 + 引擎正解
                    "spec": t["spec"],
                    "params": params,
                    "engine_answer": _engine_answer(t["spec"], params),
                }
                ctx = GateContext(
                    artifact_type="item",
                    pack_id=PACK_ID,
                    artifact_payload=payload,
                    db=session,
                    required_keys=[
                        "objective", "interaction_ref", "content",
                        "scoring_ref", "error_bindings", "lineage",
                    ],
                )
                outcome = await run_gate(
                    artifact_ref=iv_id,
                    artifact_type="item",
                    pack_id=PACK_ID,
                    ctx=ctx,
                    policy=policy,
                    db=session,
                    issued_by=ISSUER,
                    cert_type="publish",
                )
                verdicts = ", ".join(
                    f"{r.validator_id}={r.verdict}" for r in outcome.runs
                )
                if outcome.final_verdict != "pass" or outcome.cert_id is None:
                    raise RuntimeError(
                        f"门未通过（{t['name']} {params}）："
                        f"final={outcome.final_verdict} 链={verdicts}"
                    )

                # 签发：draft/quarantined → published（已 published 幂等跳过）
                version = await session.get(ItemVersion, iv_id)
                if version.status != "published":
                    issue = await issue_item_version(
                        item_version_id=iv_id,
                        gate_certificate_id=outcome.cert_id,
                        published_by=ISSUER,
                        db=session,
                    )
                    action = f"签发 publication_id={issue['publication_id'][:20]}…"
                else:
                    action = "已 published（幂等跳过重签）"
                stem = result.content["blocks"][0]["rendered"]
                print(f"  ✅ {stem}")
                print(f"     门链：{verdicts} → PASS cert={outcome.cert_id[:24]}…")
                print(f"     {action} status=published")
                # 组卷池条目：item_version 本体以引擎规范形（kind/template/rendered）
                # 入库；渲染视图按 Render IR 契约适配为 type/value/fill 块
                # （weekly_batch 的 pool 契约，见其单测 _make_item_version）。
                pool_iv = result.model_dump()
                fill_kind = (
                    "text" if t["interaction_id"] == "text_blank" else "numeric"
                )
                pool_iv["content"] = {
                    "blocks": [
                        {"type": "text", "value": stem},
                        {"type": "fill", "blank_id": "b1", "kind": fill_kind},
                    ]
                }
                published_pool.append(pool_iv)

            # ── e+f. 组卷 + 渲染 PDF（T-W2-038 / T-W2-035）─────────
            print()
            print("=" * 70)
            print("步骤 e+f：周更批处理组卷（T-W2-038）→ 渲染 PDF（T-W2-035）")
            print("=" * 70)
            scope = WeeklyScope(
                subject_pack_id=PACK_ID,
                gradeband="L",
                kp_codes=(
                    "math.nal.int.mul",
                    "math.nal.decimal.compare",
                    "math.meas.unit.convert",
                ),
                kp_snapshot_ref="kp-snap-demo-2026W30",
            )
            constraints = WeeklyConstraints(
                num_items=10,
                interaction_distribution={"numeric_blank": 7, "text_blank": 3},
                seed=20260727,
                paper_title="三年级数学周练卷",
            )
            pdf_backend = "edge" if _find_edge() else "playwright"
            batch = weekly_batch_run(
                scope,
                constraints,
                OUT_DIR,
                item_version_pool=published_pool,
                pdf_backend=pdf_backend,  # type: ignore[arg-type]
                created_by=ISSUER,
            )
            print(f"  卷码 paper_code = {batch.paper_code}")
            print(f"  卷规格 paper_spec_id = {batch.paper_spec_id}（QR 只含此 id+校验位）")
            print(f"  选题 10 题（确定性 seed={constraints.seed}，后端={pdf_backend}）：")
            for row in batch.paper_item_rows:
                print(
                    f"    q{row['item_number']:<2} 短码={row['item_short_code']}  "
                    f"iv={row['item_version_id'][:28]}…"
                )
            # paper / paper_item 追溯行落库（交付域账本，只增不改）
            session.add(Paper(**batch.paper_row))
            for row in batch.paper_item_rows:
                session.add(PaperItem(**row))
            await session.commit()
            for label, path in (
                ("试卷", batch.paper_pdf_path),
                ("解析册", batch.solution_pdf_path),
            ):
                head = path.read_bytes()[:5]
                assert head == b"%PDF-", f"{label} PDF 头非法：{path}"
                size_kb = path.stat().st_size / 1024
                print(f"  ✅ {label} PDF：{path}（{size_kb:.1f} KB，%PDF 头校验通过）")

            # ── g. 追溯演示 ────────────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 g：短码追溯演示（题码→paper_item→item_version→谱系→门证书→签发人）")
            print("=" * 70)
            first = batch.paper_item_rows[0]
            short_code = first["item_short_code"]
            assert verify_item_short_code(short_code), f"短码校验失败：{short_code}"
            iv_row = await session.get(ItemVersion, first["item_version_id"])
            assert iv_row is not None
            cert_row = (
                await session.execute(
                    text(
                        "SELECT cert_id, issued_by, issued_at, policy_version"
                        " FROM gate_certificate WHERE cert_id = :cid"
                    ),
                    {"cid": iv_row.gate_certificate_id},
                )
            ).one()
            pub_row = (
                await session.execute(
                    text(
                        "SELECT published_by, published_at FROM publication"
                        " WHERE item_version_id = :vid LIMIT 1"
                    ),
                    {"vid": iv_row.item_version_id},
                )
            ).one_or_none()
            chain = build_trace_chain(
                paper_item_row=first,
                item_version_row={
                    "item_version_id": iv_row.item_version_id,
                    "item_id": iv_row.item_id,
                    "gate_certificate_id": iv_row.gate_certificate_id,
                    "status": iv_row.status,
                    "lineage": iv_row.lineage,
                },
                gate_certificate_row={
                    "cert_id": cert_row[0],
                    "issued_by": cert_row[1],
                    "issued_at": cert_row[2].isoformat() if cert_row[2] else None,
                    "policy_version": cert_row[3],
                },
            )
            lineage = chain["lineage"]
            print(f"  题短码      : {chain['item_short_code']}（Luhn 校验通过）")
            print(f"  paper_item  : {chain['paper_item_id']}（卷内 q{chain['item_number']}）")
            print(f"  paper       : {chain['paper_id']} 卷码={batch.paper_code}")
            print(f"  item_version: {chain['item_version_id'][:40]}… status={iv_row.status}")
            print(f"  item        : {chain['item_id'][:40]}…")
            print(
                f"  谱系 lineage: tier={lineage.get('tier')} "
                f"pipeline={lineage.get('pipeline', {}).get('id')}"
                f"@{lineage.get('pipeline', {}).get('version')} "
                f"tvid={str(lineage.get('template_version_id'))[:28]}…"
            )
            print(
                f"     params={json.dumps(lineage.get('params', {}).get('normalized'), ensure_ascii=False)} "
                f"signed_by={lineage.get('signed_by')}"
            )
            print(
                f"  门证书      : {chain['gate_certificate_id'][:32]}… "
                f"policy={chain['policy_version']}"
            )
            print(
                f"  签发人      : 门证书 issued_by={chain['issued_by']}；"
                f"publication published_by={pub_row[0] if pub_row else None}"
            )

            # ── h. 作答与阻断演示 ──────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 h：作答事件 record_event ×3 → UPDATE 必须被触发器拒绝（D1）")
            print("=" * 70)
            student_alias = uuid.uuid4()
            event_ids: list[uuid.UUID] = []
            for i, row in enumerate(batch.paper_item_rows[:3], start=1):
                eid = uuid.uuid4()
                await record_event(
                    session,
                    event_id=eid,
                    student_alias_id=student_alias,
                    item_version_id=row["item_version_id"],
                    scene="practice",
                    raw_payload={"answer": ["322", ">", "2000"][i - 1]},
                    scoring_trace={"scorer_id": "exact_match", "is_correct": True},
                    error_inferences=[],
                    created_at=datetime.now(timezone.utc),
                    duration_ms=15000 + i * 1000,
                    source_ref={
                        "paper_id": batch.paper_id,
                        "placement_token": row["placement_token"],
                    },
                )
                event_ids.append(eid)
                print(f"  ✅ 作答事件 {i}：event_id={eid}（scene=practice）")

            # 对刚写入的事件 UPDATE：append-only 触发器必须在 DB 层拒绝
            blocked = False
            try:
                await session.execute(
                    text(
                        "UPDATE response_event SET duration_ms = 0"
                        " WHERE event_id = :eid"
                    ),
                    {"eid": event_ids[0]},
                )
                await session.commit()
            except Exception as e:  # noqa: BLE001 —— 预期被触发器拒绝
                blocked = "append-only" in str(e) or "只增不改" in str(e)
                print(f"  ✅ UPDATE 被 DB 触发器物理拒绝：{type(e).__name__}")
                print(f"     错误信息摘录：{str(e)[:100]}…")
                await session.rollback()
            if not blocked:
                raise RuntimeError(
                    "append-only 阻断失败：UPDATE response_event 未被触发器拒绝"
                )

        print()
        print("=" * 70)
        print("W2 BUSINESS E2E PASS")
        print("=" * 70)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:  # noqa: BLE001 —— 演示脚本：任何失败即链路不通
        print(f"W2 BUSINESS E2E FAIL: {e}", file=sys.stderr)
        sys.exit(1)
