"""T-W4-024 卷面音频二维码：签名 URL + QR 码生成（架构 v2 §4.6 / §4.8 / S5）.

卷面印二维码，学生扫码播放听力音频。二维码内容是签名 URL——含时效签名
（24h 有效），防盗链（ADR §4.8「试卷与音频资源走签名 URL 防盗链」）。

签名机制：
- message = f"{audio_id}|{paper_id}|{expires_at_ts}"
- sig = HMAC-SHA256(secret, message) 取 hex 前 32 字符
- signed_url = f"{base_url}/{audio_id}.mp3?paper={paper_id}&exp={ts}&sig={sig}"

为什么用 HMAC 而非裸 MD5：HMAC 带密钥，攻击者无法伪造签名（防盗链关键）。
为什么 24h：卷面二维码印发后学生当天使用，24h 足够；过期后扫码返回 410 Gone。

复用 trace_codes.generate_qr_svg 生成 QR 码 SVG（T-W2-037 已落地 qrcode 依赖）。

宪法 A5/X6：不 import 学科包/学段包。
宪法 D7：audio_id 是内容寻址 hash，不含 PII；paper_id 是卷规格 id，不含 PII。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, ConfigDict, Field

from src.core.render.trace_codes import generate_qr_svg


# ════════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════════

# 签名 URL 默认有效期（小时）
DEFAULT_VALIDITY_HOURS: int = 24

# 签名 URL 默认基础地址（与 MockAudioStorageWriter.BASE_URL 一致）
DEFAULT_BASE_URL: str = "http://localhost:9000/audio-listening"

# 签名截取长度（HMAC-SHA256 产出 64 hex 字符，取前 32 足够防伪且 URL 更短）
SIG_HEX_LEN: int = 32


# ════════════════════════════════════════════════════════════════════
# 异常
# ════════════════════════════════════════════════════════════════════


class QRSignatureError(Exception):
    """二维码签名 URL 验证失败（签名不符或已过期）."""


# ════════════════════════════════════════════════════════════════════
# 签名 URL 结果
# ════════════════════════════════════════════════════════════════════


class QRSignedUrl(BaseModel):
    """二维码签名 URL 结果（验收 #2）.

    - audio_id / paper_id：签名绑定的资源标识。
    - signed_url：含签名的音频访问 URL（QR 码内容）。
    - expires_at：过期时间（UTC）。
    - qr_svg：QR 码 SVG 字符串（可嵌入 HTML/PDF）。
    """

    model_config = ConfigDict(extra="forbid")

    audio_id: str
    paper_id: str
    signed_url: str
    expires_at: datetime
    qr_svg: str = Field(description="QR 码 SVG 字符串")


# ════════════════════════════════════════════════════════════════════
# 签名与验签
# ════════════════════════════════════════════════════════════════════


def _compute_signature(
    audio_id: str, paper_id: str, expires_at_ts: int, secret: str
) -> str:
    """计算 HMAC-SHA256 签名.

    message = f"{audio_id}|{paper_id}|{expires_at_ts}"
    sig = HMAC-SHA256(secret, message) hex 前 32 字符

    为什么纳入 expires_at_ts：签名与时效绑定，过期后签名失效（防重放）。
    """
    message = f"{audio_id}|{paper_id}|{expires_at_ts}"
    mac = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    )
    return mac.hexdigest()[:SIG_HEX_LEN]


def _build_signed_url(
    base_url: str,
    audio_id: str,
    paper_id: str,
    expires_at_ts: int,
    sig: str,
) -> str:
    """构造签名 URL.

    格式：{base_url}/{audio_id}.mp3?paper={paper_id}&exp={ts}&sig={sig}
    """
    params = urlencode({"paper": paper_id, "exp": expires_at_ts, "sig": sig})
    return f"{base_url}/{audio_id}.mp3?{params}"


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════


def generate_qr(
    audio_id: str,
    paper_id: str,
    *,
    secret: str,
    base_url: str = DEFAULT_BASE_URL,
    validity_hours: int = DEFAULT_VALIDITY_HOURS,
    now: datetime | None = None,
) -> QRSignedUrl:
    """生成卷面音频二维码（验收 #2）.

    流程：
    1. 计算过期时间（now + validity_hours）。
    2. 用 HMAC-SHA256 签名 (audio_id, paper_id, expires_at_ts)。
    3. 构造签名 URL。
    4. 用 qrcode 库生成 QR 码 SVG（复用 trace_codes.generate_qr_svg）。

    Args:
        audio_id: 音频素材内容寻址 id。
        paper_id: 卷规格 id（二维码绑定到具体卷）。
        secret: HMAC 签名密钥（从环境变量注入，禁止硬编码）。
        base_url: 音频服务基础 URL（默认本地 MinIO mock）。
        validity_hours: 签名有效期（小时，默认 24h）。
        now: 当前时间（None → datetime.now(UTC)，测试可注入固定时间）。

    Returns:
        QRSignedUrl：含 signed_url + expires_at + qr_svg。

    Raises:
        ValueError: audio_id / paper_id / secret 为空。
    """
    if not audio_id:
        raise ValueError("audio_id 不能为空")
    if not paper_id:
        raise ValueError("paper_id 不能为空")
    if not secret:
        raise ValueError("secret 不能为空（HMAC 签名密钥）")

    current = now if now is not None else datetime.now(timezone.utc)
    expires_at = current + timedelta(hours=validity_hours)
    expires_at_ts = int(expires_at.timestamp())

    sig = _compute_signature(audio_id, paper_id, expires_at_ts, secret)
    signed_url = _build_signed_url(base_url, audio_id, paper_id, expires_at_ts, sig)
    qr_svg = generate_qr_svg(signed_url)

    return QRSignedUrl(
        audio_id=audio_id,
        paper_id=paper_id,
        signed_url=signed_url,
        expires_at=expires_at,
        qr_svg=qr_svg,
    )


def verify_qr_url(
    signed_url: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> bool:
    """验证签名 URL 是否有效（签名正确且未过期）.

    Args:
        signed_url: 待验证的签名 URL。
        secret: HMAC 签名密钥。
        now: 当前时间（None → datetime.now(UTC)，测试可注入过期时间）。

    Returns:
        True 有效；False 无效（签名不符或已过期）。
    """
    try:
        parsed = urlparse(signed_url)
        params = parse_qs(parsed.query)
        paper_id = params.get("paper", [None])[0]
        exp_str = params.get("exp", [None])[0]
        sig = params.get("sig", [None])[0]

        if not paper_id or not exp_str or not sig:
            return False

        expires_at_ts = int(exp_str)

        # 从 URL path 提取 audio_id（格式：/{audio_id}.mp3）
        path = parsed.path.rstrip("/")
        if not path or not path.endswith(".mp3"):
            return False
        audio_id = path.rsplit("/", 1)[-1][:-4]  # 去掉 .mp3

        # 验签
        expected_sig = _compute_signature(audio_id, paper_id, expires_at_ts, secret)
        if not hmac.compare_digest(sig, expected_sig):
            return False

        # 验时效
        current = now if now is not None else datetime.now(timezone.utc)
        if current.timestamp() > expires_at_ts:
            return False  # 已过期

        return True
    except (ValueError, IndexError, TypeError):
        return False


__all__ = [
    "DEFAULT_VALIDITY_HOURS",
    "DEFAULT_BASE_URL",
    "QRSignatureError",
    "QRSignedUrl",
    "generate_qr",
    "verify_qr_url",
]
