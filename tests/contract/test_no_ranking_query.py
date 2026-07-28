"""T-W4-033 契约测试：跨用户排名查询路径不存在（宪法 D8）.

落地 specs/constitution.md#D8「代码层不得提供跨用户成绩排名的查询路径；
对外呈现一律等级化」与 ADR §4.8.

验证维度：
1. API 层不暴露任何排名/排行榜端点——尝试调用常见排名路径均返回 404。
2. OpenAPI 规范中不含排名相关路径。
3. 静态扫描脚本对 src/ 全仓扫描无违规（E2E-6 合规承载）。

为什么用 TestClient 而非真实 HTTP：
- 契约测试关注「路由是否存在」而非「网络可达」，TestClient 直接在 ASGI 层
  检查路由表，不依赖 uvicorn 进程，确定性高、速度快。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app

# ────────────────────────────────────────────────────────────────────
# 静态扫描器加载（scripts/ci/ 非包成员，用 importlib 加载）
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCANNER_PATH = _PROJECT_ROOT / "scripts" / "ci" / "check_no_ranking.py"


def _load_scanner() -> object:
    """importlib 加载 check_no_ranking.py 为模块（scripts/ 非包）."""
    spec = importlib.util.spec_from_file_location(
        "_check_no_ranking", _SCANNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_check_no_ranking"] = mod
    spec.loader.exec_module(mod)
    return mod


# ────────────────────────────────────────────────────────────────────
# 1. API 层无排名端点
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _api_client() -> TestClient:
    """构造 TestClient（不连 DB——404 在路由匹配阶段即返回）."""
    return TestClient(create_app())


class TestNoRankingEndpoint:
    """尝试调用排名端点，断言返回 404（路由不存在）."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/rankings"),
            ("GET", "/ranking"),
            ("GET", "/rank"),
            ("GET", "/leaderboard"),
            ("GET", "/leaderboards"),
            ("GET", "/students/ranking"),
            ("GET", "/students/rank"),
            ("GET", "/students/leaderboard"),
            ("GET", "/api/v1/rankings"),
            ("GET", "/api/v1/leaderboard"),
            ("GET", "/reports/ranking"),
            ("GET", "/reports/leaderboard"),
            ("GET", "/class/ranking"),
            ("GET", "/class/leaderboard"),
            ("GET", "/grade/ranking"),
            ("POST", "/rankings"),
            ("POST", "/leaderboard"),
        ],
    )
    def test_ranking_endpoint_not_found(
        self, _api_client: TestClient, method: str, path: str
    ) -> None:
        """排名端点必须不存在（404 = 路由未注册）."""
        resp = _api_client.request(method, path)
        assert resp.status_code == 404, (
            f"{method} {path} 不应是已注册端点（D8 禁排名），"
            f"但返回 {resp.status_code}"
        )


# ────────────────────────────────────────────────────────────────────
# 2. OpenAPI 规范无排名路径
# ────────────────────────────────────────────────────────────────────

class TestOpenApiNoRankingPath:
    """OpenAPI 规范中不得包含排名相关路径."""

    _RANK_KEYWORDS = ("rank", "ranking", "leaderboard")

    def test_openapi_paths_no_ranking(self) -> None:
        """OpenAPI 所有路径不含排名关键词."""
        app = create_app()
        schema = app.openapi()
        paths = list(schema.get("paths", {}).keys())
        for path in paths:
            for kw in self._RANK_KEYWORDS:
                assert kw not in path.lower(), (
                    f"OpenAPI 路径 {path!r} 含排名关键词 {kw!r}（违反 D8）"
                )


# ────────────────────────────────────────────────────────────────────
# 3. 全仓静态扫描无违规
# ────────────────────────────────────────────────────────────────────

class TestStaticScanNoRanking:
    """scripts/ci/check_no_ranking.py 扫描 src/ 无违规（E2E-6 承载）."""

    def test_src_has_no_ranking_query(self) -> None:
        """静态扫描 src/ 全部代码，确认无跨用户排名查询路径."""
        scanner_mod = _load_scanner()
        src_dir = _PROJECT_ROOT / "src"
        violations = scanner_mod.scan_directory(src_dir)
        assert violations == [], (
            "src/ 发现跨用户排名查询路径（违反 D8）：\n"
            + "\n".join(f"  {v}" for v in violations)
        )
