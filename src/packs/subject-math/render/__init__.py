"""T-W2-029 学科包渲染组件（数学：数轴/方格 SVG）.

学科零特判（A5）：本包是学科包，可被核心域 render 模块经注册表挂载，
但本包不得反向 import src.core.* —— SVG 产出原样嵌入 RenderIR.math_svg 块。

目录名 subject-math 含连字符（与 ADR §5.1 契约一致），无法作为 Python 包名，
故用 importlib 加载同目录下的 number_line.py / grid.py（与 variable-types/
functions.py 同模式）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_RENDER_DIR = Path(__file__).parent


def _load_module(path: Path, name: str) -> Any:
    """以 importlib 加载 .py 文件为独立模块并注册到 sys.modules.

    Why: 目录 subject-math 含连字符，无法作为 Python 包导入；
         用 importlib.util.spec_from_file_location 绕过限制。
         注册到 sys.modules 保证后续加载复用同一模块实例。
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # 必须在 exec_module 之前
    spec.loader.exec_module(mod)
    return mod


# 加载两个组件模块，注册为稳定模块名（测试可 importlib 复用）
_NUMBER_LINE_MOD = _load_module(_RENDER_DIR / "number_line.py", "subject_math_render_number_line")
_GRID_MOD = _load_module(_RENDER_DIR / "grid.py", "subject_math_render_grid")

NumberLineSpec = _NUMBER_LINE_MOD.NumberLineSpec
render_number_line = _NUMBER_LINE_MOD.render_number_line
GridSpec = _GRID_MOD.GridSpec
render_grid = _GRID_MOD.render_grid

__all__ = [
    "NumberLineSpec",
    "render_number_line",
    "GridSpec",
    "render_grid",
]
