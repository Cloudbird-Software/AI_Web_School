"""T-W2-041 工作台静态 token 鉴权.

W2 任务卡 non_goals 已排除完整 RBAC；采用单用户静态 token：
- token 通过环境变量 WORKBENCH_TOKEN 配置（启动时读取）。
- 登录页 POST 校验 token，成功后设置 cookie `workbench_session=<token>`。
- 受保护页面通过 `require_session` 依赖校验 cookie；失败 302 重定向到 /login。

为什么用 cookie 而非 Authorization header：工作台是浏览器多页应用，
cookie 由浏览器自动携带，无需 JS 手动管理 header；W2 不引入前端构建链。
为什么用 signed-token cookie 而非 session store：W2 单用户，token 即 session id，
无需服务端 session 存储（Redis 留给后续波次）。

宪法 D1：本模块不写 DB；宪法 A5/X6：不 import 学科包。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Cookie, HTTPException, Request, status

# ────────────────────────────────────────────────────────────────────
# 配置：静态 token 从环境变量读取
# ────────────────────────────────────────────────────────────────────
# 为什么允许默认 token：开发环境便利；生产部署必须通过 .env 覆盖。
# 默认 token 明示 'dev-token-change-me'，运维一眼可识别未配置环境。
_DEFAULT_TOKEN: str = "dev-token-change-me"


def get_workbench_token() -> str:
    """读取工作台静态 token（启动时与每次请求时调用）.

    Returns:
        WORKBENCH_TOKEN 环境变量值；未设置时返回默认 dev token。
    """
    return os.environ.get("WORKBENCH_TOKEN", _DEFAULT_TOKEN)


# cookie 名：workbench_session
SESSION_COOKIE_NAME: str = "workbench_session"

# 会话 cookie 值的域分离常量：防止派生值与 token 本身用途混淆
_SESSION_PURPOSE: bytes = b"workbench-session-v1"


def session_cookie_value() -> str:
    """派生会话 cookie 值 = HMAC-SHA256(WORKBENCH_TOKEN, purpose 常量).

    为什么不让 cookie 直接存用户提交的 token（CodeQL py/cookie-injection）：
    cookie 值若原样携带用户输入，攻击面包括 header 注入与回显存储；
    由服务端密钥派生的固定值与用户输入完全解耦，且校验仍是常量时间比较。
    token 本身只出现在表单校验路径，不再进入任何响应。
    """
    key = get_workbench_token().encode("utf-8")
    return hmac.new(key, _SESSION_PURPOSE, hashlib.sha256).hexdigest()


def verify_session_cookie(value: str) -> bool:
    """常量时间校验会话 cookie 值是否为服务端派生值."""
    expected = session_cookie_value()
    if not expected:
        return False
    return secrets.compare_digest(value, expected)


def verify_token(token: str) -> bool:
    """常量时间比较 token，避免时序侧信道（即使 W2 单用户也按工程规范写）.

    为什么用 secrets.compare_digest 而非 ==：字符串比较在第一个不同字节即返回，
    攻击者可通过响应时间侧信道逐字节探测 token；常量时间比较规避此风险。
    """
    expected = get_workbench_token()
    if not expected:
        return False
    return secrets.compare_digest(token, expected)


# ────────────────────────────────────────────────────────────────────
# 依赖：校验 session cookie
# ────────────────────────────────────────────────────────────────────


async def require_session(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str:
    """受保护页面依赖：校验 cookie 中的 token，失败抛 302 → /login.

    Returns:
        校验通过的 token（可在路由内取当前用户身份）。

    Raises:
        HTTPException(302)：重定向到 /login?next=<原路径>，由调用方捕获。
        本依赖改为抛 HTTPException(302) 而非直接返回 RedirectResponse，
        以便 FastAPI 路由层统一处理（避免双重响应）。
    """
    if session and verify_session_cookie(session):
        return session
    # 重定向到登录页，携带 next 参数
    next_path = request.url.path
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={next_path}"},
    )


async def optional_session(
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Optional[str]:
    """非受保护页面依赖：返回 token 或 None（不重定向）.

    用于登录页本身：已登录用户访问 /login 时可重定向到首页。
    """
    if session and verify_session_cookie(session):
        return session
    return None


__all__ = [
    "SESSION_COOKIE_NAME",
    "get_workbench_token",
    "require_session",
    "optional_session",
    "session_cookie_value",
    "verify_session_cookie",
    "verify_token",
]
