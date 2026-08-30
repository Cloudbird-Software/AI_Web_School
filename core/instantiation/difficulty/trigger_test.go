// trigger_test.go 难度重估触发器的验收测试（T-W2-006）。
//
// 覆盖（对齐验收 §1/§2）：
//  1. DetectDifficultyChange：difficulty_relevant 槽变更命中 / 未变更 /
//     无基准 / 仅非 relevant 槽变更 / 缺槽不误报；
//  2. EmitDifficultyReestimate：命中产事件（schema 校验通过），
//     无变更 / 无基准拒绝；字段校验（changed_slots 非空、sha256: 前缀、scene 枚举）；
//  3. 确定性：同输入必得同一 changed_slots 顺序（排序稳定化）。
package difficulty

import (
	"strings"
	"testing"
)

func triggerTemplate() map[string]any {
	return map[string]any{
		"template_version_id": "sha256:tpl-version-0000000000000000000000000000000000000000000000000000000000",
		"template_id":         "tpl-difficulty",
		"spec": map[string]any{
			"objective": map[string]any{
				"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.int.add"}},
				"kp_set_mode":     "single",
				"cognitive_level": "apply",
				"gradeband":       "L",
				"graph_release":   "2026.1",
			},
			"slots": map[string]any{
				"a":   map[string]any{"type": "int", "difficulty_relevant": true},
				"b":   map[string]any{"type": "int", "difficulty_relevant": true},
				"col": map[string]any{"type": "int", "difficulty_relevant": false},
			},
			"variation_axes":   map[string]any{"axes": []any{}},
			"presentation":     map[string]any{"blocks": []any{}},
			"answer_program":   map[string]any{"expression": "a + b", "returns": "number"},
			"distractor_rules": map[string]any{"rules": []any{}},
		},
	}
}

func TestDetectDifficultyChange(t *testing.T) {
	tpl := triggerTemplate()
	base := map[string]any{"a": 3, "b": 4, "col": 1}

	hit, err := DetectDifficultyChange(tpl, map[string]any{"a": 5, "b": 4, "col": 1}, base)
	if err != nil || !hit {
		t.Errorf("relevant 槽变更应命中: hit=%v err=%v", hit, err)
	}
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 3, "b": 4, "col": 2}, base)
	if err != nil || hit {
		t.Errorf("仅非 relevant 槽变更不应命中: hit=%v err=%v", hit, err)
	}
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 3, "b": 4, "col": 1}, base)
	if err != nil || hit {
		t.Errorf("完全相同不应命中: hit=%v err=%v", hit, err)
	}
	// 无基准
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 9}, nil)
	if err != nil || hit {
		t.Errorf("无基准不应命中: hit=%v err=%v", hit, err)
	}
	// 缺槽视为未变更（避免误报）
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 9}, base)
	if err != nil || !hit {
		t.Errorf("a 变更应命中: hit=%v err=%v", hit, err)
	}
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 3, "b": 4}, base)
	if err != nil || hit {
		t.Errorf("col 缺失不应误报: hit=%v err=%v", hit, err)
	}
	// 跨类型数值相等：1 与 1.0 不算变更
	hit, err = DetectDifficultyChange(tpl, map[string]any{"a": 3, "b": 4.0}, base)
	if err != nil || hit {
		t.Errorf("1 != 1.0 为 False（Python 语义），不应命中: hit=%v err=%v", hit, err)
	}
}

func TestEmitDifficultyReestimate(t *testing.T) {
	tpl := triggerTemplate()
	base := map[string]any{"a": 3, "b": 4, "col": 1}
	params := map[string]any{"a": 5, "b": 4, "col": 2}

	ev, err := EmitDifficultyReestimate(tpl, params, base, EmitOptions{
		ItemVersionID: "sha256:iv-0000000000000000000000000000000000000000000000000000000000",
		PackDigest:    "sha256:pack-0000000000000000000000000000000000000000000000000000000000",
		Scene:         ScenePractice,
		EventID:       "evt-1",
		CreatedAt:     "2026-07-27T00:00:00+00:00",
	})
	if err != nil {
		t.Fatalf("发事件失败: %v", err)
	}
	if ev.EventType != "difficulty_reestimate" {
		t.Errorf("event_type = %q", ev.EventType)
	}
	// 只有 relevant 槽进 changed_slots（col 变更但不 relevant）
	if len(ev.ChangedSlots) != 1 || ev.ChangedSlots[0] != "a" {
		t.Errorf("changed_slots = %v, 期望 [a]", ev.ChangedSlots)
	}
	if ev.TemplateVersionID != tpl["template_version_id"] {
		t.Errorf("template_version_id = %q", ev.TemplateVersionID)
	}
	if ev.Scene != ScenePractice {
		t.Errorf("scene = %q", ev.Scene)
	}
	// schema 校验面：构造期已通过（validate 内嵌于 Emit）
	if err := ev.validate(); err != nil {
		t.Errorf("事件 schema 校验失败: %v", err)
	}
}

func TestEmitDeterministicChangedSlots(t *testing.T) {
	tpl := triggerTemplate()
	base := map[string]any{"a": 3, "b": 4, "col": 1}
	params := map[string]any{"a": 9, "b": 8, "col": 1}
	var prev []string
	for range 8 {
		ev, err := EmitDifficultyReestimate(tpl, params, base, EmitOptions{
			ItemVersionID: "sha256:iv",
			PackDigest:    "sha256:pd",
			Scene:         SceneDiagnosis,
		})
		if err != nil {
			t.Fatalf("发事件失败: %v", err)
		}
		if prev != nil && strings.Join(prev, ",") != strings.Join(ev.ChangedSlots, ",") {
			t.Fatalf("changed_slots 顺序漂移: %v vs %v", prev, ev.ChangedSlots)
		}
		prev = ev.ChangedSlots
	}
}

func TestEmitFailClosed(t *testing.T) {
	tpl := triggerTemplate()
	base := map[string]any{"a": 3, "b": 4}
	same := map[string]any{"a": 3, "b": 4}
	ivID := "sha256:iv-0000000000000000000000000000000000000000000000000000000000"
	pd := "sha256:pack-0000000000000000000000000000000000000000000000000000000000"

	// 无变更槽
	if _, err := EmitDifficultyReestimate(tpl, same, base, EmitOptions{ItemVersionID: ivID, PackDigest: pd, Scene: ScenePractice}); err == nil {
		t.Errorf("无变更槽应拒绝")
	}
	// 无基准
	if _, err := EmitDifficultyReestimate(tpl, map[string]any{"a": 5}, nil, EmitOptions{ItemVersionID: ivID, PackDigest: pd, Scene: ScenePractice}); err == nil {
		t.Errorf("无基准应拒绝")
	}
	// 非 sha256: 前缀的 id
	if _, err := EmitDifficultyReestimate(tpl, map[string]any{"a": 9}, base, EmitOptions{ItemVersionID: "not-a-digest", PackDigest: pd, Scene: ScenePractice}); err == nil {
		t.Errorf("item_version_id 缺 sha256: 前缀应拒绝")
	}
	// 非法 scene
	if _, err := EmitDifficultyReestimate(tpl, map[string]any{"a": 9}, base, EmitOptions{ItemVersionID: ivID, PackDigest: pd, Scene: Scene("mixed")}); err == nil {
		t.Errorf("非法 scene 应拒绝（D5 分场景）")
	}
}
