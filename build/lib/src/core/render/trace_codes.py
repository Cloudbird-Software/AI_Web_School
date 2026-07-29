"""T-W2-037 卷码/QR/题短码生成与校验.

三种码：
1. paper_code：卷码 = ULID + Luhn 校验位。打印在卷面，人类可读，防手抄错。
2. QR payload：仅含 paper_spec_id + 校验位。扫码后端反查 paper 表定位卷。
   - 不含 item_version_id 等实例明文（QR 公开打印，不能泄露题目）
3. item_short_code：题短码 = base32(paper_item_id 前 6 字节) + Luhn 校验位。
   - 短码（8 位）便于打印与学生/家长口述
   - 校验位防口述错
   - 反查 paper_item → item_version → gate_certificate → 签发人

为什么用 Luhn 而非 CRC32：Luhn 是 1 位校验位，专门为人手工输入设计
（数字抄错检测率 ~100% 单错、~90% 互换错）；CRC32 校验力强但码太长不利于人读。
ULID 本身 26 字符 base32，加 1 位 Luhn 校验位 = 27 字符。

设计要点：
- 学科零特判（A5）：本模块是核心域，不 import 学科包
- QR 用 qrcode 库生成 SVG（pyproject.toml 已声明依赖 X8）
- 所有函数纯函数（无副作用），可单元测试与确定性复现
"""
from __future__ import annotations

from typing import Optional


# ════════════════════════════════════════════════════════════════════
# Luhn 校验位（用于 paper_code 与 item_short_code）
# ════════════════════════════════════════════════════════════════════

# Luhn 算法：从右往左，每隔一位乘 2，超过 9 减 9，其余位不变，求和模 10。
# 这里我们用「校验位放在末尾」的变体：先算除校验位外各位的 Luhn 和，
# 校验位 = (10 - sum % 10) % 10，使整体（含校验位）模 10 为 0。
#
# 标准 Luhn 用 0-9 数字。我们的 paper_code 是 ULID（base32 字母+数字），
# 需要把字母也映射到 0-9 才能用 Luhn。映射方式：ord(c) % 10。
# 这不是密码学安全哈希，只是防手抄错的轻量校验。

# base32 字符集（Crockford，剔除 I/L/O/U 避免混淆）
_BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _luhn_checksum(data: str) -> int:
    """计算 data 字符串的 Luhn 校验位（0-9）.

    字符到数字的映射：数字→本身，字母→ord(c) % 10。
    """
    digits: list[int] = []
    for ch in data:
        if ch.isdigit():
            digits.append(int(ch))
        else:
            digits.append(ord(ch) % 10)
    # 从右往左，每隔一位乘 2
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d2 = d * 2
            digits_v = d2 - 9 if d2 > 9 else d2
        else:
            digits_v = d
        total += digits_v
    return (10 - total % 10) % 10


def _luhn_verify(data: str) -> bool:
    """验证 data（末位为 Luhn 校验位）是否通过校验."""
    if len(data) < 2:
        return False
    payload = data[:-1]
    check_digit = data[-1]
    if not check_digit.isdigit():
        return False
    expected = _luhn_checksum(payload)
    return int(check_digit) == expected


# ════════════════════════════════════════════════════════════════════
# 卷码 paper_code
# ════════════════════════════════════════════════════════════════════

def generate_paper_code(ulid: Optional[str] = None) -> str:
    """生成卷码 = ULID + Luhn 校验位.

    Args:
        ulid: 可选的 ULID 字符串（26 字符 base32）。None 则现生成。

    Returns:
        27 字符卷码（26 ULID + 1 校验位数字）

    Raises:
        ValueError: ulid 长度或字符集不符
    """
    if ulid is None:
        import ulid
        ulid = str(ulid.new())
    if len(ulid) != 26:
        raise ValueError(f"ULID 长度必须 26，得到 {len(ulid)}")
    ulid_upper = ulid.upper()
    for ch in ulid_upper:
        if ch not in _BASE32_ALPHABET:
            raise ValueError(f"ULID 含非法字符 {ch!r}（base32 字符集：{_BASE32_ALPHABET}）")
    check = _luhn_checksum(ulid_upper)
    return ulid_upper + str(check)


