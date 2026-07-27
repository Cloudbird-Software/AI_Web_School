"""T-W2-039 FastAPI 只读 API v1 应用入口.

启动方式：
    uvicorn src.api.main:app --reload

宪法 D1：本应用仅暴露只读端点；写入路径在 src/core/content/writer.py 等。
宪法 A5/X6：本应用不 import 任何学科包/学段包。
"""
from __future__ import annotations

from fastapi import FastAPI

from src.api.routers import gate, items, report


def create_app() -> FastAPI:
    """构造 FastAPI 应用（测试与生产共用；测试可覆写依赖）.

    为什么用工厂函数：测试需在 import 时拿到 app 实例以注入依赖覆写；
    模块级 app 实例亦暴露（`app`），方便 uvicorn 直接启动。
    """
    app = FastAPI(
        title="muti-platform 只读 API v1",
        version="1.0.0",
        description=(
            "小学语数英个性化练习平台 · 只读查询 API。"
            "覆盖 item / item_version / item_template / gate_certificate。"
            "写入路径走 src/core/content/writer.py 等服务层，本 API 不暴露。"
        ),
        # OpenAPI 草稿落在 src/api/openapi-draft.yaml（T-W2-040 据此反向定稿契约）
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.include_router(items.router)
    app.include_router(gate.router)
    app.include_router(report.router)

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict[str, str]:
        """健康检查端点（无需 DB 连接）."""
        return {"status": "ok"}

    return app


# 模块级 app 实例：uvicorn src.api.main:app 直接启动
app = create_app()


__all__ = ["app", "create_app"]
