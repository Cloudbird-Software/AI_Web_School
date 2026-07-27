"""T-W2-035 HTML → PDF 导出（无头 Chromium 后端）.

两种后端可配置（pdf_backend: edge|playwright）：
- edge：Windows 本机用系统自带 Edge headless（无额外依赖，开发/测试首选）
- playwright：CI 或无 Edge 环境用 playwright chromium（延迟导入，本机缺 DLL 时跳过）

为什么不固定单一后端：
- Edge 在 Windows 开发机自带，零依赖零下载，但 CI（Linux）无 Edge
- playwright 跨平台但需额外 `playwright install chromium`（~150MB）
- 双后端 + 配置切换让本机与 CI 各取所需

调用方约定：
- 输入 HTML 是完整页面（含品牌模板 default.css + page.html 渲染产物）
- 输出 PDF 路径由调用方指定，导出器只负责生成与校验非空
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional

from src.core.render.html_renderer import get_template
from src.core.render.ir import RenderIR, TextBlock
from src.core.render.item_to_ir import item_to_ir


# ── Edge 可执行文件探测 ─────────────────────────────────────────────

# Windows Edge 常见安装路径（按优先级）
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_edge() -> Optional[str]:
    """探测系统 Edge 可执行文件路径.

    优先级：环境变量 EDGE_PATH > 候选路径 > PATH 中的 msedge。
    返回 None 表示未找到（调用方应回退到 playwright 后端）。
    """
    env_path = os.environ.get("EDGE_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    for cand in _EDGE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    # 尝试 PATH 中查找
    found = shutil.which("msedge") or shutil.which("microsoft-edge")
    return found


# ════════════════════════════════════════════════════════════════════
# PDF 导出器
# ════════════════════════════════════════════════════════════════════

PdfBackend = Literal["edge", "playwright"]


class PdfExporter:
    """HTML → PDF 导出器（支持 edge / playwright 双后端）.

    使用：
        exporter = PdfExporter(backend="edge")
        exporter.export(html_string, Path("out.pdf"))

    为什么把 backend 做成实例属性而非全局配置：
    - 周更批处理可能并发导出多卷，每卷独立 exporter 避免状态污染
    - 测试可注入不同后端，不依赖全局 mock
    """

    def __init__(
        self,
        backend: PdfBackend = "edge",
        *,
        edge_path: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        """初始化导出器.

        参数:
            backend: edge（本机）或 playwright（CI）
            edge_path: Edge 可执行文件路径（None 则自动探测）
            timeout: 导出超时秒数（Edge headless 启动+渲染）
        """
        if backend not in ("edge", "playwright"):
            raise ValueError(f"未知 PDF 后端: {backend!r}（支持 edge|playwright）")
        self.backend = backend
        self.edge_path = edge_path
        self.timeout = timeout

    # ── Edge 后端 ───────────────────────────────────────────────────

    def _resolve_edge_path(self) -> str:
        """解析 Edge 路径：构造时传入 > 自动探测 > 报错."""
        if self.edge_path:
            return self.edge_path
        found = _find_edge()
        if not found:
            raise FileNotFoundError(
                "未找到 Edge 可执行文件：设置 EDGE_PATH 环境变量，"
                "或改用 PdfExporter(backend='playwright')"
            )
        return found

    def _export_with_edge(self, html: str, output_path: Path) -> Path:
        """用 Edge headless 模式导出 PDF.

        命令：msedge --headless --disable-gpu --print-to-pdf=out.pdf file:///abs/path.html
        为什么先写临时 HTML 文件：Edge 的 --print-to-pdf 需要文件 URL，
        直接传 HTML 字符串不可行；临时文件用完即删。
        """
        edge = self._resolve_edge_path()
        # 写临时 HTML 文件
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            html_file = Path(f.name)
        try:
            # file URL 必须用绝对路径 + 正斜杠
            file_url = html_file.resolve().as_uri()
            # 关键参数说明（经实测，缺一可能卡死或超时）：
            # - --headless=new：新版无头模式（旧版 --headless 已废弃）
            # - --virtual-time-budget=5000：虚拟时间预算 5 秒，让 Edge 用虚拟时钟
            #   跑完所有定时器/动画后自动退出，否则会一直挂着等待真实时间流逝
            # - --disable-extensions / --no-first-run / --no-default-browser-check：
            #   避免任何弹窗/首次运行向导阻塞退出
            # - --run-all-compositor-stages-before-draw：确保渲染管线完整跑完再输出
            cmd = [
                edge,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=5000",
                f"--print-to-pdf={output_path.resolve()}",
                file_url,
            ]
            result = subprocess.run(
                cmd,
                timeout=self.timeout,
                capture_output=True,
                check=False,
            )
            # Edge headless 即使成功也可能返回非 0；以输出文件存在+非空为准
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError(
                    f"Edge PDF 导出失败（exit={result.returncode}）: "
                    f"{result.stderr.decode('utf-8', errors='replace')[:500]}"
                )
            return output_path
        finally:
            try:
                html_file.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Playwright 后端 ─────────────────────────────────────────────

    def _export_with_playwright(self, html: str, output_path: Path) -> Path:
        """用 playwright chromium 导出 PDF.

        延迟导入 playwright：本机可能未安装或缺 DLL，避免模块加载即崩溃。
        CI 环境执行 `playwright install chromium` 后可用。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright 未安装：pip install playwright && playwright install chromium"
            ) from exc

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="domcontentloaded")
                page.pdf(path=str(output_path), format="A4")
            finally:
                browser.close()
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("playwright PDF 导出失败：输出文件为空")
        return output_path

    # ── 公共入口 ────────────────────────────────────────────────────

    def export(self, html: str, output_path: Path) -> Path:
        """导出 HTML 为 PDF 文件.

        参数:
            html: 完整 HTML 页面字符串（含 <html><head><body>）
            output_path: PDF 输出路径（父目录需存在）

        返回:
            output_path（导出成功后）

        异常:
            FileNotFoundError: edge 后端未找到 Edge
            RuntimeError: 导出失败（后端报错或输出空文件）
            subprocess.TimeoutExpired: 导出超时
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.backend == "edge":
            return self._export_with_edge(html, output_path)
        return self._export_with_playwright(html, output_path)


# ── 便利函数 ────────────────────────────────────────────────────────

def export_pdf(
    html: str,
    output_path: Path,
    *,
    backend: PdfBackend = "edge",
    edge_path: Optional[str] = None,
) -> Path:
    """便利函数：一次性导出 PDF（无复用场景）."""
    return PdfExporter(backend=backend, edge_path=edge_path).export(html, output_path)


__all__ = ["PdfExporter", "export_pdf", "PdfBackend"]