def verify_paper_code(code: str) -> bool:
    """验证卷码是否通过 Luhn 校验.

    Args:
        code: 待验证的卷码（27 字符）

    Returns:
        True 通过校验；False 不通过（含长度/字符集/校验位不符）
    """
    if len(code) != 27:
        return False
    ulid_part = code[:26]
    for ch in ulid_part:
        if ch not in _BASE32_ALPHABET:
            return False
    return _luhn_verify(code)


# ════════════════════════════════════════════════════════════════════
# QR payload
# ════════════════════════════════════════════════════════════════════

def generate_qr_payload(paper_spec_id: str) -> str:
    """生成 QR payload = paper_spec_id + Luhn 校验位.

    QR 内容设计原则：
    - 只含 paper_spec_id（卷规格稳定 ID）+ 校验位
    - 不含 item_version_id 等实例明文（防题目泄露）
    - 扫码后端用 spec_id 反查 paper 表的 paper_code 字段定位卷

    Args:
        paper_spec_id: 卷规格 id（非空字符串）

    Returns:
        QR payload 字符串（paper_spec_id + 1 位校验位）

    Raises:
        ValueError: paper_spec_id 为空
    """
    if not paper_spec_id:
        raise ValueError("paper_spec_id 不能为空")
    check = _luhn_checksum(paper_spec_id)
    return paper_spec_id + str(check)


def verify_qr_payload(payload: str) -> bool:
    """验证 QR payload 是否通过 Luhn 校验."""
    if len(payload) < 2:
        return False
    return _luhn_verify(payload)


def extract_paper_spec_id(payload: str) -> Optional[str]:
    """从 QR payload 提取 paper_spec_id（去校验位）.

    Returns:
        paper_spec_id（通过校验）或 None（不通过）
    """
    if not verify_qr_payload(payload):
        return None
    return payload[:-1]


# ════════════════════════════════════════════════════════════════════
# 题短码 item_short_code
# ════════════════════════════════════════════════════════════════════

# 题短码 = paper_item_id 的 SHA1 前 30 bit → 6 字符 base32 + 1 Luhn 校验位
# 为什么 30 bit：6 字符 base32 = 30 bit，约 10 亿组合，单卷 100 题远够用；
# 全局唯一靠 paper_item_id 的 SHA1，短码只承担「人读+校验」。
_SHORT_CODE_LEN = 6  # 不含校验位


def _to_base32_crockford(n: int, length: int) -> str:
    """整数 → Crockford base32 定长字符串."""
    if n < 0:
        raise ValueError("n 不能为负")
    chars = []
    for _ in range(length):
        chars.append(_BASE32_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars))


def generate_item_short_code(paper_item_id: str) -> str:
    """生成题短码 = SHA1(paper_item_id) 前 30 bit → 6 字符 base32 + 1 Luhn 校验位.

    Args:
        paper_item_id: paper_item 内部 id（应用层 ULID）

    Returns:
        7 字符短码（6 base32 + 1 数字校验位）

    Raises:
        ValueError: paper_item_id 为空
    """
    if not paper_item_id:
        raise ValueError("paper_item_id 不能为空")
    import hashlib
    digest = hashlib.sha1(paper_item_id.encode("utf-8")).digest()
    # 前 30 bit = 4 字节中的前 30 bit
    n = int.from_bytes(digest[:4], "big") >> 2  # 32 bit → 30 bit
    body = _to_base32_crockford(n, _SHORT_CODE_LEN)
    check = _luhn_checksum(body)
    return body + str(check)


def verify_item_short_code(code: str) -> bool:
    """验证题短码是否通过 Luhn 校验.

    Args:
        code: 7 字符短码

    Returns:
        True 通过；False 不通过
    """
    if len(code) != _SHORT_CODE_LEN + 1:
        return False
    body = code[:_SHORT_CODE_LEN]
    for ch in body:
        if ch not in _BASE32_ALPHABET:
            return False
    return _luhn_verify(code)


# ════════════════════════════════════════════════════════════════════
# QR SVG 生成
# ════════════════════════════════════════════════════════════════════

