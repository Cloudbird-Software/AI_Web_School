#!/usr/bin/env python3
"""W3 业务端到端演示（学生侧闭环现场版）：练习→即时评分→弱项报告→针对性练习→复习队列→诊断→数据飞轮→时长保护.

真实走通的业务链路（全部用 W0-W3 现有实现，无 mock）：
  a. 数据准备：3 个「小数比较」单选母题（不同母题、干扰项统一绑定错误类型
     「位数多的小数更大」）×2 实例 + W2 两位数乘法母题 ×2 实例，
     过校验门（schema + 数学双实现验算 dual_check + 查重提示）签发 published；
     另有 subject-english 词汇题（W3 S7 英语包 A 线管线真实产出）。
  b. 学生练习闭环：start_session → get_next_item → submit_answer，
     3 题故意选「位数多的小数更大」干扰项 → 即时评分反馈含 error_feedback、
     错题进 wrong_marks。
  c. 弱项报告：build_weakness_report(student) —— 贝叶斯归因到预设错误类型
     （3 条证据达阈值 concluded，后验置信度>0），recommend_practice 返回
     绑定同错误类型的练习（剔除来源题）。
  d. 复习队列：sync_review_queue 后错题入队（固定间隔策略 [1,3,7,21] 天），
     get_due_reviews 可取出到期条目。
  e. 诊断闭环：assembly 的 diagnosis_profile（孤立题≥3/知识点，R-Z-03）组诊断卷
     → 诊断会话模拟作答 → 诊断场景弱项报告；另演示数据不足时
     「禁止静默放松」的不可行结构化冲突原因路径（InfeasibleError.ConflictReport）。
  f. 数据飞轮：4 名模拟学生作答（真实 score_and_record 落账）→
     run_ctt_calibration(purpose_scope="practice") → item_param 实测值落库
     （source=measured_ctt，practice/diagnosis 分场景隔离，D5）→
     CTT 难度换算 Elo 题目评级，在线掌握度增量更新演示。
  g. 时长保护：低学段（L，15 分钟阈值）会话注入时钟推进 >15 分钟，
     休息提示触发（RestRequiredError），休息确认后恢复。

用法（需 db 容器运行、.env 含 POSTGRES_*）：
    python scripts/demo-w3-business.py
退出码 0 = 全链路通过（结尾打印 W3 BUSINESS E2E PASS）；非 0 = 失败。

幂等性：母题/实例内容寻址（同输入同 id），已 published 版本跳过重复签发；
作答事件/门证书/参数行均为只增账，重复执行只追加新行，不破坏 D1。

演示专用库隔离：本脚本使用独立数据库 muti_w3_demo（可用 W3_DEMO_DB 覆盖），
不存在则自动创建并迁移到 head。为什么不与测试共用 muti_dev：作答事件是
append-only 账（D1，无法 DELETE 清理），演示事件会持续累积；测试套件与
muti_dev 共用一库且 test_gate_bypass 会真实 TRUNCATE 内容表——演示事件
若落在共享库，CTT 标定会读到「事件在、题目版本已被清」的孤儿数据。
独立库让演示与测试互不干扰，两边都可任意重跑。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# ── 让脚本能 import 项目 src（与 tests/conftest.py 同处理）──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 UTF-8 输出兜底（题面含中文/○/> < 符号）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from src.core.assembly.candidates import candidate_from_serving_row  # noqa: E402
from src.core.assembly.profile import diagnosis_profile  # noqa: E402
from src.core.assembly.solver import InfeasibleError, assemble  # noqa: E402
from src.core.content.publication import issue_item_version  # noqa: E402
from src.core.content.writer import publish_item_version  # noqa: E402
from src.core.data.ctt import run_ctt_calibration  # noqa: E402
from src.core.data.elo import (  # noqa: E402
    BASE_RATING,
    difficulty_to_rating,
    elo_update,
    expected_score,
)
from src.core.gate.orchestrator import run_gate  # noqa: E402
from src.core.gate.policy.loader import ChainEntry, GatePolicy, ValidatorStep  # noqa: E402
from src.core.gate.validator import GateContext  # noqa: E402
import src.core.gate.validators.generic  # noqa: E402,F401 —— import 即注册 platform 通用验证器
import src.core.scoring.platform_scorers  # noqa: E402,F401 —— import 即注册 platform 评分器
from src.core.instantiation.dsl.linter import lint  # noqa: E402
from src.core.instantiation.engine import ENGINE_DIGEST, instantiate  # noqa: E402
from src.core.instantiation.expr import evaluate  # noqa: E402
from src.core.models.item_param import ItemParam  # noqa: E402
from src.core.models.item_template import ItemTemplate  # noqa: E402
from src.core.models.item_template_version import ItemTemplateVersion  # noqa: E402
from src.core.models.item_version import ItemVersion  # noqa: E402
from src.core.report.service import build_weakness_report  # noqa: E402
from src.core.review.service import get_due_reviews, sync_review_queue  # noqa: E402
from src.core.scoring.service import score_and_record  # noqa: E402
from src.core.session.service import (  # noqa: E402
    RestRequiredError,
    get_next_item,
    resume_session,
    start_session,
    submit_answer,
)

MATH_PACK_ID = "subject-math"
# pack_digest：与 tests/golden 数学包约定一致（sha256("subject-math")）
MATH_PACK_DIGEST = "sha256:" + hashlib.sha256(MATH_PACK_ID.encode("utf-8")).hexdigest()
ISSUER = "demo-w3-operator"

# 预设错误类型（与 W3 各模块测试口径一致：tests/unit/test_session_service.py）
ERR_DECIMAL = "math.decimal.digits_more_is_larger"
ERR_DECIMAL_LABEL = "位数多的小数更大"
ERR_DECIMAL_OTHER = "math.decimal.compare_misjudge"
KP_DECIMAL = "math.nal.decimal.compare"

SEED = 20260727


# ────────────────────────────────────────────────────────────────────
# 环境：.env 加载 + 异步引擎 + 学科包动态加载
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


def _build_async_dsn(db: str) -> str:
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD 未设置：请通过 .env 或环境变量提供")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


async def _ensure_demo_db() -> str:
    """创建（如不存在）并迁移演示专用库到 head，返回库名.

    与测试共享库（muti_dev）隔离的理由见模块 docstring；迁移复用项目
    alembic（env.py 从 POSTGRES_* 环境变量取 DSN），幂等。
    """
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    demo_db = os.environ.get("W3_DEMO_DB", "muti_w3_demo")

    conn = await asyncpg.connect(
        user=user, password=password, host=host, port=port, database="postgres"
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", demo_db
        )
        if not exists:
            # 标识符来自受控默认值/环境变量，加引号防注入
            await conn.execute(f'CREATE DATABASE "{demo_db}"')
    finally:
        await conn.close()

    env = dict(os.environ, POSTGRES_DB=demo_db)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head",
        cwd=str(PROJECT_ROOT), env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"演示库 {demo_db} 迁移失败：{out.decode('utf-8', 'replace')[-800:]}"
        )
    return demo_db


def _load_module(alias: str, path: Path) -> Any:
    """以 importlib 加载学科包模块（目录名含连字符，无法普通 import）."""
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_math_validators() -> None:
    """加载数学包验证器（模块加载时自注册 dual_check）."""
    _load_module(
        "subject_math_dual_check",
        PROJECT_ROOT / "src" / "packs" / "subject-math" / "validators" / "dual_check.py",
    )


# ────────────────────────────────────────────────────────────────────
# a. 母题 DSL：3 个小数比较单选母题（干扰项绑定错误类型）+ W2 乘法母题
# ────────────────────────────────────────────────────────────────────

def _objective(kp_code: str) -> dict[str, Any]:
    return {
        "kp_set": [{"dimension": "kp", "code": kp_code}],
        "kp_set_mode": "single",  # 孤立题：诊断归因的定位题（§4.5）
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


def _decimal_compare_spec(stem_template: str) -> dict[str, Any]:
    """小数比较单选母题 spec.

    设计要点：
    - 选项即符号 >/< =，正解由 answer_program（a - b 的符号）语义承载，
      scorer_params.answer 为符号串（exact_match 标量比对）；
    - 干扰项经 distractor_rules 绑定错误类型：wrong_md 位绑
      「位数多的小数更大」（认为小数位数多的数更大这一典型迷思），
      other 位绑一般性比较误判——error_bindings 由引擎按规则装配，
      选择题「选项→错误类型」确定映射（架构 §4.5）由此而来；
    - 3 个母题题面措辞不同（template_id 不同），保证诊断卷内
      「同母题不同卷」曝光互斥下仍能满足每知识点≥3 孤立题（R-Z-03）。
    """
    return {
        "objective": _objective(KP_DECIMAL),
        "slots": {
            "a": {"type": "decimal", "difficulty_relevant": True},
            "b": {"type": "decimal", "difficulty_relevant": True},
            # 干扰项符号由调用方按「位数多的小数更大」迷思注入（与英语包模板同手法）
            "wrong_md": {"type": "string", "difficulty_relevant": False},
            "other": {"type": "string", "difficulty_relevant": False},
        },
        "variation_axes": {"axes": []},
        "presentation": {
            "blocks": [{"kind": "text", "template": stem_template}]
        },
        # 正解语义 = a-b 的符号；dual_check 用 SymPy 独立验算该表达式
        "answer_program": {"expression": "a - b", "returns": "number"},
        "distractor_rules": {
            "rules": [
                {
                    "rule_type": "deterministic",
                    "error_type_id": ERR_DECIMAL,
                    "expression": "wrong_md",
                    "label": ERR_DECIMAL_LABEL,
                },
                {
                    "rule_type": "deterministic",
                    "error_type_id": ERR_DECIMAL_OTHER,
                    "expression": "other",
                    "label": "比较关系误判",
                },
            ]
        },
    }


def build_templates() -> list[dict[str, Any]]:
    """4 个母题：3 个小数比较单选（各 2 实例）+ 1 个 W2 两位数乘法（2 实例）."""
    templates: list[dict[str, Any]] = []

    decimal_variants = [
        (
            "tpl-demo-w3-decimal-compare-a",
            "在 ○ 里填 >、< 或 =：{a} ○ {b}",
            [
                # 0.7 只有 1 位小数，0.45 有 2 位 → 迷思认为 0.45 大 → 错选 <
                {"params": {"a": "0.7", "b": "0.45", "wrong_md": "<", "other": "="},
                 "answer": ">"},
                {"params": {"a": "3.25", "b": "3.5", "wrong_md": ">", "other": "="},
                 "answer": "<"},
            ],
        ),
        (
            "tpl-demo-w3-decimal-compare-b",
            "比一比：{a} ○ {b}，○ 里应填哪个符号？",
            [
                {"params": {"a": "4.6", "b": "4.58", "wrong_md": "<", "other": "="},
                 "answer": ">"},
                {"params": {"a": "2.09", "b": "2.9", "wrong_md": ">", "other": "="},
                 "answer": "<"},
            ],
        ),
        (
            "tpl-demo-w3-decimal-compare-c",
            "选出正确的比较结果：{a} ○ {b}",
            [
                # 6.80 位数多但值相等 → 迷思认为 6.80 大 → 错选 <
                {"params": {"a": "6.8", "b": "6.80", "wrong_md": "<", "other": ">"},
                 "answer": "="},
                {"params": {"a": "1.5", "b": "1.47", "wrong_md": "<", "other": "="},
                 "answer": ">"},
            ],
        ),
    ]
    for template_id, stem, instances in decimal_variants:
        templates.append({
            "name": f"小数比较（{template_id.rsplit('-', 1)[-1]}）",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "template_id": template_id,
            "spec": _decimal_compare_spec(stem),
            "instances": instances,
        })

    # W2 成果复用：两位数乘法（与 demo-w2-business.py 同 spec → 同内容寻址 id）
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
        ],
    })

    for t in templates:
        t["template_version"] = {
            "template_version_id": _template_version_id(t["template_id"], t["spec"]),
            "template_id": t["template_id"],
            "dsl_version": "1",
            "spec": t["spec"],
        }
    return templates


def build_demo_policy() -> GatePolicy:
    """门链：schema（阻断）→ dual_check（阻断）→ duplicate_placeholder（提示）."""
    return GatePolicy(
        policy_version="gate-policy-demo-w3",
        status="frozen-candidate",
        description="W3 业务演示链：schema + dual_check + duplicate_placeholder",
        chains=[
            ChainEntry(
                pack_id=MATH_PACK_ID,
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
    """按槽类型构造求值 env，用引擎求值器算正解（供 dual_check 独立比对）."""
    env: dict[str, Any] = {}
    for name, value in params.items():
        slot_type = spec["slots"][name]["type"]
        env[name] = Decimal(str(value)) if slot_type == "decimal" else value
    answer = evaluate(spec["answer_program"]["expression"], env=env)
    if isinstance(answer, Decimal):
        return str(answer)
    return answer


# ────────────────────────────────────────────────────────────────────
# a. 数据准备：母题落库 → 实例化 → 门 → 签发 published
# ────────────────────────────────────────────────────────────────────

async def prepare_math_items(
    session: AsyncSession, policy: GatePolicy
) -> dict[str, Any]:
    """数学实例全链路：DSL→实例化→校验门→签发，返回演示用题目索引."""
    print("=" * 70)
    print("步骤 a：数据准备 —— 母题实例化 → 校验门 → 签发 published")
    print("=" * 70)
    templates = build_templates()

    # DSL Linter + 母题版本落库（item.template_version_id FK 前置；幂等）
    for t in templates:
        result = lint(t["spec"])
        if not result.valid:
            raise RuntimeError(
                f"DSL Linter 未通过（{t['name']}）：{[e.message for e in result.errors]}"
            )
        tv = t["template_version"]
        if await session.get(ItemTemplate, tv["template_id"]) is None:
            session.add(ItemTemplate(
                template_id=tv["template_id"], pack_id=MATH_PACK_ID,
                current_version_id=None,
            ))
            await session.flush()
        if await session.get(ItemTemplateVersion, tv["template_version_id"]) is None:
            session.add(ItemTemplateVersion(
                template_version_id=tv["template_version_id"],
                template_id=tv["template_id"],
                dsl_version=tv["dsl_version"],
                spec=t["spec"],
                status="draft",
            ))
    await session.commit()
    print(f"  ✅ {len(templates)} 个母题 Linter PASS，版本落库（幂等）")

    decimal_items: list[dict[str, Any]] = []  # 6 个小数比较实例（演示主载体）
    mul_items: list[dict[str, Any]] = []
    for t in templates:
        for inst in t["instances"]:
            params, answer = inst["params"], inst["answer"]
            result = instantiate(
                t["template_version"], params,
                pack_digest=MATH_PACK_DIGEST,
                interaction_id=t["interaction_id"],
                scorer_id=t["scorer_id"],
                scorer_params={"answer": answer},
                locale="zh-CN", corpus_digests=[], seed=0,
            )
            iv_id = result.item_version_id

            existing = await session.get(ItemVersion, iv_id)
            if existing is None:
                vd = result.model_dump()
                vd.update({
                    "pack_id": MATH_PACK_ID,
                    "tier": "A",
                    "template_version_id": t["template_version"]["template_version_id"],
                    "template_version_digest": t["template_version"]["template_version_id"],
                    "normalized_params": result.lineage["params"]["normalized"],
                    "pack_digest": MATH_PACK_DIGEST,
                    "engine_digest": ENGINE_DIGEST,
                    "corpus_digests": [],
                    "locale": "zh-CN",
                })
                ids = await publish_item_version(
                    item_id=None, version_data=vd,
                    gate_certificate_id=None, db=session,
                )
                assert ids["item_version_id"] == iv_id

            # 校验门：schema → dual_check → duplicate_placeholder
            payload = {
                "objective": result.objective,
                "interaction_ref": result.interaction_ref,
                "content": result.content,
                "scoring_ref": result.scoring_ref,
                "error_bindings": result.error_bindings,
                "lineage": result.lineage,
                # dual_check 验算载荷：母题 spec + 参数 + 引擎正解。
                # 字符串槽（干扰项符号 >/< 等）不参与 answer_program 算术，
                # SymPy 环境只取数值槽（否则 sympify('<') 无法解析 → review）。
                "spec": t["spec"],
                "params": {
                    k: v for k, v in params.items()
                    if t["spec"]["slots"][k]["type"] in ("int", "decimal", "fraction")
                },
                "engine_answer": _engine_answer(t["spec"], params),
            }
            ctx = GateContext(
                artifact_type="item", pack_id=MATH_PACK_ID,
                artifact_payload=payload, db=session,
                required_keys=[
                    "objective", "interaction_ref", "content",
                    "scoring_ref", "error_bindings", "lineage",
                ],
            )
            outcome = await run_gate(
                artifact_ref=iv_id, artifact_type="item", pack_id=MATH_PACK_ID,
                ctx=ctx, policy=policy, db=session,
                issued_by=ISSUER, cert_type="publish",
            )
            if outcome.final_verdict != "pass" or outcome.cert_id is None:
                verdicts = ", ".join(
                    f"{r.validator_id}={r.verdict}" for r in outcome.runs
                )
                raise RuntimeError(f"门未通过（{t['name']} {params}）：{verdicts}")

            version = await session.get(ItemVersion, iv_id)
            action = "已 published（幂等跳过）"
            if version.status != "published":
                await issue_item_version(
                    item_version_id=iv_id,
                    gate_certificate_id=outcome.cert_id,
                    published_by=ISSUER, db=session,
                )
                action = "新签发 published"

            stem = result.content["blocks"][0]["rendered"]
            rec = {
                "item_version_id": iv_id,
                "template_id": t["template_id"],
                "stem": stem,
                "answer": answer,
                "interaction_id": t["interaction_id"],
                "error_bindings": result.error_bindings,
            }
            (decimal_items if t["interaction_id"] == "single_choice" else mul_items).append(rec)
            bind_note = ""
            if result.error_bindings:
                bind_note = "  干扰项绑定=" + ",".join(
                    f"{b['option_value']}→{b['label']}" for b in result.error_bindings
                )
            print(f"  ✅ {stem}  正解={answer}（{action}）{bind_note}")

    return {"decimal": decimal_items, "mul": mul_items}


async def prepare_english_items(session: AsyncSession) -> list[dict[str, Any]]:
    """英语词汇题：跑 W3 S7 英语包 A 线真实管线，取词汇单选实例."""
    pipeline = _load_module(
        "subject_english_pipeline",
        PROJECT_ROOT / "src" / "packs" / "subject-english" / "english_pipeline.py",
    )
    results = await pipeline.run_pipeline(n_words=6, db=session)
    choice_items: list[dict[str, Any]] = []
    for r in results:
        if r["kind"] != "choice":
            continue
        version = await session.get(ItemVersion, r["item_version_id"])
        if version is None or version.status != "published":
            raise RuntimeError(
                f"英语词汇题未 published：{r['word']} → {r.get('publish_result')}"
            )
        answer = (version.scoring_ref or {}).get("scorer_params", {}).get("answer")
        choice_items.append({
            "item_version_id": version.item_version_id,
            "stem": f'选出单词 "{r["word"]}" 的正确释义',
            "answer": answer,
        })
    print(
        f"  ✅ subject-english 词汇题 {len(choice_items)} 道 published"
        f"（A 线管线真实产出：{[c['stem'] for c in choice_items]}）"
    )
    return choice_items


# ────────────────────────────────────────────────────────────────────
# 作答辅助
# ────────────────────────────────────────────────────────────────────

def _wrong_option(rec: dict[str, Any], error_type_id: str = ERR_DECIMAL) -> str:
    """取绑定了指定错误类型的干扰项选项值（故意答错用）."""
    for b in rec["error_bindings"]:
        if b.get("error_type_id") == error_type_id:
            return str(b["option_value"])
    raise RuntimeError(f"题目无绑定 {error_type_id} 的干扰项：{rec['stem']}")


async def _serve_and_answer(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    expect_stem: str,
    response: dict[str, Any],
    duration_ms: int,
) -> Any:
    """取当前题 → 校验题面 → 提交作答 → 打印即时反馈."""
    nxt = await get_next_item(db, session_id)
    assert nxt is not None, "会话已提前完成，无题可取"
    stem = next(
        (b.get("rendered") or b.get("value") or "" for b in nxt.content_blocks), ""
    )
    fb = await submit_answer(
        db, session_id,
        item_version_id=nxt.item_version_id,
        response=response,
        duration_ms=duration_ms,
    )
    verdict = "✓ 对" if fb.correct else "✗ 错"
    shown = response.get("selected", response.get("answer"))
    print(f"  题{nxt.position}/{nxt.total} {stem}")
    print(f"     作答={shown} → {verdict}（session={fb.session_status}）")
    for ef in fb.error_feedback:
        print(
            f"     错误归因：{ef['error_type_id']}（label={ef['label']}，"
            f"推断置信度={ef['confidence']}）"
        )
    return fb


# ────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────

async def main() -> int:
    _load_dotenv()
    demo_db = await _ensure_demo_db()
    print(f"演示专用库：{demo_db}（已创建/迁移到 head；与测试共享库隔离）")
    engine = create_async_engine(
        _build_async_dsn(demo_db), echo=False, pool_pre_ping=True
    )
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

            # ── a. 数据准备 ──────────────────────────────────────
            pool = await prepare_math_items(session, policy)
            decimal_items, mul_items = pool["decimal"], pool["mul"]
            assert len(decimal_items) == 6 and len(mul_items) == 2
            english_items = await prepare_english_items(session)
            assert len(english_items) >= 2

            student_alias = uuid.uuid4()
            print(f"\n  演示学生 student_alias_id = {student_alias}（匿名 id，D7）")

            # ── b. 学生练习闭环 ──────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 b：学生练习闭环 —— start_session → 取题 → 提交 → 即时反馈")
            print("=" * 70)
            # 题序：小数比较 A1/B1/C1（故意错选「位数多更大」干扰项）
            #      + A2（答对）+ 乘法（答对）+ 英语词汇 ×2（答对）
            a1, a2 = decimal_items[0], decimal_items[1]
            b1, b2 = decimal_items[2], decimal_items[3]
            c1, c2 = decimal_items[4], decimal_items[5]
            practice_plan = [
                (a1, {"selected": _wrong_option(a1)}, "故意错选"),
                (b1, {"selected": _wrong_option(b1)}, "故意错选"),
                (c1, {"selected": _wrong_option(c1)}, "故意错选"),
                (a2, {"selected": a2["answer"]}, "答对"),
                (mul_items[0], {"answer": mul_items[0]["answer"]}, "答对"),
                (english_items[0], {"selected": english_items[0]["answer"]}, "答对"),
                (english_items[1], {"selected": english_items[1]["answer"]}, "答对"),
            ]
            ps = await start_session(
                session,
                student_alias_id=student_alias,
                gradeband="L",
                scene="practice",
                item_version_ids=[r["item_version_id"] for r, _, _ in practice_plan],
            )
            print(f"  会话 session_id={ps.session_id}（scene=practice，"
                  f"gradeband=L，时长阈值={ps.time_limit_sec // 60} 分钟）")
            feedbacks = []
            for i, (rec, response, note) in enumerate(practice_plan, start=1):
                fb = await _serve_and_answer(
                    session, ps.session_id,
                    expect_stem=rec["stem"],
                    response=response,
                    duration_ms=12000 + i * 1500,
                )
                fb._demo_note = note  # 仅打印用
                feedbacks.append(fb)

            # 验证：3 道故意错题的即时反馈必须含预设错误类型的 error_feedback
            wrong_fbs = feedbacks[:3]
            for fb in wrong_fbs:
                assert not fb.correct
                assert any(
                    ef["error_type_id"] == ERR_DECIMAL and ef["label"] == ERR_DECIMAL_LABEL
                    for ef in fb.error_feedback
                ), "即时反馈缺少预设错误类型归因"
            await session.refresh(ps)
            marks = ps.wrong_marks or []
            assert len(marks) == 3, f"错题标记数应为 3，实际 {len(marks)}"
            print(f"  ✅ 3 道故意错题的即时反馈均含 error_feedback"
                  f"（{ERR_DECIMAL} / {ERR_DECIMAL_LABEL}）")
            print(f"  ✅ 错题已标记 wrong_marks={len(marks)} 条"
                  f"（进度 {ps.correct_count}/{ps.answered_count} 对）")

            # ── c. 弱项报告 ──────────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 c：弱项报告 —— build_weakness_report（贝叶斯归因 + 针对性练习）")
            print("=" * 70)
            report = await build_weakness_report(
                session, student_alias_id=student_alias, scene="practice",
            )
            assert report.items, "弱项报告为空"
            top = report.items[0]
            assert top.error_type_id == ERR_DECIMAL, (
                f"首要弱项应为 {ERR_DECIMAL}，实际 {top.error_type_id}"
            )
            assert top.status == "concluded" and top.confidence > 0
            assert top.recommended_item_version_ids, "针对性练习推荐为空"
            print(f"  弱项报告（scene=practice，min_evidence={report.min_evidence}）：")
            for item in report.items:
                print(
                    f"    - {item.error_type_id}：status={item.status} "
                    f"证据={item.evidence_count} 条 "
                    f"归因置信度（贝叶斯后验）={item.confidence}"
                )
            contributing = {a1["item_version_id"], b1["item_version_id"], c1["item_version_id"]}
            rec_ids = top.recommended_item_version_ids
            assert not (contributing & set(rec_ids)), "推荐练习必须剔除来源题"
            stem_by_id = {d["item_version_id"]: d["stem"] for d in decimal_items}
            print(f"  ✅ 归因到预设错误类型「{ERR_DECIMAL_LABEL}」"
                  f"（{ERR_DECIMAL}，后验 {top.confidence}）")
            print(f"  ✅ recommend_practice 返回 {len(rec_ids)} 道针对练习（已剔除来源题）：")
            for rid in rec_ids:
                print(f"     - {stem_by_id.get(rid, rid[:40])}")

            # ── d. 复习队列 ──────────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 d：复习队列 —— sync_review_queue 入队 → get_due_reviews 取出")
            print("=" * 70)
            queued = await sync_review_queue(session, student_alias_id=student_alias)
            assert queued >= 3, f"错题入队数应 ≥3，实际 {queued}"
            # 策略间隔 [1,3,7,21] 天：错题 stage=0，due=答错时刻+1 天
            due = await get_due_reviews(
                session, student_alias_id=student_alias,
                now=datetime.now(timezone.utc) + timedelta(days=2),
            )
            assert due, "到期复习条目为空"
            print(f"  ✅ sync_review_queue 在队 {queued} 条（全量重放幂等，策略 fixed-interval/1.0.0）")
            print(f"  ✅ get_due_reviews（时钟推到 +2 天）取出 {len(due)} 条到期错题：")
            for entry in due:
                print(
                    f"     - {stem_by_id.get(entry.item_version_id, entry.item_version_id[:36])}"
                    f"  stage={entry.stage} due_at={entry.due_at:%Y-%m-%d %H:%M}"
                    f" 来源错误={entry.source_error_type_id}"
                )

            # ── e. 诊断闭环 ──────────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 e：诊断闭环 —— diagnosis_profile 组卷（孤立题≥3/知识点）→ 作答 → 报告")
            print("=" * 70)
            # 候选池：6 个小数比较实例（3 母题 × 2 实例，真实 DB 行构建）
            candidates = []
            for rec in decimal_items:
                v = await session.get(ItemVersion, rec["item_version_id"])
                candidates.append(candidate_from_serving_row({
                    "item_version_id": v.item_version_id,
                    "item_id": v.item_id,
                    # ORM 无 template_version_id 列，承载在 lineage JSONB（契约 §2.3）
                    "template_version_id": (v.lineage or {}).get("template_version_id"),
                    "objective": v.objective,
                    "interaction_ref": v.interaction_ref,
                    "lineage": v.lineage,
                }))
            profile = diagnosis_profile(
                profile_id="diag-demo-w3",
                profile_version="1.0.0",
                gradeband="L",
                kp_codes=[KP_DECIMAL],
                item_count_range=(3, 6),
                min_items_per_isolated_kp=3,
                target_p_correct_range=None,  # 冷启动无先验，不加正确率区间约束
            )
            result = assemble(
                profile, candidates, seed=SEED,
                snapshot_ref="kp-snap-demo-w3-decimal",
            )
            diag_ids = [it.item_version_id for it in result.items]
            assert len(diag_ids) >= 3
            diag_templates = {
                next(r["template_id"] for r in decimal_items
                     if r["item_version_id"] == i)
                for i in diag_ids
            }
            assert len(diag_templates) == len(diag_ids), "同卷同母题互斥被破坏"
            print(f"  诊断 Profile：purpose=diagnosis，孤立题≥3/知识点（R-Z-03），"
                  f"题量 [3,6]，seed={SEED}")
            print(f"  ✅ 组卷 {len(diag_ids)} 题（selection_digest={result.selection_digest[:16]}…，"
                  f"母题互斥 ✓）：")
            for iid in diag_ids:
                print(f"     - {stem_by_id[iid]}")

            # 不可行路径：追加无候选知识点 → 禁止静默放松，结构化冲突原因
            bad_profile = diagnosis_profile(
                profile_id="diag-demo-w3-infeasible",
                profile_version="1.0.0",
                gradeband="L",
                kp_codes=[KP_DECIMAL, "math.nal.fraction.add"],
                item_count_range=(6, 12),
                min_items_per_isolated_kp=3,
                target_p_correct_range=None,
            )
            try:
                assemble(bad_profile, candidates, seed=SEED,
                         snapshot_ref="kp-snap-demo-w3-decimal")
                raise RuntimeError("预期不可行却组卷成功（静默放松！）")
            except InfeasibleError as e:
                conflict = e.report.conflicts[0]
                print(f"  ✅ 数据不足路径：InfeasibleError 结构化冲突原因（禁止静默放松）：")
                print(f"     constraint={conflict.constraint_id} kp={conflict.kp_code}"
                      f" 需≥{conflict.required} 可用={conflict.available}")

            # 诊断会话作答：3 题全部错选「位数多更大」干扰项
            ds = await start_session(
                session,
                student_alias_id=student_alias,
                gradeband="L",
                scene="diagnosis",
                item_version_ids=diag_ids,
            )
            rec_by_id = {r["item_version_id"]: r for r in decimal_items}
            for iid in diag_ids:
                rec = rec_by_id[iid]
                await _serve_and_answer(
                    session, ds.session_id,
                    expect_stem=rec["stem"],
                    response={"selected": _wrong_option(rec)},
                    duration_ms=18000,
                )
            diag_report = await build_weakness_report(
                session, student_alias_id=student_alias, scene="diagnosis",
            )
            diag_top = diag_report.items[0]
            assert diag_top.error_type_id == ERR_DECIMAL
            assert diag_top.status == "concluded"
            print(f"  ✅ 诊断场景弱项报告：{diag_top.error_type_id} "
                  f"证据={diag_top.evidence_count} 后验={diag_top.confidence} "
                  f"→ 推荐 {len(diag_top.recommended_item_version_ids)} 道针对练习"
                  f"（与 practice 场景分场景取数，D5）")

            # ── f. 数据飞轮：CTT 标定 → Elo 掌握度 ───────────────
            print()
            print("=" * 70)
            print("步骤 f：数据飞轮 —— 模拟 cohort 作答 → CTT 标定落库 → Elo 掌握度更新")
            print("=" * 70)
            # 4 名模拟学生 × 6 道小数比较题（真实 score_and_record 评分落账）：
            # 每题恰被 1 人答错（错选迷思干扰项）→ cohort 正确率 3/4
            cohort = [uuid.uuid4() for _ in range(4)]
            for j, rec in enumerate(decimal_items):
                version = await session.get(ItemVersion, rec["item_version_id"])
                for i, alias in enumerate(cohort):
                    wrong = (j % len(cohort)) == i
                    selected = _wrong_option(rec) if wrong else rec["answer"]
                    await score_and_record(
                        session,
                        item_version=version,
                        response={"selected": selected},
                        student_alias_id=alias,
                        scene="practice",
                        pack_id=MATH_PACK_ID,
                        duration_ms=10000 + i * 800,
                    )
            print(f"  模拟 cohort：{len(cohort)} 名学生 × {len(decimal_items)} 题"
                  f"作答落账（score_and_record 真实评分）")

            written = await run_ctt_calibration(session, purpose_scope="practice")
            assert written, "CTT practice 标定无产出"
            assert all(r.source == "measured_ctt" for r in written)
            assert all(r.purpose_scope == "practice" for r in written)
            print(f"  ✅ run_ctt_calibration(purpose_scope='practice') 落库 "
                  f"{len(written)} 行 item_param（source=measured_ctt，method=ctt-v1）：")
            shown = 0
            difficulty_by_id: dict[str, float] = {}
            for row in written:
                difficulty_by_id[row.item_version_id] = float(row.params["difficulty"])
                if row.item_version_id in stem_by_id and shown < 6:
                    shown += 1
                    disc = row.params["discrimination"]
                    print(
                        f"     - {stem_by_id[row.item_version_id]}"
                        f"  n={row.sample_size} 难度p={row.params['difficulty']:.3f}"
                        f" 区分度={disc if disc is None else f'{disc:.3f}'}"
                    )
            diag_written = await run_ctt_calibration(session, purpose_scope="diagnosis")
            assert all(r.purpose_scope == "diagnosis" for r in diag_written)
            # 分场景隔离实证：同一题 practice 与 diagnosis 各自独立估计
            a1_diag = next(
                (r for r in diag_written if r.item_version_id == a1["item_version_id"]),
                None,
            )
            a1_practice_p = difficulty_by_id.get(a1["item_version_id"])
            assert a1_practice_p is not None
            print(f"  ✅ 分场景隔离（D5）：run_ctt_calibration(purpose_scope='diagnosis') "
                  f"落库 {len(diag_written)} 行")
            if a1_diag is not None:
                print(
                    f"     同题「{a1['stem']}」practice 难度={a1_practice_p:.3f}"
                    f"（n≥5）vs diagnosis 难度={a1_diag.params['difficulty']:.3f}"
                    f"（n={a1_diag.sample_size}）——独立估计不混估"
                )
            # 库内复核：source/scope 物理分存储
            db_rows = (
                await session.execute(
                    select(ItemParam.source, ItemParam.purpose_scope)
                    .distinct()
                )
            ).all()
            print(f"     item_param 现存 (source, purpose_scope) 组合："
                  f"{sorted({(r[0], r[1]) for r in db_rows})}")

            # Elo 掌握度：CTT 难度换算题目评级，回放演示学生练习作答序列
            print("  Elo 掌握度在线更新（学生基准 1500，题目评级由 CTT 难度换算）：")
            r_s = BASE_RATING
            outcomes = [(a1, 0.0), (b1, 0.0), (c1, 0.0), (a2, 1.0)]
            for rec, score in outcomes:
                p = difficulty_by_id[rec["item_version_id"]]
                r_i = difficulty_to_rating(p)
                e = expected_score(r_s, r_i)
                r_s, _ = elo_update(r_s, r_i, score)
                print(
                    f"     {rec['stem']}  题p={p:.2f}→R_i={r_i:.0f} "
                    f"期望得分E={e:.2f} 实际S={score:.0f} → R_s={r_s:.1f}"
                )
            assert r_s < BASE_RATING, "连错 3 题后掌握度应低于基准"
            print(f"  ✅ 掌握度 {BASE_RATING:.0f} → {r_s:.1f}"
                  f"（3 错 1 对，「位数多更大」迷思拉低评级）")

            # ── g. 时长保护 ──────────────────────────────────────
            print()
            print("=" * 70)
            print("步骤 g：时长保护 —— 低学段（L）15 分钟阈值，注入时钟推进验证")
            print("=" * 70)
            t0 = datetime.now(timezone.utc)
            gs = await start_session(
                session,
                student_alias_id=uuid.uuid4(),
                gradeband="L",
                scene="practice",
                item_version_ids=[mul_items[1]["item_version_id"]],
                now=t0,
            )
            nxt = await get_next_item(session, gs.session_id, now=t0)
            assert nxt is not None
            print(f"  会话 gradeband=L（阈值 {gs.time_limit_sec // 60} 分钟），"
                  f"第 1 题已出示：{nxt.content_blocks[0].get('rendered')}")
            later = t0 + timedelta(minutes=16)
            triggered = False
            try:
                await submit_answer(
                    session, gs.session_id,
                    item_version_id=nxt.item_version_id,
                    response={"answer": mul_items[1]["answer"]},
                    duration_ms=5000,
                    now=later,
                )
            except RestRequiredError as e:
                triggered = True
                print(f"  ✅ 时钟推进 16 分钟后提交 → 休息提示触发：")
                print(f"     「{e.message}」")
                print(f"     （已连续作答 {e.elapsed_sec // 60} 分钟 > "
                      f"阈值 {e.time_limit_sec // 60} 分钟，取题/提交均被阻断）")
            assert triggered, "时长保护未触发"
            state = await resume_session(session, gs.session_id, now=later)
            assert state.status == "active" and state.remaining_sec == state.time_limit_sec
            print(f"  ✅ 休息确认 resume_session：status={state.status}，"
                  f"计时锚点重置（剩余 {state.remaining_sec // 60} 分钟）")

        print()
        print("=" * 70)
        print("W3 BUSINESS E2E PASS")
        print("=" * 70)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:  # noqa: BLE001 —— 演示脚本：任何失败即链路不通
        print(f"W3 BUSINESS E2E FAIL: {e}", file=sys.stderr)
        sys.exit(1)
