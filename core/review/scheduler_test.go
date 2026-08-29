package review

import (
	"errors"
	"testing"
	"time"
)

// 基准时刻固定，保证排程产物确定性可断言.
var base = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)

func boolPtr(b bool) *bool { return &b }

func event(id, ivID string, at time.Time, correct *bool, errTypes ...string) ReviewEventView {
	return ReviewEventView{
		EventID: id, ItemVersionID: ivID, CreatedAt: at,
		Correct: correct, ErrorTypeIDs: errTypes,
	}
}

// DeriveCorrectness 判据优先级：显式 bool > 错误推断非空 > 未知.
func TestDeriveCorrectness(t *testing.T) {
	tests := []struct {
		name       string
		trace      map[string]any
		inferences []map[string]any
		want       *bool
	}{
		{
			name:  "显式correct为真_优先采用",
			trace: map[string]any{"process": map[string]any{"correct": true}},
			want:  boolPtr(true),
		},
		{
			name:  "显式correct为假_即使有错误推断也优先",
			trace: map[string]any{"process": map[string]any{"correct": false}},
			inferences: []map[string]any{
				{"error_type_id": "e1"},
			},
			want: boolPtr(false),
		},
		{
			name:  "process存在但correct非bool_向下判",
			trace: map[string]any{"process": map[string]any{"correct": "yes"}},
			inferences: []map[string]any{
				{"error_type_id": "e1"},
			},
			want: boolPtr(false),
		},
		{
			name:  "无process_错误推断非空判错",
			trace: map[string]any{},
			inferences: []map[string]any{
				{"error_type_id": "e1"},
			},
			want: boolPtr(false),
		},
		{
			name:       "全缺_未知不猜",
			trace:      map[string]any{},
			inferences: nil,
			want:       nil,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := DeriveCorrectness(tt.trace, tt.inferences)
			switch {
			case tt.want == nil && got != nil:
				t.Fatalf("期望 nil，得到 %v", *got)
			case tt.want != nil && got == nil:
				t.Fatal("期望判定，得到 nil")
			case tt.want != nil && *got != *tt.want:
				t.Fatalf("期望 %v，得到 %v", *tt.want, *got)
			}
		})
	}
}

// 状态机核心迁移表（与冻结实现 apply_event 逐分支对齐）.
func TestApplyEvent(t *testing.T) {
	intervals := DefaultIntervalsDays() // [1, 3, 7, 21]
	enqueued := base
	stage1 := EntryState{
		Stage: 1, Status: StatusPending,
		EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 3),
		SourceErrorTypeID: "e-old", LastEventID: "ev0",
	}

	tests := []struct {
		name       string
		state      *EntryState
		event      ReviewEventView
		intervals  []int
		want       *EntryState
		wantErr    bool
		wantNoMove bool // 期望返回与入参同一状态（含 nil）
	}{
		{
			name:  "答错_不在队_入队stage0_due为1天后",
			state: nil,
			event: event("ev1", "iv", base, boolPtr(false), "e1", "e2"),
			want: &EntryState{
				Stage: 0, Status: StatusPending,
				EnqueuedAt: base, DueAt: base.AddDate(0, 0, 1),
				SourceErrorTypeID: "e1", LastEventID: "ev1",
			},
		},
		{
			name:  "答错_不在队_无归因",
			state: nil,
			event: event("ev1", "iv", base, boolPtr(false)),
			want: &EntryState{
				Stage: 0, Status: StatusPending,
				EnqueuedAt: base, DueAt: base.AddDate(0, 0, 1),
				SourceErrorTypeID: "", LastEventID: "ev1",
			},
		},
		{
			name:  "答错_在队_重置stage0_保留首次入队时刻",
			state: &stage1,
			event: event("ev2", "iv", base.AddDate(0, 0, 5), boolPtr(false), "e-new"),
			want: &EntryState{
				Stage: 0, Status: StatusPending,
				EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 5).AddDate(0, 0, 1),
				SourceErrorTypeID: "e-new", LastEventID: "ev2",
			},
		},
		{
			name:  "答错_已done_重新入队_入队时刻仍保留done前的",
			state: &EntryState{Stage: 3, Status: StatusDone, EnqueuedAt: enqueued, DueAt: base, LastEventID: "ev0"},
			event: event("ev3", "iv", base.AddDate(0, 0, 30), boolPtr(false)),
			want: &EntryState{
				Stage: 0, Status: StatusPending,
				EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 30).AddDate(0, 0, 1),
				SourceErrorTypeID: "", LastEventID: "ev3",
			},
		},
		{
			name:  "答对_在队_推进stage_due重算",
			state: &stage1,
			event: event("ev4", "iv", base.AddDate(0, 0, 4), boolPtr(true)),
			want: &EntryState{
				Stage: 2, Status: StatusPending,
				EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 4).AddDate(0, 0, 7),
				SourceErrorTypeID: "e-old", LastEventID: "ev4",
			},
		},
		{
			name:  "答对_走完末间隔_出队done_保留stage与due",
			state: &EntryState{Stage: 3, Status: StatusPending, EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 21), LastEventID: "ev0"},
			event: event("ev5", "iv", base.AddDate(0, 0, 21), boolPtr(true)),
			want: &EntryState{
				Stage: 3, Status: StatusDone,
				EnqueuedAt: enqueued, DueAt: base.AddDate(0, 0, 21),
				SourceErrorTypeID: "", LastEventID: "ev5",
			},
		},
		{
			name:       "答对_不在队_忽略",
			state:      nil,
			event:      event("ev6", "iv", base, boolPtr(true)),
			wantNoMove: true,
		},
		{
			name:       "对错未知_在队_忽略",
			state:      &stage1,
			event:      event("ev7", "iv", base, nil),
			wantNoMove: true,
		},
		{
			name:       "对错未知_不在队_仍不在队",
			state:      nil,
			event:      event("ev8", "iv", base, nil),
			wantNoMove: true,
		},
		{
			name:      "空间隔表_报错",
			state:     nil,
			event:     event("ev9", "iv", base, boolPtr(false)),
			intervals: []int{},
			wantErr:   true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			iv := tt.intervals
			if iv == nil && !tt.wantErr {
				iv = intervals
			}
			got, err := ApplyEvent(tt.state, tt.event, iv)
			if tt.wantErr {
				if !errors.Is(err, ErrInvalidIntervals) {
					t.Fatalf("期望 ErrInvalidIntervals，得到 %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("意外报错: %v", err)
			}
			if tt.wantNoMove {
				if got != tt.state {
					t.Fatalf("期望原状态原样返回（含 nil），得到 %+v", got)
				}
				return
			}
			if got == nil || *got != *tt.want {
				t.Fatalf("状态迁移错:\n got:  %+v\nwant: %+v", got, *tt.want)
			}
		})
	}
}

