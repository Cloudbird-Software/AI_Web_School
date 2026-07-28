"""T-W4-008 PII 剥离中间件（宪法 D7）.

LLM/TTS 调用前对 prompt/文本做 PII 剥离：
- 学生姓名 → 「学生A」「学生B」…（按出现顺序编号）
- 电话号码 → [PHONE]
- 身份证号 → [ID_CARD]
- 邮箱 → [EMAIL]
- 地址 → [ADDRESS]

保留：
- student_alias_id（ULID/UUID 格式，非 PII，是总线合法身份）

为什么用启发式正则而非 NER 模型：
- PII 剥离在总线热路径，NER 模型引入 AI 调用（自举问题：剥离 PII 的 AI 调用本身
  可能泄漏 PII）；
- 启发式正则确定性、可审计、零外部依赖；
- 误判由 raw_meta.pii_warning 标记，下游可观测。

宪法 A5：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

import re

# PII 类型常量（stripped 返回值用）
NAME = "name"
PHONE = "phone"
ID_CARD = "id_card"
EMAIL = "email"
ADDRESS = "address"

# 中文手机号：1[3-9]xxxxxxxxx（11 位）
_PHONE_RE = re.compile(r"1[3-9]\d{9}")

# 身份证号：18 位（前 17 位数字，末位数字或 X/x）
_ID_CARD_RE = re.compile(r"\d{17}[\dXx]")

# 邮箱：标准 RFC 5322 简化子集
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# 地址：连续汉字 + 省/市/区/县/镇/乡/村/路/街/号/弄/室/栋/单元 关键字
# 至少 2 个汉字前缀 + 1 个行政区划/门牌关键字，避免误伤短词
_ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,}(?:省|市|区|县|镇|乡|村|路|街|号|弄|室|栋|单元)"
)

# 学生姓名：上下文关键字 + 2-4 字汉字姓名
# 上下文：学生/同学/家长/我叫/姓名：/学生姓名 等，避免误伤普通名词
# 仅在明确指人上下文中替换，降低误判率
# 非贪婪 {2,4}?：优先匹配 2 字姓名，避免贪婪吃掉后续关键字（如"家长"的"家"）
_NAME_CONTEXT_RE = re.compile(
    r"(学生|同学|家长|我叫|姓名[：:]?\s*)([\u4e00-\u9fa5]{2,4}?)"
)

# student_alias_id 格式（ULID 26 位 / UUID 36 位）——刻意不剥离，D7 允许
# 本常量仅用于文档与测试断言，不参与剥离逻辑


def strip(text: str) -> tuple[str, list[str]]:
    """剥离文本中的 PII，返回 (剥离后文本, 剥离的 PII 类型列表).

    Args:
        text: 待剥离的 prompt/文本（可能含 PII）。

    Returns:
        (sanitized_text, stripped_kinds)：剥离后文本 + 剥离的 PII 类型列表
        （如 ["name", "phone"]；无 PII 时为空列表）。

    Notes:
        - 剥离顺序：id_card（18 位）→ phone（11 位）→ email → address → name。
          id_card 先于 phone：18 位身份证号中可能含 1[3-9]\\d{9} 子串（如出生年份
          1990 段），先剥离长格式避免身份证被部分误识别为手机号。
        - student_alias_id（ULID/UUID）不在剥离范围，保留原样（D7：alias 非直标识）。
        - 同一文本中多个姓名按出现顺序编号为学生A/学生B/…，保持指代一致。
    """
    if not text:
        return text, []

    stripped: list[str] = []
    sanitized = text

    # 1. 身份证号 → [ID_CARD]（先于 phone，避免长数字串被部分误识别）
    if _ID_CARD_RE.search(sanitized):
        sanitized = _ID_CARD_RE.sub("[ID_CARD]", sanitized)
        stripped.append(ID_CARD)

    # 2. 电话 → [PHONE]
    if _PHONE_RE.search(sanitized):
        sanitized = _PHONE_RE.sub("[PHONE]", sanitized)
        stripped.append(PHONE)

    # 3. 邮箱 → [EMAIL]
    if _EMAIL_RE.search(sanitized):
        sanitized = _EMAIL_RE.sub("[EMAIL]", sanitized)
        stripped.append(EMAIL)

    # 4. 地址 → [ADDRESS]
    if _ADDRESS_RE.search(sanitized):
        sanitized = _ADDRESS_RE.sub("[ADDRESS]", sanitized)
        stripped.append(ADDRESS)

    # 5. 姓名 → 学生A/学生B/…（按出现顺序编号，统一为「学生X」指代）
    # 验收 #2：学生姓名替换为「学生A」——不保留原上下文关键字（学生/同学/家长），
    # 统一替换为「学生X」，避免指代歧义
    name_counter = {"i": 0}

    def _name_sub(match: re.Match[str]) -> str:
        name_counter["i"] += 1
        # 65='A'，按出现顺序编号为学生A/学生B/…
        return f"学生{chr(64 + name_counter['i'])}"

    if _NAME_CONTEXT_RE.search(sanitized):
        sanitized = _NAME_CONTEXT_RE.sub(_name_sub, sanitized)
        stripped.append(NAME)

    return sanitized, stripped
