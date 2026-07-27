"""T-W2-006 difficulty_relevant 槽变更检测与难度重估事件 单元测试.

验收对照：
  §1 detect_difficulty_change 返回布尔值
  §2 命中时写入 difficulty_reestimate 事件
  §3 单元测试覆盖变与不变两种场景
  §4 不 import 任何学科包/学段包
"""
from __future__ import annotations

from typing import Any

import pytest

from src.core.instantiation.difficulty import (
    DifficultyReestimateEvent,
    detect_difficulty_change,
    emit_difficulty_reestimate,
)


# ────────────────────────────────────────────────────────────────────
# 测试夹具
# ────────────────────────────────────────────────────────────────────

def _make_template(
    *,
    slots: dict[str, dict[str, Any]] | None = None,
    template_version_id: str = "sha256:fixture-template-difficulty-test",
) -> dict[str, Any]:
    """构造测试用母题版本 dict.

    默认 slots：
      - a (int, difficulty_relevant=True)
      - b (int, difficulty_relevant=True)
      - c (string, difficulty_relevant=False)
    """
    if slots is None:
        slots = {
            "a": {"type": "int", "difficulty_relevant": True},
            "b": {"type": "int", "difficulty_relevant": True},
            "c": {"type": "string", "difficulty_relevant": False},
        }
    return {
        "template_version_id": template_version_id,
        "template_id": "tpl-difficulty-test",
        "dsl_version": "1",
        "spec": {
            "objective": {
                "kp_set": [{"dimension": "kp", "code": "math.nal.int.add"}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": "L",
                "graph_release": "2026.1",
            },
            "slots": slots,
            "variation_axes": {"axes": []},
            "presentation": {
                "blocks": [{"kind": "text", "template": "{a} + {b}"}]
            },
            "answer_program": {"expression": "a + b", "returns": "number"},
            "distractor_rules": {"rules": []},
        },
    }


_BASELINE = {"a": 3, "b": 4, "c": "hello"}


# ────────────────────────────────────────────────────────────────────
# §1 + §3 变更检测
# ────────────────────────────────────────────────────────────────────

class TestDetectDifficultyChange:
    """验收 §1 / §3：detect_difficulty_change 返回布尔值。"""

    def test_no_change_returns_false(self):
        """所有 difficulty_relevant 槽值相同 → False（不变场景）。"""
        template = _make_template()
        params = {"a": 3, "b": 4, "c": "hello"}  # 与 baseline 完全一致
        assert detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        ) is False

    def test_difficulty_relevant_slot_changed_returns_true(self):
        """difficulty_relevant 槽 a 变更 → True（变场景）。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}  # a 从 3→5
        assert detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        ) is True

    def test_non_difficulty_relevant_slot_changed_returns_false(self):
        """非 difficulty_relevant 槽 c 变更 → False。"""
        template = _make_template()
        params = {"a": 3, "b": 4, "c": "world"}  # c 变了但非 difficulty_relevant
        assert detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        ) is False

    def test_multiple_difficulty_relevant_changed_returns_true(self):
        """多个 difficulty_relevant 槽变更 → True。"""
        template = _make_template()
        params = {"a": 10, "b": 20, "c": "hello"}
        assert detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        ) is True

    def test_no_baseline_returns_false(self):
        """baseline_params=None → False（无基准无法检测）。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}
        assert detect_difficulty_change(
            template, params, baseline_params=None
        ) is False

    def test_no_difficulty_relevant_slots_returns_false(self):
        """母题无 difficulty_relevant 槽 → False。"""
        template = _make_template(
            slots={
                "a": {"type": "int", "difficulty_relevant": False},
                "b": {"type": "int", "difficulty_relevant": False},
            }
        )
        params = {"a": 100, "b": 200}
        baseline = {"a": 3, "b": 4}
        assert detect_difficulty_change(
            template, params, baseline_params=baseline
        ) is False

    def test_missing_slot_in_params_not_counted(self):
        """params 缺少某 difficulty_relevant 槽 → 不视为变更。"""
        template = _make_template()
        params = {"a": 3, "c": "hello"}  # 缺 b
        assert detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        ) is False


# ────────────────────────────────────────────────────────────────────
# §2 事件发布
# ────────────────────────────────────────────────────────────────────

