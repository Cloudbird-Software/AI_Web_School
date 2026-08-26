"""T-W2-041 教研工作台 FastAPI 应用入口.

启动方式：
    uvicorn src.workbench.main:app --reload --port 8001

挂载：
- /login（GET/POST）：登录页 + 静态 token 校验
- /logout：清除 cookie
- /：首页（重定向到 /items）
- /items、/items/{id}：题库列表/详情（T-W2-041）
- /templates/new：母题表单 + 预览（T-W2-042）
- /issue/{item_version_id}：签发闭环（T-W2-043）

宪法 D1：写入路径走 src/core/content/writer.py + publication.py；
宪法 A5/X6：本包不 import 学科包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from src.workbench.auth import (
    SESSION_COOKIE_NAME,
    optional_session,
    require_session,
    session_cookie_value,
    verify_token,
)
from src.workbench.pages import items, template_form, issue

# ────────────────────────────────────────────────────────────────────
# 模板目录：src/workbench/templates/
# ────────────────────────────────────────────────────────────────────
_TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"

# 登录后回跳的默认路径
_DEFAULT_NEXT: str = "/items"


def _safe_next(next_url: str) -> str:
    """登录回跳地址白名单化（CodeQL py/url-redirection，T-W0-011）.

    三道防线（缺一不可，为什么：CodeQL UrlRedirect 污点模型只认两类
    sanitizer——常量前缀拼接（用户只控制后缀）与常量比较守卫；纯前缀/
    urlparse 负向守卫不在模型内，T-W0-010/#39 两轮实证其不足以关警报）：

    1. 前缀防线：仅接受以单个 ``/`` 开头，且第二个字符不得是 ``/`` 或 ``\\``
       （WHATWG URL 规范把权威段位置的 ``\\`` 归一化为 ``/``——
       ``/\\evil.com`` 在浏览器里等于 ``//evil.com``，是跨域跳转，必须拦）；
    2. 结构防线：``urlparse`` 解析后 scheme 与 netloc 必须为空——任何携带
       协议或授权段的值一律回落 ``/items``；
    3. 重构防线：输出恒为 ``"/" + 用户可控后缀``——重定向目标的前缀永远
       由常量提供（CodeQL StringConcatAsSanitizer 认可形态），用户输入
       不可能出现在 URL 前缀位置，开放重定向在构造上不可达。
    """
    if not next_url.startswith("/") or next_url[1:2] in ("/", "\\"):
        return _DEFAULT_NEXT
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return _DEFAULT_NEXT
    safe = "/" + parsed.path.lstrip("/")
    if parsed.query:
        safe += "?" + parsed.query
    return safe


def create_app() -> FastAPI:
    """构造工作台 FastAPI 应用.

    为什么用工厂函数：与 src/api/main.py 一致，便于测试 ASGI 启动 + 依赖覆写。
    """
    app = FastAPI(
        title="muti-platform 教研工作台",
        version="1.0.0",
        description=(
            "教研工作台 v1：登录 + 题库只读 CRUD + 母题表单 + 签发闭环。"
            "服务端 Jinja2 渲染 HTML，无前端构建链。"
        ),
        docs_url=None,  # 工作台不暴露 Swagger UI（避免与 src/api 冲突）
        redoc_url=None,
        openapi_url=None,
    )

    # Jinja2 模板引擎：挂到 app.state，由 pages 通过 request.app.state 取
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # 注册路由
    app.include_router(items.router)
    app.include_router(template_form.router)
    app.include_router(issue.router)

    # ── 登录页 ──
    @app.get("/login", response_class=HTMLResponse, summary="登录页")
    async def login_page(
        request: Request,
        next: str = "/items",
        session: Optional[str] = Depends(optional_session),
    ) -> HTMLResponse:
        """GET /login：渲染登录表单；已登录则重定向到 next."""
        safe_next = _safe_next(next)
        if session:
            return RedirectResponse(url=safe_next, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": safe_next, "error": None},
        )

    @app.post("/login", summary="登录提交")
    async def login_submit(
        request: Request,
        token: str = Form(...),
        next: str = Form("/items"),
    ) -> RedirectResponse:
        """POST /login：校验 token，成功设置 cookie + 重定向到 next.

        失败重渲染登录页，回显 'token 错误' 提示（不暴露 token 是否存在）。
        """
        safe_next = _safe_next(next)
        if verify_token(token):
            resp = RedirectResponse(url=safe_next, status_code=303)
            # httponly + samesite=lax：防 XSS 读 cookie + 防 CSRF 跨站携带；
            # value 为服务端 HMAC 派生值（CodeQL py/cookie-injection），
            # 用户提交的 token 不再进入响应或 cookie。
            resp.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=session_cookie_value(),
                httponly=True,
                samesite="lax",
                secure=False,  # W2 开发环境 HTTP；生产部署需切 HTTPS + secure=True
            )
            return resp
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": safe_next, "error": "token 错误，请重试"},
            status_code=401,
        )

    @app.get("/logout", summary="登出")
    async def logout(
        _token: str = Depends(require_session),
    ) -> RedirectResponse:
        """GET /logout：清除 session cookie，重定向到 /login."""
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(key=SESSION_COOKIE_NAME)
        return resp

    # ── 首页：重定向到 /items ──
    @app.get("/", summary="首页")
    async def index(
        _token: str = Depends(require_session),
    ) -> RedirectResponse:
        return RedirectResponse(url="/items", status_code=303)

    # ── 健康检查（无需鉴权，便于 docker compose 健康探测）──
    @app.get("/health", summary="健康检查")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# 模块级 app 实例：uvicorn src.workbench.main:app 直接启动
app = create_app()


__all__ = ["app", "create_app"]
