"""看拼音写词语全链路管线（T-W2-032）.

端到端链路：
  字词库 YAML → 选词 → pypinyin 算拼音 → T-W2-004 引擎实例化
  → T-W2-009 通用验证器 + 语文字规范校验（char_in_corpus）
  → issue_certificate 签发门证书 → W1 publish_item_version 入库
  → 渲染 HTML 片段

运行方式（在 worktree 根目录）：
  python -m src.packs.subject-chinese.pinyin_to_word_pipeline
  或
  python src/packs/subject-chinese/pinyin_to_word_pipeline.py

宪法 X6：本脚本属于学科包，可 import 核心域；核心域不 import 本脚本。
宪法 D4：交互与评分器只复用注册表（text_blank + exact_match）。
"""
from __future__ import annotations

import asyncio
import importlib.util
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

# 现在可以 import 核心域模块
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from src.core.content.writer import publish_item_version  # noqa: E402
from src.core.gate.certifier.service import issue_certificate  # noqa: E402
from src.core.gate.validators.generic import (  # noqa: E402
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)
from src.core.gate.validator import GateContext, ValidatorResult  # noqa: E402
from src.core.instantiation.engine import (  # noqa: E402
    ENGINE_DIGEST,
    instantiate,
)
from src.core.content.source_registry import SourceRegistry  # noqa: E402

# ────────────────────────────────────────────────────────────────────
# 路径常量
# ────────────────────────────────────────────────────────────────────
_PACK_DIR = Path(__file__).resolve().parent
_TEMPLATE_PATH = _PACK_DIR / "templates" / "pinyin_to_word.yaml"
_CORPUS_PATH = _PACK_DIR / "corpora" / "character_word.yaml"
_VALIDATOR_PATH = _PACK_DIR / "validators" / "char_in_corpus.py"

# 学科包摘要（与 W2a-integrate 的 pack_digest 约定一致：sha256:pack-<name>）
_PACK_DIGEST = "sha256:pack-subject-chinese-v1"
# 字库语料摘要（简化：用文件名 + version 标识，生产线由 corpus_version 表承载）
_CORPUS_DIGEST = "sha256:corpus-character-word-v1"


# ────────────────────────────────────────────────────────────────────
# importlib 加载语文学科验证器（目录名含连字符，无法用普通 import）
# ────────────────────────────────────────────────────────────────────


