"""T-W4-045 学生模拟器复习链路场景（错题入队→到期提醒→取到复习题→作答）.

完整 e2e 链路（架构 §4.4 / OPC §6.5 E2E-1 承载）：
1. practice_to_produce_wrong：POST /sessions scene=practice → 故意答错（落 response_event）
2. sync_queue：调用 sync_fn 回调（测试提供，调 sync_review_queue 服务入队）
3. fetch_due_reviews：GET /review/due/{sid}?now=<future> 取到期复习题
4. start_review_session：POST /sessions 用到期 item_version_ids 开复习会话
5. answer_review_items：循环 GET /next → POST /responses 作答复习题

为什么 sync_queue 用回调而不直连 DB：
- 场景是「客户端参考实现」，不应直连服务端 DB（耦合 + 跨进程不可用）。
- sync_review_queue 是服务端内部入口（无 openapi-v1 端点暴露——队列同步由
  作答链路或定时作业触发，非学生 C 端行为）。
- 测试提供 sync_fn 闭包，封装对 sync_review_queue 的调用（含 db_session 闭包），
  保持场景的客户端纯度同时让 e2e 链路自洽.

复习排程（验收 #3）：固定间隔 1/3/7/21 天（迁移 0010 内置策略种子）.
错题入队后 due_at = 事件时刻 + 1 天；测试用 due_now 推进到 1 天后触发到期.

宪法 A5/X6：本场景只通过 SimulatorClient 调用 openapi-v1 端点 + sync_fn 回调，
不 import 学科包.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tests.simulator.scenarios import BaseScenario
from tests.simulator.client import SimulatorClient


@dataclass
class ReviewScenario(BaseScenario):
    """复习链路场景：错题入队 → 到期提醒 → 取到复习题 → 作答.

    用法::

        # 测试提供 sync_fn（闭包封装 db_session）
        def _sync_fn(student_alias_id: str) -> int:
            return asyncio.run(sync_review_queue(
                async_session, student_alias_id=UUID(student_alias_id)
            ))

        scenario = ReviewScenario(
            client=client,
            student_alias_id=str(student_alias_id),
            practice_item_version_ids=[v1, v2],
            gradeband="M",
            practice_answers={v1: {"selected": "A"}, v2: {"selected": "A"}},  # 全错
            sync_fn=_sync_fn,
            due_now="2026-08-15T00:00:00Z",  # 推进到 1 天后
        )
        result = scenario.run()
        assert result["all_ok"] is True
        assert "review_feedbacks" in result["state"]
    """

    student_alias_id: str = ""
    # 第一阶段练习卷题（用于产生错题入队）
    practice_item_version_ids: list[str] = field(default_factory=list)
    gradeband: str | None = None
    # 练习阶段每题作答（故意答错以入复习队列）
    practice_answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # sync_review_queue 回调（测试提供，避免场景直连 DB）
    sync_fn: Optional[Callable[[str], int]] = None
    # 到期判定的基准时刻（ISO 字符串，传给 GET /review/due?now=...）
    due_now: Optional[str] = None

    steps: list[str] = field(
        default_factory=lambda: [
            "practice_to_produce_wrong",
            "sync_queue",
            "fetch_due_reviews",
            "start_review_session",
            "answer_review_items",
        ]
    )

    def practice_to_produce_wrong(self) -> list[dict[str, Any]]:
        """步骤 1：跑练习会话故意答错，落 response_event 产生错题证据.

        Feedback 模型不含 item_version_id 字段（仅 event_id/correct/...），
        故从 get_next_item 响应记录题号，关联到 feedback 供下游追溯.
        """
        session = self.client.start_session(
            student_alias_id=self.student_alias_id,
            item_version_ids=self.practice_item_version_ids,
            scene="practice",
            gradeband=self.gradeband,
        )
        session_id = session["session_id"]
        feedbacks: list[dict[str, Any]] = []
        wrong_item_version_ids: list[str] = []
        while True:
            nxt = self.client.get_next_item(session_id)
            if nxt.get("done") is True:
                break
            iv_id = nxt.get("item_version_id")
            if iv_id is None:
                raise RuntimeError(f"next 响应缺 item_version_id: {nxt}")
            answer = self.practice_answers.get(iv_id, {})
            fb = self.client.submit_response(
                session_id, item_version_id=iv_id, response=answer
            )
            # 关联 item_version_id（Feedback 不含此字段，从 next 响应补入）
            fb_with_iv = {**fb, "item_version_id": iv_id}
            feedbacks.append(fb_with_iv)
            if fb.get("correct") is False:
                wrong_item_version_ids.append(iv_id)
        self.state["practice_feedbacks"] = feedbacks
        self.state["wrong_item_version_ids"] = wrong_item_version_ids
        return feedbacks

    def sync_queue(self) -> int:
        """步骤 2：调用 sync_review_queue 入队（通过测试提供的 sync_fn 回调）.

        为什么不直连 DB：场景是客户端参考实现，sync_review_queue 是服务端内部
        入口（无 C 端 API）。测试提供闭包封装 db_session 调用，保持场景纯度.
        """
        if self.sync_fn is None:
            raise RuntimeError(
                "sync_fn 未提供——复习场景需要 sync_review_queue 入队；"
                "测试须提供 sync_fn(student_alias_id) -> int 回调"
            )
        count = self.sync_fn(self.student_alias_id)
        self.state["queue_count"] = count
        return count

    def fetch_due_reviews(self) -> list[dict[str, Any]]:
        """步骤 3：GET /review/due/{sid}?now=<future> 取到期复习题.

        验收 #2/#3：到期提醒——固定间隔 1/3/7/21 天，错题入队后 +1 天到期.
        """
        due = self.client.get_review_due(
            self.student_alias_id, now=self.due_now
        )
        if not isinstance(due, list):
            raise RuntimeError(f"到期复习响应非 list: {due}")
        self.state["due_reviews"] = due
        self.state["due_item_version_ids"] = [
            e.get("item_version_id") for e in due if isinstance(e, dict)
        ]
        return due

    def start_review_session(self) -> dict[str, Any]:
        """步骤 4：POST /sessions 用到期复习题开新会话.

        复习会话 scene=practice（复习是练习的子集，diagnosis 仅用于首次诊断）.
        """
        due_ids = self.state.get("due_item_version_ids", [])
        if not due_ids:
            raise RuntimeError("无到期复习题——fetch_due_reviews 未返回条目")
        session = self.client.start_session(
            student_alias_id=self.student_alias_id,
            item_version_ids=due_ids,
            scene="practice",
            gradeband=self.gradeband,
        )
        self.state["review_session_id"] = session["session_id"]
        return session

    def answer_review_items(self) -> list[dict[str, Any]]:
        """步骤 5：循环作答复习题（答对推进 stage）."""
        session_id = self.state["review_session_id"]
        feedbacks: list[dict[str, Any]] = []
        while True:
            nxt = self.client.get_next_item(session_id)
            if nxt.get("done") is True:
                break
            iv_id = nxt.get("item_version_id")
            if iv_id is None:
                raise RuntimeError(f"复习 next 响应缺 item_version_id: {nxt}")
            # 复习作答：默认答对（推进 stage；测试可覆写 review_answers 控制对错）
            answer = self.state.get("review_answers", {}).get(iv_id, {"selected": "B"})
            fb = self.client.submit_response(
                session_id, item_version_id=iv_id, response=answer
            )
            feedbacks.append(fb)
        self.state["review_feedbacks"] = feedbacks
        return feedbacks

    def assert_review_chain_consistent(self) -> None:
        """断言复习链路数据一致性（验收 #2）.

        - 有错题入队（wrong_item_version_ids 非空）
        - sync_queue 入队数 > 0
        - 到期复习题非空
        - 复习会话完成且作答数 = 到期题数
        """
        wrong_ids = self.state.get("wrong_item_version_ids", [])
        if not wrong_ids:
            raise RuntimeError("练习阶段未产生错题——practice_answers 可能全对")
        if self.state.get("queue_count", 0) <= 0:
            raise RuntimeError("sync_queue 入队数为 0")
        due_ids = self.state.get("due_item_version_ids", [])
        if not due_ids:
            raise RuntimeError("无到期复习题——due_now 可能未推进到到期时刻")
        review_feedbacks = self.state.get("review_feedbacks", [])
        if len(review_feedbacks) != len(due_ids):
            raise RuntimeError(
                f"复习作答数 {len(review_feedbacks)} != 到期题数 {len(due_ids)}"
            )


__all__ = ["ReviewScenario"]