// 到期边界：未来/今天/过期（get_due_reviews 判据 due_at <= now 的纯函数面）.
func TestDueReviewsBoundaries(t *testing.T) {
	intervals := DefaultIntervalsDays()
	wrong := event("ev-w", "iv", base, boolPtr(false))
	state, err := ApplyEvent(nil, wrong, intervals)
	if err != nil {
		t.Fatalf("入队: %v", err)
	}
	if !state.DueAt.Equal(base.AddDate(0, 0, 1)) {
		t.Fatalf("due 应为次日: %v", state.DueAt)
	}

	dueAt := state.DueAt
	before := dueAt.Add(-time.Nanosecond)
	after := dueAt.Add(time.Nanosecond)

	states := map[string]EntryState{"iv": *state}

	// 未来：不可取
	if got := DueReviews(states, before); len(got) != 0 {
		t.Fatalf("到期前不应可取: %+v", got)
	}
	// 今天（恰到期时刻）：可取——「今天到期今天取」
	got := DueReviews(states, dueAt)
	if len(got) != 1 || got[0].ItemVersionID != "iv" {
		t.Fatalf("恰到期应可取: %+v", got)
	}
	// 过期：可取
	if got := DueReviews(states, after); len(got) != 1 {
		t.Fatalf("过期应可取: %+v", got)
	}
}

// 到期排序：最逾期优先；同刻按 item_version_id 字典序兜底（确定性）；done 不取.
func TestDueReviewsOrdering(t *testing.T) {
	states := map[string]EntryState{
		"iv-b": {Stage: 0, Status: StatusPending, DueAt: base.AddDate(0, 0, -5)},
		"iv-a": {Stage: 0, Status: StatusPending, DueAt: base.AddDate(0, 0, -5)},
		"iv-c": {Stage: 0, Status: StatusPending, DueAt: base.AddDate(0, 0, -1)},
		"iv-d": {Stage: 0, Status: StatusPending, DueAt: base.AddDate(0, 0, 2)}, // 未到期
		"iv-e": {Stage: 3, Status: StatusDone, DueAt: base.AddDate(0, 0, -9)},   // done 不取
	}
	got := DueReviews(states, base)
	if len(got) != 3 {
		t.Fatalf("应取 3 条: %+v", got)
	}
	wantOrder := []string{"iv-a", "iv-b", "iv-c"}
	for i, want := range wantOrder {
		if got[i].ItemVersionID != want {
			t.Fatalf("排序错（最逾期优先，同刻字典序）[%d]: got %s want %s", i, got[i].ItemVersionID, want)
		}
	}
}

