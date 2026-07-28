"""T-W4-012 C 线语篇 schema 与模型单元测试.

覆盖验收标准：
1. Passage 模型含完整字段（id/content_hash/body/genre/kp_refs/difficulty_metrics/
   license/grade_band/subject/created_at + status/gate_certificate_id/published_at）。
2. 命题方向 schema 校验：知识点存在性、难度区间合法性、学段匹配性。
3. 迁移可升降级（migrate-check 验证）；DB CHECK 拒绝非法 genre/grade_band/
   published-without-gate（D2 门强制 DB 兜底）。
4. make accept TASK=T-W4-012 全绿。
5. 不 import 任何学科包/学段包（A5/X6）。

测试隔离：复用 conftest.async_session 的事务回滚（savepoint），不污染测试库。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.core.content.passage_schema import (
    DifficultyTarget,
    PromptDirection,
    direction_to_prompt,
    validate_prompt_direction,
)
from src.core.models.item_version import KpRef
from src.core.models.passage import (
    GENRE_VALUES,
    GRADE_BAND_VALUES,
    DifficultyMetrics,
    Passage,
)


# ────────────────────────────────────────────────────────────────────
# 辅助构造
# ────────────────────────────────────────────────────────────────────


def _kp(code: str = "read.main_idea") -> KpRef:
    return KpRef(dimension="kp", code=code)


def _metrics() -> dict:
    return {
        "avg_sentence_length": 12.5,
        "oov_rate": 0.08,
        "total_chars": 240,
        "total_sentences": 12,
        "char_freq": {"的": 18, "了": 6},
    }


def _passage(**overrides) -> Passage:
    """构造合法 draft Passage 行（供 DB 测试）."""
    base = dict(
        passage_id="pass_test_001",
        content_hash="sha256:abc123",
        body="春天来了，万物复苏。小鸟在枝头唱歌。",
        genre="narrative",
        kp_refs=[{"dimension": "kp", "code": "read.main_idea"}],
        difficulty_metrics=_metrics(),
        license_id=None,
        grade_band="M",
        subject="subject-chinese",
        status="draft",
    )
    base.update(overrides)
    return Passage(**base)


# ────────────────────────────────────────────────────────────────────
# 1. Passage ORM 字段完整性（验收 #1）
# ────────────────────────────────────────────────────────────────────


class TestPassageORMFields:
    """Passage 模型字段覆盖任务卡验收 #1 的全部字段."""

    def test_all_required_fields_present(self):
        """验收#1：Passage 含全部必填字段."""
        cols = {c.name for c in Passage.__table__.columns}
        required = {
            "passage_id",
            "content_hash",
            "body",
            "genre",
            "kp_refs",
            "difficulty_metrics",
            "license_id",
            "grade_band",
            "subject",
            "created_at",
            # 状态机 + 门强制（D2）
            "status",
            "gate_certificate_id",
            "published_at",
        }
        missing = required - cols
        assert not missing, f"Passage 缺字段：{missing}"

    def test_genre_values_non_empty(self):
        """体裁枚举非空，供 schema 校验与门策略引用."""
        assert len(GENRE_VALUES) >= 8, "通用体裁至少覆盖 8 类"

    def test_grade_band_values(self):
        """学段三档 L/M/H."""
        assert set(GRADE_BAND_VALUES) == {"L", "M", "H"}

    def test_difficulty_metrics_model(self):
        """DifficultyMetrics 含字频/句长/生词率（架构 §4.1）."""
        m = DifficultyMetrics(
            avg_sentence_length=10.0,
            oov_rate=0.1,
            total_chars=100,
            total_sentences=10,
        )
        assert m.avg_sentence_length == 10.0
        assert m.oov_rate == 0.1
        assert m.char_freq == {}


# ────────────────────────────────────────────────────────────────────
# 2. 命题方向 schema 校验（验收 #2）
# ────────────────────────────────────────────────────────────────────


