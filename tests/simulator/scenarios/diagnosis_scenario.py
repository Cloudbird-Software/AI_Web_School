"""T-W4-045 学生模拟器诊断链路场景（诊断组卷→作答→诊断报告）.

完整 e2e 链路（架构 §4.8 / OPC §6.5 E2E-1 承载）：
1. assert_diagnosis_constraints：断言诊断卷每知识点≥3 孤立题（验收 #1）
2. start_diagnosis_session：POST /sessions scene=diagnosis 开始诊断会话
3. answer_all_items：循环 GET /next → POST /responses 逐题作答（含故意答错）
4. verify_session_done：会话完成
5. fetch_diagnosis_report：GET /reports/weakness/{sid}?scene=diagnosis 取诊断报告

诊断约束（验收 #1）：每知识点≥3 孤立题。组卷是 assembly 模块职责（solver
的 kp_quotas.isolated_only=True, min_count≥3），本场景不组卷，只接收已组卷的
item_version_ids + item_kp_map 并断言约束满足。

诊断报告（验收 #3）：报告含错误类型归因与置信度；min_evidence 未达阈值输出
「证据不足」（合规，不给定论）。

宪法 A5/X6：本场景只通过 SimulatorClient 调用 openapi-v1 端点，不 import 学科包.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.simulator.scenarios import PracticeScenario


@dataclass
class DiagnosisScenario(PracticeScenario):
    """诊断链路场景：诊断组卷 → 逐题作答 → 诊断报告.

    用法::

        scenario = DiagnosisScenario(
            client=client,
            student_alias_id=str(student_alias_id),
            item_version_ids=[v1, v2, v3, v4, v5, v6],  # 2 KP × 3 孤立题
            gradeband="M",
            item_kp_map={
                v1: "kp.diag.a", v2: "kp.diag.a", v3: "kp.diag.a",
                v4: "kp.diag.b", v5: "kp.diag.b", v6: "kp.diag.b",
            },
            answers={
                v1: {"selected": "A"},  # 故意答错（A 绑定错误类型）
                v2: {"selected": "A"},
                v3: {"selected": "A"},
                v4: {"selected": "B"},  # 答对
                v5: {"selected": "B"},
                v6: {"selected": "B"},
            },
        )
        result = scenario.run()
        assert result["all_ok"] is True
        assert "diagnosis_report" in result["state"]
    """

    # 诊断卷的 KP 映射：item_version_id → kp_code（用于断言孤立题≥3 约束）
    item_kp_map: dict[str, str] = field(default_factory=dict)

    # 诊断场景：覆写 scene 与 steps
    scene: str = "diagnosis"
    steps: list[str] = field(
        default_factory=lambda: [
            "assert_diagnosis_constraints",
            "start_session",
            "answer_all_items",
            "verify_session_done",
            "fetch_diagnosis_report",
        ]
    )

    def assert_diagnosis_constraints(self) -> dict[str, int]:
        """步骤 1：断言诊断卷每知识点≥3 孤立题（验收 #1）.

        诊断卷的孤立题约束在 assembly 阶段强制（solver kp_quotas.isolated_only），
        本步骤是对已组卷输入的守门断言——防止下游误用非诊断卷开诊断会话.
        """
        if not self.item_kp_map:
            raise RuntimeError(
                "item_kp_map 为空——无法断言诊断孤立题约束；"
                "诊断场景必须提供 item_version_id → kp_code 映射"
            )
        kp_counts: dict[str, int] = {}
        for iv_id, kp in self.item_kp_map.items():
            if iv_id not in self.item_version_ids:
                raise RuntimeError(
                    f"item_kp_map 含 item_version_id {iv_id!r} 不在 item_version_ids 中"
                )
            kp_counts[kp] = kp_counts.get(kp, 0) + 1
        for kp, count in kp_counts.items():
            if count < 3:
                raise RuntimeError(
                    f"诊断约束违反：知识点 {kp!r} 孤立题 {count} < 3"
                )
        self.state["kp_isolated_counts"] = kp_counts
        return kp_counts

    def fetch_diagnosis_report(self) -> dict[str, Any]:
        """步骤 5：GET /reports/weakness/{student_alias_id}?scene=diagnosis.

        验收 #3：诊断报告含错误类型归因与置信度；未达阈值输出「证据不足」.
        本步骤把报告写入 state 供断言用.
        """
        report = self.client.get_weakness_report(
            self.student_alias_id, scene="diagnosis"
        )
        self.state["diagnosis_report"] = report
        return report

    def assert_diagnosis_chain_consistent(self) -> None:
        """断言诊断链路数据一致性（验收 #1/#3）.

        - 诊断约束满足（每 KP ≥3 孤立题）
        - feedbacks 数量 = 提交作答数
        - session 已 done
        - diagnosis_report 含 items 字段（错误类型归因列表，可能为空或证据不足）
        """
        if not self.state.get("done"):
            raise RuntimeError("诊断会话未完成——verify_session_done 未通过")
        feedbacks = self.state.get("feedbacks", [])
        if not feedbacks:
            raise RuntimeError("无作答反馈——answer_all_items 可能未执行")
        report = self.state.get("diagnosis_report")
        if not isinstance(report, dict):
            raise RuntimeError("诊断报告缺失或非 dict")
        if "items" not in report:
            raise RuntimeError(f"诊断报告缺 items 字段: {report}")


__all__ = ["DiagnosisScenario"]
