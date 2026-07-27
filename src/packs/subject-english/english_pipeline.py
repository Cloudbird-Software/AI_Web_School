"""英语词汇 A 线全链路管线（W3 S7）.

端到端链路（两个模板各走一遍，验证「模板→实例化→词表校验→签证→入库」）：
  课标词表 JSON → 选词 → （词汇单选：选干扰释义+洗牌选项 / 单词拼写：生成首字母提示）
  → T-W2-004 引擎实例化 → 通用验证器（schema/license/duplicate）
  + 英语词表等级校验（word_in_vocab）→ issue_certificate 签发门证书
  → W1 publish_item_version 入库 → 渲染 HTML 片段

license 说明（W3 S9-② 适配后）：item 产物不携带 license_id——许可义务在
语料侧（词表 license_id 经 SourceRegistry.is_approved 核验），
LicenseValidator 对无 license_id 的 item 返回 pass（skipped）。

运行方式（在 worktree 根目录）：
  python -m src.packs.subject-english.english_pipeline
  或
  python src/packs/subject-english/english_pipeline.py

宪法 X6：本脚本属于学科包，可 import 核心域；核心域不 import 本脚本。
宪法 D4：交互与评分器只复用注册表（single_choice/text_blank + exact_match）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
from pathlib import Path
from typing import Any

import yaml

# ────────────────────────────────────────────────────────────────────
# 让脚本可独立运行：把项目根加入 sys.path
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.content.source_registry import SourceRegistry  # noqa: E402
from src.core.content.writer import publish_item_version  # noqa: E402
from src.core.gate.certifier.service import issue_certificate  # noqa: E402
from src.core.gate.validator import GateContext, ValidatorResult  # noqa: E402
from src.core.gate.validators.generic import (  # noqa: E402
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)
from src.core.instantiation.engine import ENGINE_DIGEST, instantiate  # noqa: E402
from src.core.models.item_template import ItemTemplate  # noqa: E402
from src.core.models.item_template_version import ItemTemplateVersion  # noqa: E402

# ────────────────────────────────────────────────────────────────────
# 路径与摘要常量
# ────────────────────────────────────────────────────────────────────
_PACK_DIR = Path(__file__).resolve().parent
_VOCAB_PATH = _PACK_DIR / "corpora" / "curriculum_words.json"
_TEMPLATE_CHOICE_PATH = _PACK_DIR / "templates" / "vocab_single_choice.yaml"
_TEMPLATE_SPELLING_PATH = _PACK_DIR / "templates" / "word_spelling.yaml"
_VALIDATOR_PATH = _PACK_DIR / "validators" / "word_in_vocab.py"

_PACK_DIGEST = "sha256:pack-subject-english-v1"
_CORPUS_DIGEST = "sha256:corpus-curriculum-words-en-v1"
_POLICY_VERSION = "w3-english-v1"
_SIGNED_BY = "w3-english-pipeline"

_LETTERS = ("A", "B", "C", "D")


# ────────────────────────────────────────────────────────────────────
# importlib 加载英语学科验证器（目录名含连字符，无法用普通 import）
# ────────────────────────────────────────────────────────────────────


def load_vocab_validator_cls():
    """以 importlib 加载 word_in_vocab.py，触发 register_validator.

    Returns:
        WordInVocabValidator 类。
    """
    spec = importlib.util.spec_from_file_location(
        "subject_english_word_in_vocab", _VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_VALIDATOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.WordInVocabValidator


# ────────────────────────────────────────────────────────────────────
# 词表加载 / 选词 / 干扰项选取 / 提示生成
# ────────────────────────────────────────────────────────────────────


def load_vocab(path: Path = _VOCAB_PATH) -> dict[str, Any]:
    """加载课标词表 JSON."""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def pick_words(vocab: dict[str, Any], n: int) -> list[dict]:
    """从词表确定性选取前 n 个二级词（同输入同选词，D3 可复现）."""
    words = [w for w in vocab.get("words", []) if w.get("level") == "二级"]
    return words[:n]


def make_hint(word: str) -> str:
    """首字母提示：'a _ _ _ _'（首字母 + 剩余字母位下划线）."""
    if len(word) <= 1:
        return word
    return word[0] + " " + " ".join("_" for _ in word[1:])


def _distinct_meanings(vocab: dict[str, Any], exclude: set[str]) -> list[dict]:
    """词表中释义不在 exclude 集合内的词条（保序）."""
    return [
        w for w in vocab["words"]
        if w["meaning"] not in exclude
    ]


def pick_distractors(
    vocab: dict[str, Any], target: dict, *, seed: int
) -> tuple[dict, dict, dict]:
    """为目标词选三个干扰词条：d1 形近 / d2 近义 / d3 同主题.

    规则（确定性，seed 由调用方按词序注入）：
    - d3 同主题混淆：同 theme 且不同词；
    - d1 形近混淆：首字母相同的不同词（小学词表内最接近的「形近」代理）；
    - d2 近义混淆：剩余词中确定性任取（词表未标同义组，v1 从简）。
    三者释义互不相同且 ≠ 目标释义（否则跳过候选顺延）。
    """
    rng = random.Random(seed)
    used_meanings = {target["meaning"]}

    def _take(candidates: list[dict]) -> dict:
        pool = [c for c in candidates if c["meaning"] not in used_meanings]
        if not pool:
            raise ValueError(f"词表不足以为 {target['word']} 选干扰项")
        pick = pool[rng.randrange(len(pool))]
        used_meanings.add(pick["meaning"])
        return pick

    others = _distinct_meanings(vocab, {target["meaning"]})
    same_theme = [w for w in others if w.get("theme") == target.get("theme")]
    same_initial = [
        w for w in others
        if w["word"][0].lower() == target["word"][0].lower()
        and w["word"] != target["word"]
    ]
    d1 = _take(same_initial or others)
    d2 = _take(others)
    d3 = _take(same_theme or others)
    return d1, d2, d3


def build_choice_params(
    target: dict, d1: dict, d2: dict, d3: dict, *, seed: int
) -> dict[str, Any]:
    """组装词汇单选实例化参数（含洗牌后的选项与正确字母）."""
    options = [target["meaning"], d1["meaning"], d2["meaning"], d3["meaning"]]
    rng = random.Random(seed)
    rng.shuffle(options)
    answer_letter = _LETTERS[options.index(target["meaning"])]
    return {
        "word": target["word"],
        "meaning": target["meaning"],
        "d1": d1["meaning"],
        "d2": d2["meaning"],
        "d3": d3["meaning"],
        "opt_a": options[0],
        "opt_b": options[1],
        "opt_c": options[2],
        "opt_d": options[3],
        "answer_letter": answer_letter,
    }


# ────────────────────────────────────────────────────────────────────
# 渲染 HTML 片段
# ────────────────────────────────────────────────────────────────────


def render_html(item_version: dict, *, kind: str) -> str:
    """把实例化产物渲染为简单 HTML 片段（kind=choice/spelling）."""
    blocks = item_version.get("content", {}).get("blocks", [])
    texts = [b.get("rendered", "") for b in blocks if b.get("kind") == "text"]
    lines = "\n".join(f"  <p>{t}</p>" for t in texts)
    return f'<div class="english-{kind}">\n{lines}\n</div>'


# ────────────────────────────────────────────────────────────────────
# 单题全链路：实例化 → 验证 → 签证 → 入库
# ────────────────────────────────────────────────────────────────────


async def _process_one(
    *,
    kind: str,
    template_version: dict,
    params: dict[str, Any],
    interaction_id: str,
    scorer_params: dict[str, Any],
    target_word: str,
    db: AsyncSession,
    vocab_validator_cls,
) -> dict[str, Any]:
    """处理一题：实例化 → 验证 → 签证 → 入库 → 渲染.

    Returns:
        结果摘要 dict：{kind, word, item_version_id, cert_id, verdicts,
        publish_result, html}。
    """
    result = instantiate(
        template_version,
        params=params,
        pack_digest=_PACK_DIGEST,
        interaction_id=interaction_id,
        scorer_id="exact_match",
        scorer_params=scorer_params,
        locale="zh-CN",
        corpus_digests=[_CORPUS_DIGEST],
        seed=0,
        signed_by=_SIGNED_BY,
        signed_at="2026-07-27T00:00:00+00:00",
    )
    item_version_dict = result.model_dump()
    item_version_id = item_version_dict["item_version_id"]

    artifact_payload = {
        "objective": item_version_dict["objective"],
        "interaction_ref": item_version_dict["interaction_ref"],
        "content": item_version_dict["content"],
        "scoring_ref": item_version_dict["scoring_ref"],
        "lineage": item_version_dict["lineage"],
    }

    # schema（结构必填键）
    schema_result = await SchemaValidator().validate(
        item_version_id,
        GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload=artifact_payload,
            required_keys=[
                "objective", "interaction_ref", "content",
                "scoring_ref", "lineage",
            ],
        ),
    )
    # license（item 无 license_id：W3 S9-② 适配后 pass/skipped；
    # 词表许可在语料侧由 SourceRegistry 核验，见 run_pipeline）
    license_result = await LicenseValidator().validate(
        item_version_id,
        GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload=artifact_payload,
            db=db,
        ),
    )
    # 词表等级校验（目标词必须在课标词表内）
    vocab_result = await vocab_validator_cls().validate(
        item_version_id,
        GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload=artifact_payload,
            target_word=target_word,
        ),
    )
    # 查重占位（非阻断）
    dup_result = await DuplicatePlaceholderValidator().validate(
        item_version_id,
        GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload=artifact_payload,
            db=db,
        ),
    )

    runs: list[tuple[ValidatorResult, bool]] = [
        (schema_result, True),
        (license_result, True),
        (vocab_result, True),
        (dup_result, False),
    ]
    verdicts = {
        "schema": schema_result.verdict,
        "license": license_result.verdict,
        "word_in_vocab": vocab_result.verdict,
        "duplicate": dup_result.verdict,
    }

    cert_id: str | None = None
    try:
        cert_id = await issue_certificate(
            artifact_ref=item_version_id,
            cert_type="publish",
            policy_version=_POLICY_VERSION,
            issued_by=_SIGNED_BY,
            runs=runs,
            db=db,
        )
    except Exception as e:
        verdicts["cert_error"] = str(e)

    html = render_html(item_version_dict, kind=kind)
    publish_result = None
    if cert_id:
        version_data = {
            "pack_id": "subject-english",
            "tier": "A",
            "status": "published",
            "template_version_id": template_version["template_version_id"],
            "template_version_digest": template_version["template_version_id"],
            "pack_digest": _PACK_DIGEST,
            "engine_digest": ENGINE_DIGEST,
            "corpus_digests": [_CORPUS_DIGEST],
            "normalized_params": item_version_dict["lineage"]["params"]["normalized"],
            "locale": "zh-CN",
            "objective": item_version_dict["objective"],
            "interaction_ref": item_version_dict["interaction_ref"],
            "content": item_version_dict["content"],
            "scoring_ref": item_version_dict["scoring_ref"],
            "error_bindings": item_version_dict["error_bindings"],
            "lineage": item_version_dict["lineage"],
            "rendered_snapshot": {"html": html},
        }
        try:
            publish_result = await publish_item_version(
                item_id=None,
                version_data=version_data,
                gate_certificate_id=cert_id,
                db=db,
            )
        except Exception as e:
            publish_result = {"error": str(e)}

    return {
        "kind": kind,
        "word": target_word,
        "item_version_id": item_version_id,
        "cert_id": cert_id,
        "verdicts": verdicts,
        "publish_result": publish_result,
        "html": html,
    }


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────


async def _ensure_templates(
    db: AsyncSession, templates: list[dict[str, Any]]
) -> None:
    """幂等落库母题 + 母题版本（status=draft）.

    item.template_version_id FK 要求母题版本先存在（契约 §2.3）；
    幂等：同 template_id / template_version_id 已存在则复用（D1 只增不改）。
    """
    for tv in templates:
        if await db.get(ItemTemplate, tv["template_id"]) is None:
            db.add(
                ItemTemplate(
                    template_id=tv["template_id"],
                    pack_id="subject-english",
                    current_version_id=None,
                )
            )
            await db.flush()
        if await db.get(ItemTemplateVersion, tv["template_version_id"]) is None:
            db.add(
                ItemTemplateVersion(
                    template_version_id=tv["template_version_id"],
                    template_id=tv["template_id"],
                    dsl_version=tv["dsl_version"],
                    spec=tv["spec"],
                    status="draft",
                )
            )
    await db.commit()


async def run_pipeline(
    n_words: int = 12,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """运行全链路管线：前 n 个二级词，奇偶交替拼写/单选两题型.

    Args:
        n_words: 选词数量（≥10）。
        db: 可选注入的会话（测试用事务回滚隔离）；
            None 时按环境变量自建引擎（默认库 muti_w3_packs）。

    Returns:
        每题处理结果列表。
    """
    with _TEMPLATE_CHOICE_PATH.open(encoding="utf-8") as f:
        template_choice = yaml.safe_load(f)
    with _TEMPLATE_SPELLING_PATH.open(encoding="utf-8") as f:
        template_spelling = yaml.safe_load(f)
    vocab = load_vocab()
    vocab_validator_cls = load_vocab_validator_cls()

    # 语料侧许可核验（R-Q-18）：词表 license_id 必须 approved
    license_id = vocab["license_id"]
    if not SourceRegistry.from_yaml().is_approved(license_id):
        raise RuntimeError(f"词表 license_id={license_id} 未 approved")

    words = pick_words(vocab, n_words)
    print(f"[pipeline] 选中 {len(words)} 个二级词：{[w['word'] for w in words]}")

    async def _run(session: AsyncSession) -> list[dict[str, Any]]:
        # 母题版本幂等落库（item.template_version_id FK 前置）
        await _ensure_templates(session, [template_spelling, template_choice])
        results: list[dict[str, Any]] = []
        for i, w in enumerate(words):
            if i % 2 == 0:
                # 单词拼写
                r = await _process_one(
                    kind="spelling",
                    template_version=template_spelling,
                    params={
                        "word": w["word"],
                        "meaning": w["meaning"],
                        "hint": make_hint(w["word"]),
                    },
                    interaction_id="text_blank",
                    scorer_params={"answer": {"b1": w["word"]}},
                    target_word=w["word"],
                    db=session,
                    vocab_validator_cls=vocab_validator_cls,
                )
            else:
                # 词汇单选（干扰项与洗牌由词序确定性驱动）
                d1, d2, d3 = pick_distractors(vocab, w, seed=i)
                choice_params = build_choice_params(w, d1, d2, d3, seed=i * 1000 + 7)
                r = await _process_one(
                    kind="choice",
                    template_version=template_choice,
                    params=choice_params,
                    interaction_id="single_choice",
                    scorer_params={"answer": choice_params["answer_letter"]},
                    target_word=w["word"],
                    db=session,
                    vocab_validator_cls=vocab_validator_cls,
                )
            results.append(r)
            print(
                f"[pipeline] {r['kind']:8s} {r['word']:12s} "
                f"-> id={r['item_version_id'][:24]}... "
                f"cert={'✓' if r['cert_id'] else '✗'} verdicts={r['verdicts']}"
            )
        return results

    if db is not None:
        return await _run(db)

    import os
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db_name = os.environ.get("POSTGRES_DB", "muti_w3_packs")
    dsn = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
    engine = create_async_engine(dsn, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await _run(session)
    finally:
        await engine.dispose()


def main() -> None:
    """CLI 入口：运行管线并打印演示输出."""
    print("=" * 72)
    print("W3 S7 英语包 A 线最小实证：词汇单选 + 单词拼写")
    print("=" * 72)

    results = asyncio.run(run_pipeline(n_words=12))

    passed = sum(1 for r in results if r["cert_id"])
    published = sum(
        1 for r in results
        if r["cert_id"]
        and isinstance(r.get("publish_result"), dict)
        and "error" not in r["publish_result"]
    )
    print(f"\n链路完成：{len(results)} 题；签证 {passed}/{len(results)}；"
          f"入库 {published}/{len(results)}")

    print("\n── HTML 渲染示例（第 1 题）──")
    if results:
        print(results[0]["html"])


if __name__ == "__main__":
    main()
