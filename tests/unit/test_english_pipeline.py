"""W3 S7 英语包全链路实证测试（E2E-3）.

走通「模板 → 实例化 → 词表等级校验 → 签证 → 入库」：
  run_pipeline(n_words=12, db=async_session) →
  ≥10 例全部签发门证书并以 published 状态落 item_version
  （license 验证器对无 license_id 的 item 走 W3 S9-② 适配路径 pass/skipped）。

测试隔离：async_session 事务回滚（T-W2-019），不污染测试库。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ────────────────────────────────────────────────────────────────────
# importlib 加载管线模块（连字符目录）
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE_PATH = _PROJECT_ROOT / "src" / "packs" / "subject-english" / "english_pipeline.py"


def _load_pipeline():
    mod_name = "subject_english_pipeline_e2e"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PIPELINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_PIPELINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestEnglishPipelineE2E:
    """E2E-3：词汇单选/拼写两题型全链路."""

    @pytest.mark.asyncio
    async def test_pipeline_certifies_and_publishes_12_items(
        self, async_session: AsyncSession
    ) -> None:
        pipeline = _load_pipeline()
        results = await pipeline.run_pipeline(n_words=12, db=async_session)

        # ≥10 例全部签证成功
        assert len(results) == 12
        certified = [r for r in results if r["cert_id"]]
        assert len(certified) >= 10, (
            f"签证成功 {len(certified)}/12："
            f"{[(r['word'], r['verdicts']) for r in results if not r['cert_id']]}"
        )
        # 全部入库（published）：publish_item_version 成功返回
        # {"item_id", "item_version_id"}；失败时为 {"error": ...} 或 None
        for r in results:
            pub = r["publish_result"]
            assert isinstance(pub, dict) and "error" not in pub, (
                f"{r['word']} 入库失败：{pub}"
            )
            assert pub["item_version_id"] == r["item_version_id"]

        # 两题型都有实例
        kinds = {r["kind"] for r in results}
        assert kinds == {"spelling", "choice"}

        # 所有验证器判定：schema/license/word_in_vocab 全 pass
        for r in results:
            assert r["verdicts"]["schema"] == "pass"
            assert r["verdicts"]["license"] == "pass"  # item 无 license_id → 适配路径
            assert r["verdicts"]["word_in_vocab"] == "pass"

        # DB 回读：item_version 以 published 落库且挂门证书
        for r in results:
            row = (
                await async_session.execute(
                    text(
                        "SELECT status, gate_certificate_id, content, lineage"
                        " FROM item_version WHERE item_version_id = :vid"
                    ),
                    {"vid": r["item_version_id"]},
                )
            ).one()
            assert row[0] == "published"
            assert row[1] == r["cert_id"]
            # lineage 记录学科包谱系
            assert row[3]["tier"] == "A"

    @pytest.mark.asyncio
    async def test_out_of_vocab_word_blocked(
        self, async_session: AsyncSession
    ) -> None:
        """词表外词走管线被 word_in_vocab 阻断（不签证、不入库）."""
        pipeline = _load_pipeline()
        template = pipeline.yaml.safe_load(
            (pipeline._PACK_DIR / "templates" / "word_spelling.yaml").read_text(
                encoding="utf-8"
            )
        )
        vocab_validator_cls = pipeline.load_vocab_validator_cls()
        result = await pipeline._process_one(
            kind="spelling",
            template_version=template,
            params={
                "word": "supercalifragilistic",
                "meaning": "超纲词",
                "hint": "s _ _ _",
            },
            interaction_id="text_blank",
            scorer_params={"answer": {"b1": "supercalifragilistic"}},
            target_word="supercalifragilistic",
            db=async_session,
            vocab_validator_cls=vocab_validator_cls,
        )
        assert result["verdicts"]["word_in_vocab"] == "fail"
        assert result["cert_id"] is None
        assert result["publish_result"] is None
        # 未入库
        row = (
            await async_session.execute(
                text(
                    "SELECT 1 FROM item_version WHERE item_version_id = :vid"
                ),
                {"vid": result["item_version_id"]},
            )
        ).first()
        assert row is None