// 全量重放：错题入队→推进→出队→再错重入，逐段断言状态；重放两次结果一致（可重建性）.
func TestRebuildQueueLifecycle(t *testing.T) {
	intervals := DefaultIntervalsDays() // [1, 3, 7, 21]
	events := []ReviewEventView{
		event("e1", "iv-1", base, boolPtr(false), "e-calc"),                   // 错 → 入队
		event("e2", "iv-1", base.AddDate(0, 0, 1), boolPtr(true)),             // 对 → stage1, due +3
		event("e3", "iv-1", base.AddDate(0, 0, 4), boolPtr(true)),             // 对 → stage2, due +7
		event("e4", "iv-1", base.AddDate(0, 0, 11), boolPtr(true)),            // 对 → stage3, due +21
		event("e5", "iv-1", base.AddDate(0, 0, 32), boolPtr(true)),            // 对 → done
		event("e6", "iv-2", base.AddDate(0, 0, 40), boolPtr(false), "e-read"), // 第二题入队
		event("e7", "iv-1", base.AddDate(0, 0, 45), boolPtr(false), "e-calc"), // 第一题再错 → 重置
	}
	states, err := RebuildQueue(events, intervals)
	if err != nil {
		t.Fatalf("RebuildQueue: %v", err)
	}
	if len(states) != 2 {
		t.Fatalf("应两条在队史: %d", len(states))
	}
	s1 := states["iv-1"]
	if s1.Stage != 0 || s1.Status != StatusPending || s1.SourceErrorTypeID != "e-calc" ||
		s1.LastEventID != "e7" {
		t.Fatalf("iv-1 最终态错: %+v", s1)
	}
	// 入队时刻保留首次（e1），due 按最后一次错题事件（e7）+1 天
	if !s1.EnqueuedAt.Equal(base) {
		t.Fatalf("enqueued_at 应保留首次入队: %v", s1.EnqueuedAt)
	}
	if !s1.DueAt.Equal(base.AddDate(0, 0, 45+1)) {
		t.Fatalf("due 应为 e7+1 天: %v", s1.DueAt)
	}
	s2 := states["iv-2"]
	if s2.Stage != 0 || s2.Status != StatusPending || !s2.DueAt.Equal(base.AddDate(0, 0, 40+1)) {
		t.Fatalf("iv-2 错: %+v", s2)
	}

	// 重放确定性：同一事件流 + 同一策略 → 同态
	replay, err := RebuildQueue(events, intervals)
	if err != nil {
		t.Fatalf("重放: %v", err)
	}
	if len(replay) != len(states) {
		t.Fatalf("重放条数不一致")
	}
	for id, st := range states {
		if replay[id] != st {
			t.Fatalf("重放不同态 %s:\n %+v\n %+v", id, st, replay[id])
		}
	}

	// 答对/未知事件从不产生条目
	correctOnly := []ReviewEventView{
		event("c1", "iv-3", base, boolPtr(true)),
		event("c2", "iv-4", base, nil),
	}
	empty, err := RebuildQueue(correctOnly, intervals)
	if err != nil {
		t.Fatalf("RebuildQueue: %v", err)
	}
	if len(empty) != 0 {
		t.Fatalf("答对/未知不应产生条目: %+v", empty)
	}
}

// 队列条目类型：由纯函数核状态装配 + IsDue 判据.
func TestReviewEntry(t *testing.T) {
	intervals := DefaultIntervalsDays()
	state, err := ApplyEvent(nil, event("e1", "iv-1", base, boolPtr(false), "e-calc"), intervals)
	if err != nil {
		t.Fatalf("入队: %v", err)
	}
	entry := NewReviewEntry("entry-1", "student-1", "iv-1", DefaultPolicyID, DefaultPolicyVersion, *state)
	if entry.EntryID != "entry-1" || entry.StudentAliasID != "student-1" ||
		entry.ItemVersionID != "iv-1" || entry.PolicyID != "fixed-interval" ||
		entry.PolicyVersion != "1.0.0" || entry.Stage != 0 || entry.Status != StatusPending ||
		entry.SourceErrorTypeID != "e-calc" || entry.LastEventID != "e1" ||
		!entry.EnqueuedAt.Equal(base) || !entry.DueAt.Equal(base.AddDate(0, 0, 1)) {
		t.Fatalf("条目装配错: %+v", entry)
	}
	if !entry.IsDue(entry.DueAt) {
		t.Fatal("恰到期应可取")
	}
	if entry.IsDue(entry.DueAt.Add(-time.Nanosecond)) {
		t.Fatal("到期前不应可取")
	}
	entry.Status = StatusDone
	if entry.IsDue(entry.DueAt.Add(time.Hour)) {
		t.Fatal("done 不应可取")
	}
}