def generate_qr_svg(payload: str, *, box_size: int = 4, border: int = 1) -> str:
    """生成 QR 码 SVG 字符串.

    用 qrcode 库生成 SVG（pyproject.toml X8 已声明 qrcode 依赖）；
    SVG 适合嵌入 HTML/PDF（位图 PNG 嵌入会模糊）。

    Args:
        payload: QR 内容字符串（一般用 generate_qr_payload 产出）
        box_size: 每个 QR 模块的像素大小
        border: QR 边框模块数（4 是标准最小值）

    Returns:
        完整 <svg>...</svg> 字符串
    """
    import qrcode
    # 显式导入 svg 子模块：qrcode.image.svg 不会随 qrcode 自动加载
    # （PyPI 包 qrcode 把 svg 工厂放在 qrcode.image.svg，需手动 import）
    import qrcode.image.svg
    qr = qrcode.QRCode(
        version=None,  # 自动选最小版本
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    # make_image() 返回 pyqrcode SvgPathImage（svg 路径，体积小，矢量缩放无损）
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    # SvgPathImage 的 to_string() 返回 bytes，解码为 str
    if isinstance(img, bytes):
        return img.decode("utf-8")
    # SvgPathImage 实例：用 _svg 属性或 to_string
    try:
        return img.to_string(encoding="unicode")  # type: ignore[no-any-return]
    except (AttributeError, TypeError):
        # 退化路径：_svg 是 lxml.etree 元素
        from xml.etree import ElementTree as ET
        return ET.tostring(img._svg, encoding="unicode")  # type: ignore[no-any-return]


# ════════════════════════════════════════════════════════════════════
# 回溯映射（短码 → paper_item → item_version → gate_certificate）
# ════════════════════════════════════════════════════════════════════

def build_trace_chain(
    paper_item_row: dict,
    item_version_row: dict,
    gate_certificate_row: Optional[dict] = None,
) -> dict:
    """构造回溯链字典（不查 DB，纯数据组装）.

    给定 paper_item / item_version / gate_certificate 三行数据，
    组装成「短码 → 题版本 → 签发证书 → 签发人」回溯链字典。

    为什么不直接查 DB：本函数保持纯函数特性，DB 查询由调用方负责
    （不同运行时——同步/异步——查法不同）；本函数只做数据形态转换。

    Args:
        paper_item_row: paper_item 表行（含 paper_item_id, item_short_code, item_version_id, paper_id, item_number）
        item_version_row: item_version 表行（含 item_version_id, item_id, gate_certificate_id, status, lineage）
        gate_certificate_row: gate_certificate 表行（含 cert_id, issued_by, issued_at, policy_version），可为 None

    Returns:
        回溯字典：
        {
            "item_short_code": str,
            "paper_item_id": str,
            "paper_id": str,
            "item_number": int,
            "item_version_id": str,
            "item_id": str,
            "gate_certificate_id": Optional[str],
            "issued_by": Optional[str],
            "issued_at": Optional[str],
            "policy_version": Optional[str],
            "lineage": dict,
        }
    """
    return {
        "item_short_code": paper_item_row["item_short_code"],
        "paper_item_id": paper_item_row["paper_item_id"],
        "paper_id": paper_item_row["paper_id"],
        "item_number": paper_item_row["item_number"],
        "item_version_id": item_version_row["item_version_id"],
        "item_id": item_version_row["item_id"],
        "gate_certificate_id": item_version_row.get("gate_certificate_id"),
        "issued_by": gate_certificate_row.get("issued_by") if gate_certificate_row else None,
        "issued_at": gate_certificate_row.get("issued_at") if gate_certificate_row else None,
        "policy_version": gate_certificate_row.get("policy_version") if gate_certificate_row else None,
        "lineage": item_version_row.get("lineage", {}),
    }


__all__ = [
    "generate_paper_code",
    "verify_paper_code",
    "generate_qr_payload",
    "verify_qr_payload",
    "extract_paper_spec_id",
    "generate_item_short_code",
    "verify_item_short_code",
    "generate_qr_svg",
    "build_trace_chain",
]