class TestEmitDifficultyReestimate:
    """验收 §2：命中时写入 difficulty_reestimate 事件。"""

    def test_emit_event_on_change(self):
        """命中变更 → 发布事件，事件含正确字段。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}  # a 变了
        event = emit_difficulty_reestimate(
            template,
            params,
            item_version_id="sha256:item-version-001",
            pack_digest="sha256:pack-math",
            scene="practice",
            baseline_params=_BASELINE,
        )
        assert isinstance(event, DifficultyReestimateEvent)
        assert event.event_type == "difficulty_reestimate"
        assert event.item_version_id == "sha256:item-version-001"
        assert event.template_version_id == "sha256:fixture-template-difficulty-test"
        assert event.pack_digest == "sha256:pack-math"
        assert event.scene == "practice"
        assert event.changed_slots == ["a"]  # 仅 a 变更
        assert event.params == params
        assert event.baseline_params == _BASELINE
        assert event.created_at  # 非空

    def test_emit_raises_on_no_change(self):
        """无变更 → 抛 ValueError（不应发布事件）。"""
        template = _make_template()
        params = {"a": 3, "b": 4, "c": "hello"}  # 无变更
        with pytest.raises(ValueError, match="未检测到"):
            emit_difficulty_reestimate(
                template,
                params,
                item_version_id="sha256:item-version-001",
                pack_digest="sha256:pack-math",
                scene="practice",
                baseline_params=_BASELINE,
            )

    def test_emit_raises_on_no_baseline(self):
        """baseline_params=None → 抛 ValueError。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}
        with pytest.raises(ValueError, match="baseline_params"):
            emit_difficulty_reestimate(
                template,
                params,
                item_version_id="sha256:item-version-001",
                pack_digest="sha256:pack-math",
                scene="practice",
                baseline_params=None,
            )

    def test_event_schema_validates_sha256_prefix(self):
        """事件 schema 校验：item_version_id 必须 sha256: 前缀。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}
        with pytest.raises(Exception, match="sha256"):
            emit_difficulty_reestimate(
                template,
                params,
                item_version_id="invalid-id",  # 缺 sha256: 前缀
                pack_digest="sha256:pack-math",
                scene="practice",
                baseline_params=_BASELINE,
            )

    def test_event_schema_rejects_empty_changed_slots(self):
        """事件 schema 校验：changed_slots 不得为空。"""
        template = _make_template()
        # 手动构造一个 changed_slots=[] 的事件应失败
        with pytest.raises(Exception):
            DifficultyReestimateEvent(
                event_id="test-id",
                item_version_id="sha256:item-001",
                template_version_id="sha256:tpl-001",
                pack_digest="sha256:pack",
                changed_slots=[],  # 空
                params={},
                scene="practice",
                created_at="2026-07-27T00:00:00+00:00",
            )

    def test_redis_push_called(self):
        """提供 redis_client → 调用 rpush 推入队列。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}

        class FakeRedis:
            def __init__(self):
                self.pushed = []

            def rpush(self, queue, data):
                self.pushed.append((queue, data))

        fake_redis = FakeRedis()
        event = emit_difficulty_reestimate(
            template,
            params,
            item_version_id="sha256:item-001",
            pack_digest="sha256:pack-math",
            scene="measurement",
            baseline_params=_BASELINE,
            redis_client=fake_redis,
        )
        assert len(fake_redis.pushed) == 1
        queue, data = fake_redis.pushed[0]
        assert queue == "difficulty_reestimate"
        # data 是 JSON 字符串
        import json
        parsed = json.loads(data)
        assert parsed["event_type"] == "difficulty_reestimate"
        assert parsed["changed_slots"] == ["a"]

    def test_custom_event_id_and_timestamp(self):
        """自定义 event_id 和 created_at。"""
        template = _make_template()
        params = {"a": 5, "b": 4, "c": "hello"}
        event = emit_difficulty_reestimate(
            template,
            params,
            item_version_id="sha256:item-001",
            pack_digest="sha256:pack-math",
            scene="diagnosis",
            baseline_params=_BASELINE,
            event_id="custom-event-id",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert event.event_id == "custom-event-id"
        assert event.created_at == "2026-01-01T00:00:00+00:00"


# ────────────────────────────────────────────────────────────────────
# §4 学科无关校验
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """验收 §4：不 import 任何学科包/学段包。"""

    def test_difficulty_module_no_subject_imports(self):
        """difficulty 子包不 import 任何 subject/grade 包。"""
        import inspect
        from src.core.instantiation.difficulty import trigger

        source = inspect.getsource(trigger)
        forbidden = [
            "import subject",
            "import gradeband",
            "from subject",
            "from gradeband",
            "import packs",
        ]
        for token in forbidden:
            assert token not in source, f"difficulty 模块不得包含 {token!r}"


# ────────────────────────────────────────────────────────────────────
# 集成：检测 + 发布联动
# ────────────────────────────────────────────────────────────────────

class TestIntegration:
    """检测 + 发布联动场景。"""

    def test_detect_then_emit(self):
        """先检测变更，再发布事件（典型调用链）。"""
        template = _make_template()
        params = {"a": 99, "b": 4, "c": "hello"}

        # 1. 检测
        changed = detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        )
        assert changed is True

        # 2. 发布
        event = emit_difficulty_reestimate(
            template,
            params,
            item_version_id="sha256:item-001",
            pack_digest="sha256:pack-math",
            scene="practice",
            baseline_params=_BASELINE,
        )
        assert event.changed_slots == ["a"]

    def test_no_emit_when_not_changed(self):
        """无变更时不发布（调用方应判断）。"""
        template = _make_template()
        params = {"a": 3, "b": 4, "c": "hello"}

        changed = detect_difficulty_change(
            template, params, baseline_params=_BASELINE
        )
        assert changed is False

        # 调用方不应调用 emit（会抛 ValueError）
        with pytest.raises(ValueError):
            emit_difficulty_reestimate(
                template,
                params,
                item_version_id="sha256:item-001",
                pack_digest="sha256:pack-math",
                scene="practice",
                baseline_params=_BASELINE,
            )
