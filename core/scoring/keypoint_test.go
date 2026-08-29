package scoring

import (
	"context"
	"errors"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// keypoint_hit 套件：子串/re: 正则命中、规范化命中、未命中推断、min_pass
// 口径、blanks 拼接确定性、配置违例 fail-loud.

func kpParams(keypoints ...map[string]any) map[string]any {
	return map[string]any{"keypoints": toAnySlice(keypoints)}
}

func toAnySlice(ms []map[string]any) []any {
	out := make([]any, 0, len(ms))
	for _, m := range ms {
		out = append(out, m)
	}
	return out
}

// TestKeypointHitScoring 命中判定的正/负例与边界.
func TestKeypointHitScoring(t *testing.T) {
	s := NewKeypointHitScorer()
	ctx := context.Background()

	t.Run("子串命中全对", func(t *testing.T) {
		res, err := s.Score(ctx, "植物通过光合作用制造养分",
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"光合作用"}, "score": 2.0}))
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("re: 正则命中", func(t *testing.T) {
		res, err := s.Score(ctx, "编号是 1234 号",
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"re:1[0-9]{3}"}, "score": 1.0}))
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("多模式取首个命中", func(t *testing.T) {
		res, err := s.Score(ctx, "月亮绕着地球转",
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"太阳", "re:月[亮亮]"}, "score": 1.0}))
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		matched := evidenceMap(t, res)["keypoints"].([]any)[0].(map[string]any)["matched_pattern"]
		if matched != "re:月[亮亮]" {
			t.Fatalf("matched_pattern=%v", matched)
		}
	})
	t.Run("全角作答经全角归一命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"text":"答案１２３"}`,
			map[string]any{
				"keypoints":     []any{map[string]any{"id": "kp1", "patterns": []any{"123"}, "score": 1.0}},
				"normalization": map[string]any{"fullwidth_to_half": true},
			})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("未命中判错并推断错误类型", func(t *testing.T) {
		res, err := s.Score(ctx, "答非所问",
			kpParams(
				map[string]any{"id": "kp1", "patterns": []any{"关键词"}, "score": 1.0},
				map[string]any{"id": "kp2", "patterns": []any{"要点"}, "score": 1.0, "error_type_id": "err.missed.point"},
			))
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		ev := evidenceMap(t, res)
		infs := ev["error_inferences"].([]any)
		if len(infs) != 1 {
			t.Fatalf("应恰一条推断: %v", ev)
		}
		inf := infs[0].(map[string]any)
		if inf["error_type_id"] != "err.missed.point" {
			t.Fatalf("error_type_id=%v", inf["error_type_id"])
		}
		if inf["confidence"] != float64(DefaultKeypointInferConfidence) {
			t.Fatalf("默认推断置信度=%v", inf["confidence"])
		}
		if inf["evidence"].(map[string]any)["missed_keypoint"] != "kp2" {
			t.Fatalf("missed_keypoint=%v", inf)
		}
	})
	t.Run("推断置信度可覆盖", func(t *testing.T) {
		res, err := s.Score(ctx, "答非所问",
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"关键词"}, "score": 1.0,
				"error_type_id": "err.x", "confidence": 0.5}))
		if err != nil {
			t.Fatal(err)
		}
		inf := evidenceMap(t, res)["error_inferences"].([]any)[0].(map[string]any)
		if inf["confidence"] != 0.5 {
			t.Fatalf("confidence=%v", inf["confidence"])
		}
	})
	t.Run("min_pass 达线算对（部分命中）", func(t *testing.T) {
		params := kpParams(
			map[string]any{"id": "kp1", "patterns": []any{"甲"}, "score": 1.0},
			map[string]any{"id": "kp2", "patterns": []any{"乙"}, "score": 1.0},
			map[string]any{"id": "kp3", "patterns": []any{"丙"}, "score": 1.0},
		)
		params["min_pass"] = 2.0
		res, err := s.Score(ctx, "甲乙", params)
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("min_pass 未达线判错", func(t *testing.T) {
		params := kpParams(
			map[string]any{"id": "kp1", "patterns": []any{"甲"}, "score": 1.0},
			map[string]any{"id": "kp2", "patterns": []any{"乙"}, "score": 1.0},
		)
		params["min_pass"] = 2.0
		res, err := s.Score(ctx, "甲", params)
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("缺省口径：全部命中才算对", func(t *testing.T) {
		params := kpParams(
			map[string]any{"id": "kp1", "patterns": []any{"甲"}, "score": 1.0},
			map[string]any{"id": "kp2", "patterns": []any{"乙"}, "score": 1.0},
		)
		res, err := s.Score(ctx, "甲", params)
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("blanks 拼接文本（键序确定）", func(t *testing.T) {
		res, err := s.Score(ctx, `{"blanks":{"b2":"香蕉","b1":"苹果"}}`,
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"苹果 香蕉"}, "score": 1.0}))
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("正则方言越界显式失败", func(t *testing.T) {
		_, err := s.Score(ctx, "任意",
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"re:(a\\1"}, "score": 1.0}))
		if !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
	t.Run("keypoints 为空配置显式失败", func(t *testing.T) {
		if _, err := s.Score(ctx, "任意", map[string]any{"keypoints": []any{}}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
}

// TestKeypointHitThroughRunner 注册表面端到端（契约校验 + trace 证据随行）.
func TestKeypointHitThroughRunner(t *testing.T) {
	tb := registry.NewScorerTable()
	if err := tb.Register("keypoint_hit", NewKeypointHitScorer()); err != nil {
		t.Fatal(err)
	}
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatal(err)
	}
	run, err := r.Run(context.Background(), RunInput{
		ScorerID: "keypoint_hit",
		Answer:   "光合作用",
		Params:   kpParams(map[string]any{"id": "kp1", "patterns": []any{"光合作用"}, "score": 2.0}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !run.Result.Correct || run.ScorerVersion != versionKeypointHit {
		t.Fatalf("run=%+v", run)
	}
	if _, ok := run.Trace["evidence"]; !ok {
		t.Fatalf("证据应随 trace 落账: %v", run.Trace)
	}
}