def _load_char_in_corpus_validator():
    """以 importlib 加载 char_in_corpus.py，触发 register_validator.

    Returns:
        CharInCorpusValidator 类。
    """
    spec = importlib.util.spec_from_file_location(
        "subject_chinese_char_in_corpus", _VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_VALIDATOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CharInCorpusValidator


# ────────────────────────────────────────────────────────────────────
# 选词 + 拼音计算
# ────────────────────────────────────────────────────────────────────


def _pick_words(corpus: dict, n: int = 12) -> list[dict]:
    """从字词库选 n 个词（优先低年级 gradeband=L 的 2-3 字词）.

    Returns:
        选中的词记录列表（含 word/pinyin/gradeband）。
    """
    words = corpus.get("words", [])
    # 优先选 gradeband=L 的词；不足时从全部词补
    low = [w for w in words if w.get("gradeband") == "L"]
    selected = low[:n]
    if len(selected) < n:
        for w in words:
            if w not in selected:
                selected.append(w)
                if len(selected) >= n:
                    break
    return selected[:n]


def _compute_pinyin(word: str) -> str:
    """用 pypinyin 计算词语拼音（带声调，空格分隔）.

    与 pinyin_pipeline.get_pinyin 一致，但内联以避免 importlib 加载。
    多音字按 pypinyin 默认词库处理。
    """
    from pypinyin import Style, pinyin

    parts = pinyin(word, style=Style.TONE, errors="default")
    return " ".join(p[0] for p in parts)


# ────────────────────────────────────────────────────────────────────
# 渲染 HTML 片段
# ────────────────────────────────────────────────────────────────────


def render_html(item_version: dict) -> str:
    """把实例化产物渲染为简单 HTML 片段：拼音 + 下划线答题位.

    Args:
        item_version: instantiate() 返回的 dict（含 content.blocks）。

    Returns:
        HTML 字符串。
    """
    blocks = item_version.get("content", {}).get("blocks", [])
    # blocks[0] = "看拼音，写词语："，blocks[1] = pinyin，blocks[2] = 田字格占位
    pinyin_text = ""
    for b in blocks:
        if b.get("kind") == "text":
            rendered = b.get("rendered", "")
            # 第二个 text block 是拼音（避免硬编码索引）
            if rendered and not rendered.startswith("看拼音") and not rendered.startswith("田字格"):
                pinyin_text = rendered
                break

    word = item_version.get("lineage", {}).get("params", {}).get("normalized", {}).get("word", "")
    blank_width = max(len(word) * 2, 6)  # 每字 2 个全角下划线，最少 6

    return (
        f'<div class="pinyin-to-word">\n'
        f'  <p class="instruction">看拼音，写词语：</p>\n'
        f'  <p class="pinyin">{pinyin_text}</p>\n'
        f'  <p class="blank">{"＿" * blank_width}</p>\n'
        f'</div>'
    )


# ────────────────────────────────────────────────────────────────────
# 单题全链路：实例化 → 验证 → 签证 → 入库 → 渲染
# ────────────────────────────────────────────────────────────────────


async def _process_one_word(
    word_rec: dict,
    template_version: dict,
    pack_digest: str,
    corpus_digests: list[str],
    license_id: str,
    db,
    char_validator_cls,
) -> dict:
    """处理一个词：实例化 → 验证 → 签证 → 入库 → 渲染.

    Returns:
        结果摘要 dict：{word, pinyin, item_version_id, cert_id, verdicts, html}。
    """
    word = word_rec["word"]
    pinyin = _compute_pinyin(word)

    # ── 1. 实例化 ──
    result = instantiate(
        template_version,
        params={"word": word, "pinyin": pinyin},
        pack_digest=pack_digest,
        interaction_id="text_blank",
        scorer_id="exact_match",
        scorer_params={"answer": {"b1": word}},
        locale="zh-CN",
        corpus_digests=corpus_digests,
        seed=0,
        signed_by="w2b-chinese-pipeline",
        signed_at="2026-07-27T00:00:00+00:00",
    )
    item_version_dict = result.model_dump()
    item_version_id = item_version_dict["item_version_id"]

    # ── 2. 验证器链（手动顺序调用：schema → license → char_in_corpus → duplicate）──
    artifact_payload = {
        "objective": item_version_dict["objective"],
        "interaction_ref": item_version_dict["interaction_ref"],
        "content": item_version_dict["content"],
        "scoring_ref": item_version_dict["scoring_ref"],
        "lineage": item_version_dict["lineage"],
        "license_id": license_id,
    }

    # 2a. SchemaValidator（校验 ItemVersion 结构必填键）
    schema_ctx = GateContext(
        artifact_type="item",
        pack_id="subject-chinese",
        artifact_payload=artifact_payload,
        required_keys=[
            "objective", "interaction_ref", "content",
            "scoring_ref", "lineage", "license_id",
        ],
    )
    schema_result = await SchemaValidator().validate(item_version_id, schema_ctx)

    # 2b. LicenseValidator（校验 license_id 合法且 approved）
    license_ctx = GateContext(
        artifact_type="item",
        pack_id="subject-chinese",
        artifact_payload=artifact_payload,
        license_id=license_id,
        db=db,
    )
    license_result = await LicenseValidator().validate(item_version_id, license_ctx)

    # 2c. CharInCorpusValidator（语文字规范：字必须在字库内）
    char_ctx = GateContext(
        artifact_type="item",
        pack_id="subject-chinese",
        artifact_payload=artifact_payload,
        target_word=word,
    )
    char_result = await char_validator_cls().validate(item_version_id, char_ctx)

    # 2d. DuplicatePlaceholderValidator（查重占位）
    dup_ctx = GateContext(
        artifact_type="item",
        pack_id="subject-chinese",
        artifact_payload=artifact_payload,
        db=db,
    )
    dup_result = await DuplicatePlaceholderValidator().validate(item_version_id, dup_ctx)

    runs: list[tuple[ValidatorResult, bool]] = [
        (schema_result, True),
        (license_result, True),
        (char_result, True),
        (dup_result, False),  # 查重不阻断
    ]

    verdicts = {
        "schema": schema_result.verdict,
        "license": license_result.verdict,
        "char_in_corpus": char_result.verdict,
        "duplicate": dup_result.verdict,
    }

    # ── 3. 签发门证书（全部阻断项 pass 时）──
    cert_id: str | None = None
    try:
        cert_id = await issue_certificate(
            artifact_ref=item_version_id,
            cert_type="publish",
            policy_version="w2b-chinese-v1",
            issued_by="w2b-chinese-pipeline",
            runs=runs,
            db=db,
        )
    except Exception as e:
        # 签证失败（某阻断项 fail/review）：记录但不中断后续词
        cert_id = None
        verdicts["cert_error"] = str(e)

    # ── 4. 入库（publish_item_version，仅当签证成功）──
    publish_result = None
    if cert_id:
        version_data = {
            "pack_id": "subject-chinese",
            "tier": "A",
            "status": "published",
            "template_version_id": template_version["template_version_id"],
            "template_version_digest": template_version["template_version_id"],
            "pack_digest": pack_digest,
            "engine_digest": ENGINE_DIGEST,
            "corpus_digests": corpus_digests,
            "normalized_params": item_version_dict["lineage"]["params"]["normalized"],
            "locale": "zh-CN",
            "objective": item_version_dict["objective"],
            "interaction_ref": item_version_dict["interaction_ref"],
            "content": item_version_dict["content"],
            "scoring_ref": item_version_dict["scoring_ref"],
            "error_bindings": item_version_dict["error_bindings"],
            "lineage": item_version_dict["lineage"],
            "rendered_snapshot": {"html": render_html(item_version_dict)},
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

    # ── 5. 渲染 HTML ──
    html = render_html(item_version_dict)

    return {
        "word": word,
        "pinyin": pinyin,
        "item_version_id": item_version_id,
        "cert_id": cert_id,
        "verdicts": verdicts,
        "publish_result": publish_result,
        "html": html,
    }


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────


async def run_pipeline(n_words: int = 12) -> list[dict]:
    """运行全链路管线.

    Args:
        n_words: 选词数量（≥10）。

    Returns:
        每个词的处理结果列表。
    """
    # 加载模板
    with _TEMPLATE_PATH.open(encoding="utf-8") as f:
        template_version = yaml.safe_load(f)

    # 加载字词库
    with _CORPUS_PATH.open(encoding="utf-8") as f:
        corpus = yaml.safe_load(f)

    # 加载语文学科验证器
    char_validator_cls = _load_char_in_corpus_validator()

    # 检查 license_id 是否在来源登记表中 approved
    reg = SourceRegistry.from_yaml()
    license_id = corpus["license_id"]
    if not reg.is_approved(license_id):
        raise RuntimeError(f"字库 license_id={license_id} 未 approved")

    # 选词
    words = _pick_words(corpus, n=n_words)
    print(f"[pipeline] 选中 {len(words)} 个词：{[w['word'] for w in words]}")

    # 连接 DB
    import os
    user = os.environ.get("POSTGRES_USER", "muti")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db_name = os.environ.get("POSTGRES_DB", "muti_w2b_chinese")
    dsn = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
    engine = create_async_engine(dsn, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    results: list[dict] = []
    try:
        async with factory() as session:
            for w in words:
                r = await _process_one_word(
                    word_rec=w,
                    template_version=template_version,
                    pack_digest=_PACK_DIGEST,
                    corpus_digests=[_CORPUS_DIGEST],
                    license_id=license_id,
                    db=session,
                    char_validator_cls=char_validator_cls,
                )
                results.append(r)
                print(
                    f"[pipeline] {r['word']} ({r['pinyin']}) "
                    f"-> id={r['item_version_id'][:24]}... "
                    f"cert={'✓' if r['cert_id'] else '✗'} "
                    f"verdicts={r['verdicts']}"
                )
    finally:
        await engine.dispose()

    return results


def main() -> None:
    """CLI 入口：运行管线并打印演示输出."""
    print("=" * 72)
    print("T-W2-032 看拼音写词语全链路实证")
    print("=" * 72)

    results = asyncio.run(run_pipeline(n_words=12))

    print("\n" + "=" * 72)
    print(f"链路完成：{len(results)} 个词")
    print("=" * 72)

    passed = sum(1 for r in results if r["cert_id"])
    print(f"签证成功：{passed}/{len(results)}")

    print("\n── HTML 渲染示例（第 1 个）──")
    if results:
        print(results[0]["html"])

    print("\n── 全部结果摘要 ──")
    for r in results:
        status = "✓ published" if r["cert_id"] and not isinstance(r.get("publish_result"), dict) else "✗"
        if isinstance(r.get("publish_result"), dict) and "error" in r.get("publish_result", {}):
            status = f"✗ publish_err"
        print(f"  {r['word']:8s} {r['pinyin']:20s} {status}")


if __name__ == "__main__":
    main()
