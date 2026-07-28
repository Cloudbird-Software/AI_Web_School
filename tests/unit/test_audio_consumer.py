"""T-W4-024 听力素材消费层单元测试.

验收对照：
  #1 在线播放器：play(audio_id, session_id) 返回音频 URL；同一 session 第 3 次被拒。
  #2 二维码：generate_qr(audio_id, paper_id) 返回签名 URL + QR SVG，24h 有效。
  #3 点读：point_read(audio_id, word_index) 返回时间戳范围。
  #4 make accept 全绿。
  #5 不 import 学科包/学段包（A5/X6）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.audio.player_service import (
    MAX_PLAYS,
    InMemoryPlayCountStore,
    PlayLimitExceededError,
    PlayResult,
    play,
)
from src.core.audio.point_read import (
    PointReadError,
    PointReadResult,
    list_words,
    point_read,
    split_words,
)
from src.core.audio.qr_generator import (
    DEFAULT_BASE_URL,
    DEFAULT_VALIDITY_HOURS,
    QRSignedUrl,
    generate_qr,
    verify_qr_url,
)


# ════════════════════════════════════════════════════════════════════
# 验收 #1：在线播放器（限次播放）
# ════════════════════════════════════════════════════════════════════


class TestPlayerService:
    """在线播放器限次策略测试."""

    @pytest.fixture
    def store(self) -> InMemoryPlayCountStore:
        """每个测试独立存储（隔离播放计数）."""
        return InMemoryPlayCountStore()

    @pytest.fixture
    def audio_url(self) -> str:
        return f"{DEFAULT_BASE_URL}/test_audio_id.mp3"

    def test_first_play_succeeds(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """第 1 次播放成功，返回 URL + play_count=1."""
        result = play(
            "audio-1", "session-1", audio_url=audio_url, store=store
        )
        assert isinstance(result, PlayResult)
        assert result.audio_id == "audio-1"
        assert result.session_id == "session-1"
        assert result.url == audio_url
        assert result.play_count == 1
        assert result.max_plays == MAX_PLAYS
        assert result.remaining == MAX_PLAYS - 1

    def test_second_play_succeeds(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """第 2 次播放成功，play_count=2."""
        play("audio-1", "session-1", audio_url=audio_url, store=store)
        result = play(
            "audio-1", "session-1", audio_url=audio_url, store=store
        )
        assert result.play_count == 2
        assert result.remaining == 0

    def test_third_play_rejected(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """第 3 次播放被拒（PlayLimitExceededError，API 层转 403）."""
        play("audio-1", "session-1", audio_url=audio_url, store=store)  # 1st
        play("audio-1", "session-1", audio_url=audio_url, store=store)  # 2nd

        with pytest.raises(PlayLimitExceededError) as exc_info:
            play("audio-1", "session-1", audio_url=audio_url, store=store)  # 3rd

        assert exc_info.value.play_count == 3
        assert exc_info.value.max_plays == MAX_PLAYS
        assert exc_info.value.audio_id == "audio-1"
        assert exc_info.value.session_id == "session-1"

    def test_fourth_play_also_rejected(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """第 4 次播放同样被拒（超限后锁定）."""
        play("audio-1", "session-1", audio_url=audio_url, store=store)
        play("audio-1", "session-1", audio_url=audio_url, store=store)
        with pytest.raises(PlayLimitExceededError):
            play("audio-1", "session-1", audio_url=audio_url, store=store)

        with pytest.raises(PlayLimitExceededError):
            play("audio-1", "session-1", audio_url=audio_url, store=store)

    def test_different_session_independent(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """不同 session 的播放计数独立（同一音频不同 session 各自计 2 次）."""
        play("audio-1", "session-A", audio_url=audio_url, store=store)
        play("audio-1", "session-A", audio_url=audio_url, store=store)

        # session-B 独立计数
        result = play(
            "audio-1", "session-B", audio_url=audio_url, store=store
        )
        assert result.play_count == 1

    def test_different_audio_independent(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """不同 audio_id 的播放计数独立."""
        play("audio-1", "session-1", audio_url=audio_url, store=store)
        play("audio-1", "session-1", audio_url=audio_url, store=store)

        result = play(
            "audio-2", "session-1", audio_url=audio_url, store=store
        )
        assert result.play_count == 1

    def test_custom_max_plays(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """可自定义 max_plays（如诊断场景允许 3 次）."""
        play("a", "s", audio_url=audio_url, store=store, max_plays=3)
        play("a", "s", audio_url=audio_url, store=store, max_plays=3)
        result = play("a", "s", audio_url=audio_url, store=store, max_plays=3)
        assert result.play_count == 3
        assert result.remaining == 0

        with pytest.raises(PlayLimitExceededError):
            play("a", "s", audio_url=audio_url, store=store, max_plays=3)

    def test_store_get_count_and_reset(
        self, store: InMemoryPlayCountStore, audio_url: str
    ) -> None:
        """store 支持 get_count / reset（测试隔离用）."""
        assert store.get_count("a", "s") == 0
        play("a", "s", audio_url=audio_url, store=store)
        play("a", "s", audio_url=audio_url, store=store)
        assert store.get_count("a", "s") == 2

        store.reset("a", "s")
        assert store.get_count("a", "s") == 0
        # reset 后可重新播放
        result = play("a", "s", audio_url=audio_url, store=store)
        assert result.play_count == 1


# ════════════════════════════════════════════════════════════════════
# 验收 #2：卷面音频二维码（签名 URL）
# ════════════════════════════════════════════════════════════════════


class TestQRGenerator:
    """二维码签名 URL 生成与验证测试."""

    SECRET = "test-secret-key-for-hmac-signing"

    def test_generate_qr_returns_signed_url(self) -> None:
        """generate_qr 返回含签名 URL + QR SVG."""
        result = generate_qr("audio-1", "paper-1", secret=self.SECRET)
        assert isinstance(result, QRSignedUrl)
        assert result.audio_id == "audio-1"
        assert result.paper_id == "paper-1"
        assert "audio-1.mp3" in result.signed_url
        assert "paper=paper-1" in result.signed_url
        assert "exp=" in result.signed_url
        assert "sig=" in result.signed_url
        # QR SVG 非空
        assert result.qr_svg.startswith("<svg")
        assert "</svg>" in result.qr_svg

    def test_generate_qr_default_24h_validity(self) -> None:
        """默认有效期 24h（expires_at ≈ now + 24h）."""
        before = datetime.now(timezone.utc)
        result = generate_qr("a", "p", secret=self.SECRET)
        after = datetime.now(timezone.utc)

        # expires_at 应在 now+24h ± 1min 内（允许执行耗时）
        expected_min = before + timedelta(hours=24) - timedelta(minutes=1)
        expected_max = after + timedelta(hours=24) + timedelta(minutes=1)
        assert expected_min <= result.expires_at <= expected_max

    def test_generate_qr_custom_validity(self) -> None:
        """可自定义有效期（如 1h）."""
        result = generate_qr(
            "a", "p", secret=self.SECRET, validity_hours=1
        )
        now = datetime.now(timezone.utc)
        # expires_at 应在 now+1h ± 1min 内
        assert abs(
            (result.expires_at - now).total_seconds() - 3600
        ) < 60

    def test_verify_qr_url_valid(self) -> None:
        """有效签名 URL 验证通过."""
        result = generate_qr("audio-1", "paper-1", secret=self.SECRET)
        assert verify_qr_url(result.signed_url, secret=self.SECRET) is True

    def test_verify_qr_url_expired(self) -> None:
        """过期签名 URL 验证失败."""
        # 生成一个已过期的 URL
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        result = generate_qr(
            "a", "p", secret=self.SECRET, validity_hours=24, now=past_time
        )
        # now 是 past_time + 24h = now + 23h... 等等，不对
        # generate_qr 的 expires_at = now + validity_hours = past_time + 24h
        # 但 past_time 是 1h 前，所以 expires_at = (now - 1h) + 24h = now + 23h
        # 这还没过期！需要用更短的有效期
        result = generate_qr(
            "a", "p", secret=self.SECRET, validity_hours=1, now=past_time
        )
        # expires_at = past_time + 1h = (now - 1h) + 1h = now → 已过期
        assert verify_qr_url(result.signed_url, secret=self.SECRET) is False

    def test_verify_qr_url_wrong_secret(self) -> None:
        """错误密钥的签名验证失败."""
        result = generate_qr("a", "p", secret=self.SECRET)
        assert verify_qr_url(result.signed_url, secret="wrong-secret") is False

    def test_verify_qr_url_tampered(self) -> None:
        """篡改过的 URL 验证失败."""
        result = generate_qr("audio-1", "paper-1", secret=self.SECRET)
        # 篡改 audio_id 部分
        tampered = result.signed_url.replace("audio-1", "audio-2")
        assert verify_qr_url(tampered, secret=self.SECRET) is False

    def test_verify_qr_url_malformed(self) -> None:
        """格式错误的 URL 验证失败."""
        assert verify_qr_url("not-a-url", secret=self.SECRET) is False
        assert verify_qr_url(
            "http://localhost/audio.mp3", secret=self.SECRET
        ) is False  # 缺参数
        assert verify_qr_url("", secret=self.SECRET) is False

    def test_generate_qr_empty_args_raise(self) -> None:
        """空参数抛 ValueError."""
        with pytest.raises(ValueError):
            generate_qr("", "p", secret=self.SECRET)
        with pytest.raises(ValueError):
            generate_qr("a", "", secret=self.SECRET)
        with pytest.raises(ValueError):
            generate_qr("a", "p", secret="")

    def test_qr_svg_is_valid_svg(self) -> None:
        """QR SVG 是合法的 <svg>...</svg> 字符串."""
        result = generate_qr("a", "p", secret=self.SECRET)
        svg = result.qr_svg
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
        # SVG 含 path 元素（QR 码的黑白模块用 path 描绘）
        assert "<path" in svg

    def test_same_inputs_produce_same_signature(self) -> None:
        """相同输入 + 相同 now → 相同签名（确定性）."""
        fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        r1 = generate_qr("a", "p", secret=self.SECRET, now=fixed_now)
        r2 = generate_qr("a", "p", secret=self.SECRET, now=fixed_now)
        assert r1.signed_url == r2.signed_url


# ════════════════════════════════════════════════════════════════════
# 验收 #3：点读（逐词播放）
# ════════════════════════════════════════════════════════════════════


class TestPointRead:
    """点读逐词时间戳测试."""

    @pytest.fixture
    def audio_url(self) -> str:
        return f"{DEFAULT_BASE_URL}/test_audio.mp3"

    def test_point_read_chinese_text(
        self, audio_url: str
    ) -> None:
        """中文文本：逐字分词，返回时间戳范围."""
        text = "苹果"  # 2 个字
        duration_ms = 1000
        result = point_read(
            "audio-1", 0, text=text, duration_ms=duration_ms, audio_url=audio_url
        )
        assert isinstance(result, PointReadResult)
        assert result.word == "苹"
        assert result.start_ms == 0
        assert result.end_ms == 500  # 1000 / 2
        assert result.method == "even_split"
        assert result.audio_url == audio_url

        result2 = point_read(
            "audio-1", 1, text=text, duration_ms=duration_ms, audio_url=audio_url
        )
        assert result2.word == "果"
        assert result2.start_ms == 500
        assert result2.end_ms == 1000  # 最后一个词取到末尾

    def test_point_read_english_text(
        self, audio_url: str
    ) -> None:
        """英文文本：按空格分词."""
        text = "Hello World"  # 2 个词
        duration_ms = 2000
        result = point_read(
            "a", 0, text=text, duration_ms=duration_ms, audio_url=audio_url
        )
        assert result.word == "Hello"
        assert result.start_ms == 0
        assert result.end_ms == 1000

        result2 = point_read(
            "a", 1, text=text, duration_ms=duration_ms, audio_url=audio_url
        )
        assert result2.word == "World"
        assert result2.start_ms == 1000
        assert result2.end_ms == 2000

    def test_point_read_mixed_text(
        self, audio_url: str
    ) -> None:
        """混合文本：CJK 逐字 + 英文按空格."""
        text = "苹果 banana"  # 苹, 果, banana = 3 个词
        words = split_words(text)
        assert words == ["苹", "果", "banana"]

        duration_ms = 3000
        for i, expected_word in enumerate(words):
            result = point_read(
                "a", i, text=text, duration_ms=duration_ms, audio_url=audio_url
            )
            assert result.word == expected_word
            assert result.start_ms == i * 1000
            if i < len(words) - 1:
                assert result.end_ms == (i + 1) * 1000
            else:
                assert result.end_ms == 3000  # 最后取到末尾

    def test_point_read_word_timings_from_metadata(
        self, audio_url: str
    ) -> None:
        """tts_metadata 含 word_timings 时用精确时间戳."""
        text = "Hello World"
        word_timings = [
            {"start_ms": 100, "end_ms": 800},
            {"start_ms": 850, "end_ms": 1500},
        ]
        result = point_read(
            "a", 0,
            text=text, duration_ms=1500, audio_url=audio_url,
            tts_metadata={"word_timings": word_timings},
        )
        assert result.method == "word_timings"
        assert result.start_ms == 100
        assert result.end_ms == 800

    def test_point_read_index_out_of_range(
        self, audio_url: str
    ) -> None:
        """word_index 越界 → PointReadError."""
        text = "abc"  # 1 个词
        with pytest.raises(PointReadError, match="越界"):
            point_read("a", 1, text=text, duration_ms=1000, audio_url=audio_url)

        with pytest.raises(PointReadError, match="越界"):
            point_read("a", -1, text=text, duration_ms=1000, audio_url=audio_url)

    def test_point_read_empty_text_raises(
        self, audio_url: str
    ) -> None:
        """空文本 → PointReadError."""
        with pytest.raises(PointReadError, match="text 为空"):
            point_read("a", 0, text="", duration_ms=1000, audio_url=audio_url)

        with pytest.raises(PointReadError, match="text 为空"):
            point_read("a", 0, text="   ", duration_ms=1000, audio_url=audio_url)

    def test_point_read_invalid_duration_raises(
        self, audio_url: str
    ) -> None:
        """duration_ms ≤ 0 → PointReadError."""
        with pytest.raises(PointReadError, match="duration_ms"):
            point_read("a", 0, text="test", duration_ms=0, audio_url=audio_url)

        with pytest.raises(PointReadError, match="duration_ms"):
            point_read("a", 0, text="test", duration_ms=-1, audio_url=audio_url)

    def test_point_read_single_word(self, audio_url: str) -> None:
        """单字文本：start=0, end=duration."""
        result = point_read(
            "a", 0, text="好", duration_ms=500, audio_url=audio_url
        )
        assert result.word == "好"
        assert result.start_ms == 0
        assert result.end_ms == 500

    def test_list_words(self) -> None:
        """list_words 返回分词列表（UI 展示用）."""
        assert list_words("苹果") == ["苹", "果"]
        assert list_words("Hello World") == ["Hello", "World"]
        assert list_words("") == []
        assert list_words("  ") == []

    def test_split_words_punctuation(self) -> None:
        """标点附着在前一个词（非独立 word）."""
        # 「Hello,」→ 逗号附着在 Hello（非 CJK 非空白累积）
        words = split_words("Hello, World!")
        assert words == ["Hello,", "World!"]

        # 中文标点：CJK 逐字，标点是非 CJK 非空白 → 附着行为
        # 「你好！」→ 你, 好, ！（！是非 CJK 非空白，前一个是 CJK 所以 flush）
        words = split_words("你好！")
        assert words == ["你", "好", "！"]


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包（A5/X6）
# ════════════════════════════════════════════════════════════════════


class TestNoSubjectPackImports:
    """消费层模块禁止 import 学科包/学段包（A5/X6）."""

    def test_no_subject_pack_imports_in_audio_consumer(self) -> None:
        """src/core/audio/ 禁止 import 学科包/学段包."""
        adir = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "audio"
        )
        assert adir.is_dir()
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(adir.rglob("*.py")):
            content = py_file.read_text(encoding="utf-8")
            if pattern.findall(content):
                violations.append(str(py_file.relative_to(adir)))
        assert not violations, (
            f"src/core/audio 存在学科包 import（违反 A5）：{violations}"
        )