class TestPromptDirectionValidation:
    """命题方向三类校验：知识点存在性 / 难度区间 / 学段匹配."""

    def _direction(self, **overrides) -> PromptDirection:
        base = dict(
            kp_refs=[_kp()],
            genre="narrative",
            difficulty_target=DifficultyTarget(min=0.2, max=0.5),
            grade_band="M",
            subject="subject-chinese",
        )
        base.update(overrides)
        return PromptDirection(**base)

    def test_valid_direction_passes(self):
        """合法命题方向：无错误."""
        d = self._direction()
        assert validate_prompt_direction(d) == []

    def test_empty_kp_refs_rejected_by_schema(self):
        """知识点存在性：空 kp_refs 被 Pydantic min_length=1 拒绝."""
        with pytest.raises(ValidationError):
            self._direction(kp_refs=[])

    def test_empty_kp_code_flagged(self):
        """知识点存在性：code 为空被校验函数标记."""
        d = self._direction(kp_refs=[KpRef(dimension="kp", code="")])
        errors = validate_prompt_direction(d)
        assert any("code" in e for e in errors)

    def test_invalid_difficulty_range_rejected(self):
        """难度区间合法性：min>max 被 DifficultyTarget 拒绝."""
        with pytest.raises(ValidationError):
            DifficultyTarget(min=0.6, max=0.4)

    def test_difficulty_out_of_range_rejected(self):
        """难度区间合法性：越界 [0,1] 被拒."""
        with pytest.raises(ValidationError):
            DifficultyTarget(min=-0.1, max=0.5)
        with pytest.raises(ValidationError):
            DifficultyTarget(min=0.2, max=1.5)

    def test_invalid_genre_flagged(self):
        """体裁非法：校验函数标记."""
        d = self._direction(genre="invalid_genre")
        errors = validate_prompt_direction(d)
        assert any("genre" in e for e in errors)

    def test_invalid_grade_band_rejected_by_schema(self):
        """学段匹配性：非法 grade_band 被 Literal 拒绝."""
        with pytest.raises(ValidationError):
            self._direction(grade_band="X")

    def test_low_band_argumentative_flagged(self):
        """学段匹配性：低段(L)不出现议论文（适龄性 §4.3）."""
        d = self._direction(grade_band="L", genre="argumentative")
        errors = validate_prompt_direction(d)
        assert any("低段" in e for e in errors)

    def test_low_band_news_report_flagged(self):
        """学段匹配性：低段(L)不出现新闻报道."""
        d = self._direction(grade_band="L", genre="news_report")
        errors = validate_prompt_direction(d)
        assert any("低段" in e for e in errors)

    def test_mid_band_argumentative_ok(self):
        """学段匹配性：中段(M)议论文合法."""
        d = self._direction(grade_band="M", genre="argumentative")
        assert validate_prompt_direction(d) == []

    def test_word_count_target_validated(self):
        """字数区间：hi<lo 被拒."""
        with pytest.raises(ValidationError):
            self._direction(word_count_target=(200, 100))

    def test_direction_to_prompt_deterministic(self):
        """direction_to_prompt 纯函数：同输入同输出（可复现基础）."""
        d = self._direction()
        assert direction_to_prompt(d) == direction_to_prompt(d)
        assert "narrative" in direction_to_prompt(d)


# ────────────────────────────────────────────────────────────────────
# 3. DB 层：迁移表存在 + CHECK 约束（验收 #3 + D2）
# ────────────────────────────────────────────────────────────────────


class TestPassageDBConstraints:
    """迁移 0018 创建 passage 表 + DB CHECK 兜底门强制（D2）."""

    async def test_passage_table_exists(self, async_session):
        """迁移 0018 已创建 passage 表."""
        rows = (
            await async_session.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='passage'"
                )
            )
        ).first()
        assert rows is not None, "passage 表必须存在（迁移 0018）"

    async def test_insert_draft_passage_ok(self, async_session):
        """合法 draft 语篇插入成功."""
        async_session.add(_passage())
        await async_session.flush()  # 不抛即通过
        assert True

    async def test_invalid_genre_rejected(self, async_session):
        """DB CHECK ck_passage_genre_domain 拒绝非法体裁."""
        async_session.add(_passage(genre="invalid_genre", passage_id="p_genre"))
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_invalid_grade_band_rejected(self, async_session):
        """DB CHECK ck_passage_grade_band_domain 拒绝非法学段."""
        async_session.add(
            _passage(grade_band="X", passage_id="p_grade")
        )
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_published_without_gate_rejected(self, async_session):
        """D2 门强制：published 无 gate_certificate_id 被 DB CHECK 拒绝.

        ck_passage_published_requires_gate: status<>'published' OR
        gate_certificate_id IS NOT NULL。绕过写入服务直写 published 行必失败。
        """
        async_session.add(
            _passage(
                status="published",
                gate_certificate_id=None,
                passage_id="p_pub",
            )
        )
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_published_with_gate_ok(self, async_session):
        """published 持 gate_certificate_id 通过 DB CHECK."""
        async_session.add(
            _passage(
                status="published",
                gate_certificate_id="cert_test_001",
                passage_id="p_pub_ok",
            )
        )
        await async_session.flush()


# ────────────────────────────────────────────────────────────────────
# 4. 学科零特判（A5/X6）
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """核心域不 import 任何学科包/学段包（宪法 A5/X6）."""

    _FORBIDDEN = ("src.packs", "subject_math", "subject_chinese",
                  "subject_english", "gradeband_low")

    def test_passage_model_no_subject_pack(self):
        src = Path(Passage.__module__.replace(".", "/") + ".py")
        # __module__ 是 'src.core.models.passage'，定位源文件
        import src.core.models.passage as mod
        text_src = Path(mod.__file__).read_text(encoding="utf-8")
        for bad in self._FORBIDDEN:
            assert bad not in text_src, (
                f"passage.py 不得引用学科包：发现 {bad!r}"
            )

    def test_passage_schema_no_subject_pack(self):
        import src.core.content.passage_schema as mod
        text_src = Path(mod.__file__).read_text(encoding="utf-8")
        for bad in self._FORBIDDEN:
            assert bad not in text_src, (
                f"passage_schema.py 不得引用学科包：发现 {bad!r}"
            )
