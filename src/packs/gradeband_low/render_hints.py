"""T-W4-035 低学段包渲染提示器.

读取 config.yaml 的低段参数，按 (item, grade_band) 产出渲染提示 dict，
供渲染与交互层（T-W4-037 学段适配层）消费。

设计要点：
- **核心域零特判（A5）**：本模块位于学段包 src/packs/gradeband_low/ 内，
  核心域不 import 本包；调用方（学段感知的编排层）加载本模块产出的 hints
  dict，注入核心适配层 `adapt_for_gradeband(render_ir, grade_band, hints=...)`。
- **低段专属只在 grade_band=='L' 时注入**：中/高段返回空提示（phonetic=False、
  font_size=None、keyboard=None），不注入低段元素（T-W4-037 验收 #3）。
- **数字键盘仅对数值填空类交互触发**，且只允许 0–9
  （config.numeric_keyboard_digits，验收 #3）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_config_cache: Optional[dict[str, Any]] = None

# 触发数字键盘的交互类型（数值填空类；呼应 interaction.yaml 的 numeric_blank）
_NUMERIC_KEYBOARD_INTERACTIONS = frozenset({"numeric_blank", "text_blank_numeric"})


def load_config() -> dict[str, Any]:
    """加载并缓存 config.yaml（低学段参数包）.

    为什么缓存：渲染提示器在每题渲染时被调用，重复读盘代价高；
    config.yaml 在运行期不可变（参数包是版本化静态配置），缓存安全。
    """
    global _config_cache
    if _config_cache is None:
        _config_cache = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    return _config_cache


def reload_config() -> dict[str, Any]:
    """强制重载 config.yaml（测试 / 热更新场景）."""
    global _config_cache
    _config_cache = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    return _config_cache


def _get_interaction_id(item: Any) -> Optional[str]:
    """从 item 取 interaction_id（兼容 dict / pydantic 对象 / None）.

    兼容三种形态：
    - item_version dict：{interaction_ref: {interaction_id: ...}}
    - RenderIR 对象：直接 .interaction_id（T-W2-033）
    - None：单题无交互上下文
    """
    if item is None:
        return None
    if isinstance(item, Mapping):
        ref = item.get("interaction_ref")
        if isinstance(ref, Mapping) and ref.get("interaction_id") is not None:
            return str(ref["interaction_id"])
        iid = item.get("interaction_id")
        return str(iid) if iid is not None else None
    # 对象（pydantic / dataclass）
    ref = getattr(item, "interaction_ref", None)
    if isinstance(ref, Mapping) and ref.get("interaction_id") is not None:
        return str(ref["interaction_id"])
    iid = getattr(item, "interaction_id", None)
    return str(iid) if iid is not None else None


def render_hints(item: Any, grade_band: str) -> dict[str, Any]:
    """返回 (item, grade_band) 的渲染提示 dict.

    低段（L）：注入注音 / 大字号 / 朗读按钮 / 数字键盘；
    中高段（M/H）：返回空提示（不注入低段专属元素）。

    Args:
        item: 题目（dict / 对象 / None）；用于判断是否触发数字键盘。
        grade_band: 学段（L/M/H）。

    Returns:
        渲染提示 dict。低段示例::

            {
              "grade_band": "L",
              "phonetic": true,
              "phonetic_coverage": "full",
              "font_size": "24px",
              "read_aloud": true,
              "keyboard": "numeric",
              "keyboard_allowed": "0123456789"
            }

        非低段返回 ``{"grade_band": <gb>, "phonetic": False,
        "font_size": None, "keyboard": None, "read_aloud": False}``。
    """
    if grade_band != "L":
        # 低段专属不注入到中/高段（T-W4-037 验收 #3）
        return {
            "grade_band": grade_band,
            "phonetic": False,
            "font_size": None,
            "keyboard": None,
            "read_aloud": False,
        }

    cfg = load_config()
    interaction_id = _get_interaction_id(item)

    # 数字键盘：仅数值填空类交互触发，且只允许 0–9
    keyboard: Optional[str] = None
    keyboard_allowed: Optional[str] = None
    if cfg.get("numeric_keyboard") and interaction_id in _NUMERIC_KEYBOARD_INTERACTIONS:
        keyboard = "numeric"
        keyboard_allowed = str(cfg.get("numeric_keyboard_digits", "0123456789"))

    return {
        "grade_band": "L",
        "phonetic": bool(cfg.get("phonetic_switch", False)),
        "phonetic_coverage": cfg.get("phonetic_coverage"),
        # 大字号 ≥20px（验收 #3）；config 中 font_size_large 已 ≥20
        "font_size": f"{int(cfg.get('font_size_large', 24))}px",
        "read_aloud": bool(cfg.get("read_aloud_button", False)),
        "keyboard": keyboard,
        "keyboard_allowed": keyboard_allowed,
    }


def numeric_keyboard_allowed_chars() -> str:
    """数字键盘允许的字符集合（验收 #3：仅 0–9）.

    供交互层做输入过滤白名单（数字键盘仅允许 0–9，禁止字母/符号/小数点）。
    """
    cfg = load_config()
    return str(cfg.get("numeric_keyboard_digits", "0123456789"))


__all__ = [
    "load_config",
    "reload_config",
    "render_hints",
    "numeric_keyboard_allowed_chars",
]
