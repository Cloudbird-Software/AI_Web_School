"""T-W4-043/044/045 学生模拟器场景编排包.

本 ``__init__.py`` 提供 ``BaseScenario`` 与 ``PracticeScenario``（T-W4-043 验收 #2
要求）；T-W4-044 在 ``practice_scenario.py`` 扩展完整 e2e 链路；T-W4-045 在
``diagnosis_scenario.py`` / ``review_scenario.py`` 扩展诊断与复习链路.

Scenario 基类支持步骤编排与状态传递：
- ``steps`` 列表声明按序执行的方法名（子类覆写）
- ``state`` dict 在步骤间传递数据（如 session_id 从 start_session 流向 answer_items）
- ``run()`` 按序执行步骤，每步结果记入 ``results``，最后返回 ``summary()``

宪法 A5/X6：场景只通过 SimulatorClient 调用 openapi-v1 端点，不 import 学科包.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from tests.simulator.client import SimulatorClient


@dataclass
class BaseScenario:
    """场景基类：步骤编排 + 状态传递 + 调用记录汇总.

    子类覆写 ``steps`` 列表（按序执行的方法名），实现各步骤方法。
    每个步骤方法可读写 ``self.state``（dict）传递数据。

    Attributes:
        client: 模拟器客户端（场景调用 openapi-v1 端点的唯一入口）。
        state: 步骤间状态传递 dict（如 {"session_id": "..."}）。
        results: 每步骤执行结果列表（审计/断言用）。
    """

    client: SimulatorClient
    state: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)

    # 子类覆写：按序执行的方法名列表
    steps: list[str] = field(default_factory=list)

    def run(self) -> dict[str, Any]:
        """按序执行 steps 列表的方法，返回 summary.

        每步骤结果追加到 ``self.results``；步骤抛异常时记入 result 并中止
        后续步骤（保留已完成的步骤结果，便于诊断）.
        """
        for step_name in self.steps:
            step_method: Callable[[], Any] = getattr(self, step_name)
            try:
                result = step_method()
                self.results.append({"step": step_name, "ok": True, "result": result})
            except Exception as e:
                self.results.append(
                    {"step": step_name, "ok": False, "error": str(e)}
                )
                # 中止后续步骤（保留已完成步骤的结果）
                break
        return self.summary()

    def summary(self) -> dict[str, Any]:
        """汇总场景执行结果（场景名 / 步骤结果 / 调用记录）."""
        all_ok = all(r.get("ok", False) for r in self.results)
        return {
            "scenario": self.__class__.__name__,
            "all_ok": all_ok,
            "steps": self.results,
            "calls": self.client.export_call_log(),
            "state": dict(self.state),
        }


@dataclass
class PracticeScenario(BaseScenario):
    """练习场景骨架：领卷 → 逐题作答 → 结束.

    验收 #2：至少提供 BaseScenario 与 PracticeScenario。
    本卡提供骨架（结构正确性 + 步骤编排），完整 e2e 数据流在 T-W4-044 扩展.

    用法::

        scenario = PracticeScenario(
            client=client,
            student_alias_id=...,
            item_version_ids=["iv-1", "iv-2"],
        )
        result = scenario.run()
        assert result["all_ok"] is True
    """

    student_alias_id: str = ""
    item_version_ids: list[str] = field(default_factory=list)
    paper_id: str | None = None
    # 每题作答（item_version_id → answer dict）；子类/调用方覆写
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)

    steps: list[str] = field(
        default_factory=lambda: [
            "start_session",
            "answer_all_items",
            "verify_session_done",
        ]
    )

    def start_session(self) -> dict[str, Any]:
        """步骤 1：POST /sessions 开始练习."""
        session = self.client.start_session(
            student_alias_id=self.student_alias_id,
            paper_id=self.paper_id,
            item_version_ids=self.item_version_ids or None,
            scene="practice",
        )
        self.state["session_id"] = session["session_id"]
        self.state["total"] = session["total"]
        return session

    def answer_all_items(self) -> list[dict[str, Any]]:
        """步骤 2：循环 GET /next → POST /responses 直到 done."""
        session_id = self.state["session_id"]
        feedbacks: list[dict[str, Any]] = []
        while True:
            nxt = self.client.get_next_item(session_id)
            if nxt.get("done") is True:
                break
            item_version_id = nxt.get("item_version_id")
            if item_version_id is None:
                raise RuntimeError(f"next 响应缺 item_version_id: {nxt}")
            answer = self.answers.get(item_version_id, {})
            fb = self.client.submit_response(
                session_id,
                item_version_id=item_version_id,
                response=answer,
            )
            feedbacks.append(fb)
        self.state["feedbacks"] = feedbacks
        return feedbacks

    def verify_session_done(self) -> dict[str, Any]:
        """步骤 3：取 next 应返回 done=True（会话已完成）."""
        session_id = self.state["session_id"]
        nxt = self.client.get_next_item(session_id)
        # 完成后 next 返回 done=True（或 409；本骨架接受 done=True）
        if nxt.get("done") is not True:
            raise RuntimeError(f"会话未完成: {nxt}")
        self.state["done"] = True
        return nxt


__all__ = ["BaseScenario", "PracticeScenario"]
