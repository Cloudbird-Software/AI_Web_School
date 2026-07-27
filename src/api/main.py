"""T-W2-039 FastAPI 只读 API v1 应用入口；W3-S3 接入会话（写）路由.

启动方式：
    uvicorn src.api.main:app --reload

路由构成：
- items / gate：只读查询（宪法 D1：写入路径在 src/core/content/writer.py 等）。
- sessions（W3-S3）：学生侧在线作答会话——开始练习/取题/提交作答/休息确认。
  作答事实经 W3-S4 score_and_record 落 response_event（append-only 账），
  会话表（practice_session）只存运行态进度（非三本账）。

宪法 A5/X6：本应用不 import 任何学科包/学段包。
"""
from __future__ import annotations

from fastapi import FastAPI

from src.api.routers import gate, items, session


def create_app() -> FastAPI:
    """构造 FastAPI 应用（测试与生产共用；测试可覆写依赖）.

    为什么用工厂函数：测试需在 import 时拿到 app 实例以注入依赖覆写；
    模块级 app 实例亦暴露（`app`），方便 uvicorn 直接启动。
    """
    app = FastAPI(
        title="muti-platform API v1",
        version="1.0.0",
        description=(
            "小学语数英个性化练习平台 API。"
            "items/gate 为只读查询；sessions 为学生侧在线作答会话（W3-S3）。"
        ),
        # OpenAPI 草稿落在 src/api/openapi-draft.yaml（T-W2-040 据此反向定稿契约）
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.include_router(items.router)
    app.include_router(gate.router)
    app.include_router(session.router)

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict[str, str]:
        """健康检查端点（无需 DB 连接）."""
        return {"status": "ok"}

    return app


# 模块级 app 实例：uvicorn src.api.main:app 直接启动
app = create_app()


__all__ = ["app", "create_app"]
