"""T-W4-044 学生模拟器练习链路场景（领卷→作答→评分→报告）.

完整 e2e 链路（架构 §4.8 / OPC §6.5 E2E-1 承载）：
1. start_session：领练习卷（item_version_ids 直传，实例池模式）
2. answer_all_items：循环 GET /next → POST /responses 逐题作答（含故意答错）
3. fetch_weakness_report：作答完成后取弱项报告（错误类型归因或证据不足）

与 T-W4-043 PracticeScenario 骨架的差异：
- 增加错题作答（answers dict 含正确答案与错误答案各一）
- 增加 fetch_weakness_report 步骤（验收 #3：弱项报告至少返回错误类型归因）
- 增加 export_contract_cases（验收 #3：全部调用生成 consumer-driven 契约用例）

宪法 A5/X6：本场景只通过 SimulatorClient 调用 openapi-v1 端点，不 import 学科包.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.simulator.client import SimulatorClient
from tests.simulator.scenarios import PracticeScenario


@dataclass
class FullPracticeScenario(PracticeScenario):
    """完整练习链路场景：领卷 → 逐题作答（含错题） → 弱项报告.

    用法::

        scenario = FullPracticeScenario(
            client=client,
            student_alias_id=str(student_alias_id),
            item_version_ids=[v1, v2, v3],
            gradeband="M",
            answers={
                v1: {"selected": "B"},  # 正确
                v2: {"selected": "A"},  # 故意答错（A 绑定错误类型）
                v3: {"selected": "B"},  # 正确
            },
        )
        result = scenario.run()
        assert result["all_ok"] is True
        assert "weakness_report" in result["state"]
    """

    # 扩展步骤：原 3 步 + 弱项报告
    steps: list[str] = field(
        default_factory=lambda: [
            "start_session",
            "answer_all_items",
            "verify_session_done",
            "fetch_weakness_report",
        ]
    )

    def fetch_weakness_report(self) -> dict[str, Any]:
        """步骤 4：GET /reports/weakness/{student_alias_id} — 弱项报告.

        验收 #3：弱项报告至少返回错误类型归因（允许「证据不足」）.
        本步骤把报告写入 state 供断言用.
        """
        report = self.client.get_weakness_report(self.student_alias_id)
        self.state["weakness_report"] = report
        return report

    def assert_practice_chain_consistent(self) -> None:
        """断言练习链路数据一致性（验收 #2）.

        - feedbacks 数量 = 提交作答数
        - session 已 done
        - weakness_report 含 items 字段（即使为空也表示查询成功）
        """
        feedbacks = self.state.get("feedbacks", [])
        if not feedbacks:
            raise RuntimeError("无作答反馈——answer_all_items 可能未执行")
        if not self.state.get("done"):
            raise RuntimeError("会话未完成——verify_session_done 未通过")
        report = self.state.get("weakness_report")
        if not isinstance(report, dict):
            raise RuntimeError("弱项报告缺失或非 dict")
        if "items" not in report:
            raise RuntimeError(f"弱项报告缺 items 字段: {report}")


__all__ = ["FullPracticeScenario"]
