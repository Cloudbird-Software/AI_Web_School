"""T-W4-043 学生模拟器参考客户端（dev-only OpenAPI 同步封装）.

定位：小程序团队的接口参考实现。模拟学生全部 C 端行为，调用 openapi-v1
全部 C 端端点；每次调用记录请求/响应，可导出为 consumer-driven 契约测试.

设计要点：
- **同步 API**：小程序团队惯用同步代码风格；内部用 asyncio.run 包装
  httpx.AsyncClient 调用。
- **双模式**：(1) HTTP 模式连真实服务器（base_url）；(2) ASGI 模式连进程内
  FastAPI app（asgi_app，用于测试，无需起服务器）。
- **mock 鉴权**：所有请求带 ``Authorization: Bearer <student_token>`` 头；
  dev-only，token 是 mock 的（生产鉴权走小程序登录链路，本模拟器不模拟）。
- **调用记录**：每次调用追加到 ``call_log``，可导出为 consumer-driven 契约
  测试用例（assert 请求 shape + 响应 status/schema）.
- **核心域零特判（A5）**：本模拟器不 import 学科包/学段包；只通过 openapi-v1
  定义的端点与平台交互.

用法（HTTP 模式）::

    client = SimulatorClient(base_url="http://localhost:8000")
    session = client.start_session(paper_id="paper-001", student_alias_id=...)

用法（ASGI 模式，测试用）::

    app = create_app()
    client = SimulatorClient(asgi_app=app)
    session = client.start_session(item_version_ids=["iv-1","iv-2"], ...)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Union
from uuid import UUID

import httpx

# Mock 学生 token（dev-only；生产鉴权走小程序登录链路，本模拟器不模拟）
DEFAULT_STUDENT_TOKEN = "sim-mock-token-student-001"


@dataclass
class CallRecord:
    """单次 API 调用记录（consumer-driven 契约测试用例的原始素材）."""

    method: str
    path: str
    request_body: Optional[dict[str, Any]] = None
    request_params: Optional[dict[str, Any]] = None
    status_code: Optional[int] = None
    response_body: Any = None
    error: Optional[str] = None

    def to_contract_case(self) -> dict[str, Any]:
        """导出为 consumer-driven 契约测试用例 dict."""
        return {
            "method": self.method,
            "path": self.path,
            "request_body": self.request_body,
            "request_params": self.request_params,
            "expected_status": self.status_code,
            "expected_response_schema_keys": (
                list(self.response_body.keys())
                if isinstance(self.response_body, dict)
                else None
            ),
        }


@dataclass
class SimulatorClient:
    """学生模拟器参考客户端（openapi-v1 全部 C 端接口同步封装）.

    Args:
        base_url: HTTP 模式基址（如 http://localhost:8000）。
        asgi_app: ASGI 模式进程内 app（测试用，与 base_url 互斥）。
        student_token: mock 鉴权 token（dev-only）。
        timeout: 请求超时（秒）。
    """

    base_url: Optional[str] = None
    asgi_app: Optional[Any] = None
    student_token: str = DEFAULT_STUDENT_TOKEN
    timeout: float = 30.0
    call_log: list[CallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.base_url is None and self.asgi_app is None:
            raise ValueError("base_url 与 asgi_app 至少提供其一")
        if self.base_url is not None and self.asgi_app is not None:
            raise ValueError("base_url 与 asgi_app 互斥（HTTP / ASGI 二选一）")

    # ── 内部：执行单次请求 ────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """构造请求头（含 mock 鉴权）."""
        return {
            "Authorization": f"Bearer {self.student_token}",
            "Content-Type": "application/json",
            "X-Simulator": "true",  # 标识模拟器调用（dev 旁路用）
        }

    def _execute(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[int, Any]:
        """执行单次 HTTP 请求（同步封装 async httpx）.

        Returns:
            (status_code, response_body) 元组。响应体非 JSON 时为原始文本。
        """
        record = CallRecord(
            method=method, path=path, request_body=body, request_params=params
        )
        self.call_log.append(record)

        async def _run() -> tuple[int, Any]:
            if self.asgi_app is not None:
                transport = httpx.ASGITransport(app=self.asgi_app)
                client_ctx = httpx.AsyncClient(
                    transport=transport, base_url="http://simulator", timeout=self.timeout
                )
            else:
                client_ctx = httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout
                )
            async with client_ctx as client:
                resp = await client.request(
                    method,
                    path,
                    params=params,
                    json=body if body is not None else None,
                    headers=self._headers(),
                )
                try:
                    resp_body: Any = resp.json()
                except (json.JSONDecodeError, ValueError):
                    resp_body = resp.text
                return resp.status_code, resp_body

        try:
            status_code, resp_body = asyncio.run(_run())
        except Exception as e:
            record.error = str(e)
            raise

        record.status_code = status_code
        record.response_body = resp_body
        return status_code, resp_body

    @staticmethod
    def _ensure_str_id(value: Union[str, UUID, Any]) -> str:
        """把 UUID / str / 其他 转为 str（路径参数序列化）."""
        if isinstance(value, UUID):
            return str(value)
        return str(value)

    # ── C 端学生侧：sessions（练习链路核心） ───────────────────────

    def start_session(
        self,
        *,
        student_alias_id: Union[str, UUID],
        paper_id: Optional[str] = None,
        item_version_ids: Optional[list[str]] = None,
        scene: str = "practice",
        gradeband: Optional[str] = None,
        retest_wrong: bool = False,
    ) -> dict[str, Any]:
        """POST /sessions — 开始练习（paper_id 或 item_version_ids 二选一）.

        Returns:
            响应 dict（session_id / status / scene / gradeband / total /
            time_limit_sec）。失败时抛 RuntimeError（含状态码与响应）.
        """
        body: dict[str, Any] = {
            "student_alias_id": self._ensure_str_id(student_alias_id),
            "scene": scene,
            "retest_wrong": retest_wrong,
        }
        if paper_id is not None:
            body["paper_id"] = paper_id
        if item_version_ids is not None:
            body["item_version_ids"] = item_version_ids
        if gradeband is not None:
            body["gradeband"] = gradeband

        status, resp = self._execute("POST", "/sessions", body=body)
        if status != 201:
            raise RuntimeError(f"POST /sessions 失败 [{status}]: {resp}")
        return resp

    def get_session(self, session_id: Union[str, UUID]) -> dict[str, Any]:
        """GET /sessions/{session_id} — 会话状态."""
        sid = self._ensure_str_id(session_id)
        status, resp = self._execute("GET", f"/sessions/{sid}")
        if status != 200:
            raise RuntimeError(f"GET /sessions/{sid} 失败 [{status}]: {resp}")
        return resp

    def get_next_item(self, session_id: Union[str, UUID]) -> dict[str, Any]:
        """GET /sessions/{session_id}/next — 取下一题（或 {done: true}）."""
        sid = self._ensure_str_id(session_id)
        status, resp = self._execute("GET", f"/sessions/{sid}/next")
        if status != 200:
            raise RuntimeError(f"GET /sessions/{sid}/next 失败 [{status}]: {resp}")
        return resp

    def submit_response(
        self,
        session_id: Union[str, UUID],
        *,
        item_version_id: str,
        response: dict[str, Any],
        duration_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """POST /sessions/{session_id}/responses — 提交作答（即时评分）."""
        sid = self._ensure_str_id(session_id)
        body: dict[str, Any] = {
            "item_version_id": item_version_id,
            "response": response,
        }
        if duration_ms is not None:
            body["duration_ms"] = duration_ms
        status, resp = self._execute("POST", f"/sessions/{sid}/responses", body=body)
        if status != 200:
            raise RuntimeError(
                f"POST /sessions/{sid}/responses 失败 [{status}]: {resp}"
            )
        return resp

    def resume_session(self, session_id: Union[str, UUID]) -> dict[str, Any]:
        """POST /sessions/{session_id}/resume — 休息确认."""
        sid = self._ensure_str_id(session_id)
        status, resp = self._execute("POST", f"/sessions/{sid}/resume")
        if status != 200:
            raise RuntimeError(f"POST /sessions/{sid}/resume 失败 [{status}]: {resp}")
        return resp

    def abandon_session(self, session_id: Union[str, UUID]) -> dict[str, Any]:
        """POST /sessions/{session_id}/abandon — 放弃会话."""
        sid = self._ensure_str_id(session_id)
        status, resp = self._execute("POST", f"/sessions/{sid}/abandon")
        if status != 200:
            raise RuntimeError(f"POST /sessions/{sid}/abandon 失败 [{status}]: {resp}")
        return resp

    # ── 报告与复习（学生侧） ───────────────────────────────────────

    def get_weakness_report(
        self,
        student_alias_id: Union[str, UUID],
        *,
        scene: Optional[str] = None,
        min_evidence: int = 3,
    ) -> dict[str, Any]:
        """GET /reports/weakness/{student_alias_id} — 弱项报告 v1."""
        sid = self._ensure_str_id(student_alias_id)
        params: dict[str, Any] = {"min_evidence": min_evidence}
        if scene is not None:
            params["scene"] = scene
        status, resp = self._execute(
            "GET", f"/reports/weakness/{sid}", params=params
        )
        if status != 200:
            raise RuntimeError(
                f"GET /reports/weakness/{sid} 失败 [{status}]: {resp}"
            )
        return resp

    def get_review_due(
        self,
        student_alias_id: Union[str, UUID],
        *,
        limit: int = 20,
        now: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """GET /review/due/{student_alias_id} — 复习到期取题."""
        sid = self._ensure_str_id(student_alias_id)
        params: dict[str, Any] = {"limit": limit}
        if now is not None:
            params["now"] = now
        status, resp = self._execute(
            "GET", f"/review/due/{sid}", params=params
        )
        if status != 200:
            raise RuntimeError(f"GET /review/due/{sid} 失败 [{status}]: {resp}")
        return resp if isinstance(resp, list) else [resp]

    # ── 教研端只读查询（模拟器作参考客户端亦覆盖） ─────────────────

    def get_item(self, item_id: str) -> dict[str, Any]:
        """GET /items/{item_id} — 查询 item 身份与当前版本."""
        status, resp = self._execute("GET", f"/items/{item_id}")
        if status != 200:
            raise RuntimeError(f"GET /items/{item_id} 失败 [{status}]: {resp}")
        return resp

    def get_item_version(self, item_version_id: str) -> dict[str, Any]:
        """GET /item_versions/{item_version_id} — 查询版本内容与谱系."""
        status, resp = self._execute("GET", f"/item_versions/{item_version_id}")
        if status != 200:
            raise RuntimeError(
                f"GET /item_versions/{item_version_id} 失败 [{status}]: {resp}"
            )
        return resp

    def get_template(self, template_id: str) -> dict[str, Any]:
        """GET /templates/{template_id} — 查询母题身份与当前版本."""
        status, resp = self._execute("GET", f"/templates/{template_id}")
        if status != 200:
            raise RuntimeError(f"GET /templates/{template_id} 失败 [{status}]: {resp}")
        return resp

    def get_gate_certificate(self, cert_id: str) -> dict[str, Any]:
        """GET /gate_certificates/{cert_id} — 查询门证书."""
        status, resp = self._execute("GET", f"/gate_certificates/{cert_id}")
        if status != 200:
            raise RuntimeError(
                f"GET /gate_certificates/{cert_id} 失败 [{status}]: {resp}"
            )
        return resp

    def health(self) -> dict[str, str]:
        """GET /health — 健康检查."""
        status, resp = self._execute("GET", "/health")
        if status != 200:
            raise RuntimeError(f"GET /health 失败 [{status}]: {resp}")
        return resp

    # ── 调用记录导出（consumer-driven 契约测试用例） ───────────────

    def export_call_log(self) -> list[dict[str, Any]]:
        """导出全部调用记录为 consumer-driven 契约测试用例列表."""
        return [r.to_contract_case() for r in self.call_log]

    def clear_call_log(self) -> None:
        """清空调用记录（场景之间复用同一 client 时用）."""
        self.call_log.clear()


__all__ = [
    "DEFAULT_STUDENT_TOKEN",
    "CallRecord",
    "SimulatorClient",
]
