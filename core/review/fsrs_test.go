package review

import (
	"testing"
	"time"
)

var fsrsBase = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)

// 入口纪律与 legacy 一致：答错入队、答对不在队忽略、未知忽略.
func TestRebuildQueueFSEntryDiscipline(t *testing.T) {
	pol := NewFSRSPolicy()
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-calc"),           // 错 → 入队
		event("e2", "iv-2", fsrsBase, boolPtr(true)),                      // 对，不在队 → 忽略
		event("e3", "iv-3", fsrsBase, nil),                                // 未知 → 忽略
		event("e4", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true), ""), // 对，在队 → 推进
	}
	states, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("RebuildQueueFSRS: %v", err)
	}
	if len(states) != 1 {
		t.Fatalf("应仅 iv-1 一条在队: %d", len(states))
	}
	s := states["iv-1"]
	if s.Status != StatusPending || s.Stage != 1 || s.LastEventID != "e4" ||
		s.SourceErrorTypeID != "e-calc" {
		t.Fatalf("iv-1 态错: %+v", s)
	}
	// 入队时刻保留首次（e1），不因答对推进而改变
	if !s.EnqueuedAt.Equal(fsrsBase) {
		t.Fatalf("enqueued_at 应保留首次入队: %v", s.EnqueuedAt)
	}
}

// 答错重置：在队 pending 时答错 → Again 重置稳定性，Stage 不增加，归因刷新.
func TestRebuildQueueFSRSLapseResets(t *testing.T) {
	pol := NewFSRSPolicy()
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-A"),            // 入队
		event("e2", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true)),    // Good → stage1
		event("e3", "iv-1", fsrsBase.AddDate(0, 0, 10), boolPtr(false), "e-B"), // Again → 重置
	}
	states, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("RebuildQueueFSRS: %v", err)
	}
	s := states["iv-1"]
	// 答错不增加成功计数：stage 回到 0（e1 错 + e3 错，仅 e2 成功）
	if s.Stage != 1 {
		t.Fatalf("lapse 后 stage 应为 1（仅一次成功）: %d", s.Stage)
	}
	if s.SourceErrorTypeID != "e-B" {
		t.Fatalf("归因应刷新为 e-B: %s", s.SourceErrorTypeID)
	}
	if !s.EnqueuedAt.Equal(fsrsBase) {
		t.Fatalf("重置应保留首次入队: %v", s.EnqueuedAt)
	}
	if s.LastEventID != "e3" {
		t.Fatalf("last_event 应为 e3: %s", s.LastEventID)
	}
}

// 可重放性（核心验收）：同一事件流 + 同一策略 → 同态。go-fsrs 无 fuzz/全局
// 随机源，重放必同——两次独立调用结果按字段全等.
func TestRebuildQueueFSRSReplayable(t *testing.T) {
	pol := NewFSRSPolicy()
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-calc"),
		event("e2", "iv-2", fsrsBase, boolPtr(false), "e-read"),
		event("e3", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true)),
		event("e4", "iv-2", fsrsBase.AddDate(0, 0, 2), boolPtr(true)),
		event("e5", "iv-1", fsrsBase.AddDate(0, 0, 5), boolPtr(true)),
		event("e6", "iv-2", fsrsBase.AddDate(0, 0, 9), boolPtr(false), "e-read2"),
		event("e7", "iv-1", fsrsBase.AddDate(0, 0, 20), boolPtr(true)),
	}
	first, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	// 第二次用全新策略实例（同默认参数），验证与随机源/时间无关
	second, err := RebuildQueueFSRS(events, NewFSRSPolicy())
	if err != nil {
		t.Fatalf("second: %v", err)
	}
	if len(first) != len(second) {
		t.Fatalf("重放条数不一致: %d vs %d", len(first), len(second))
	}
	for id, want := range first {
		if got, ok := second[id]; !ok {
			t.Fatalf("重放缺失 %s", id)
		} else if got != want {
			t.Fatalf("重放不同态 %s:\n got %+v\nwant %+v", id, got, want)
		}
	}
}

// 到期单调性（核心验收）：连续答对序列的到期日非递减。FSRS 学习态→复习态
// 稳定性增长，间隔单调递增.
func TestRebuildQueueFSRSDueMonotonic(t *testing.T) {
	pol := NewFSRSPolicy()
	// 入队后连续答对：错 → 对 → 对 → 对 → 对
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-calc"),
		event("e2", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true)),
		event("e3", "iv-1", fsrsBase.AddDate(0, 0, 5), boolPtr(true)),
		event("e4", "iv-1", fsrsBase.AddDate(0, 0, 20), boolPtr(true)),
		event("e5", "iv-1", fsrsBase.AddDate(0, 0, 50), boolPtr(true)),
	}
	states, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("RebuildQueueFSRS: %v", err)
	}
	s := states["iv-1"]
	// 最终到期日必须晚于首次入队（间隔推远）
	if !s.DueAt.After(fsrsBase) {
		t.Fatalf("最终 due 应晚于入队: %v", s.DueAt)
	}
	// 逐事件追踪 due 单调非递减
	var prev time.Time
	for i := range events {
		snap, err := RebuildQueueFSRS(events[:i+1], pol)
		if err != nil {
			t.Fatalf("prefix[%d]: %v", i, err)
		}
		if st, ok := snap["iv-1"]; ok {
			if !prev.IsZero() && st.DueAt.Before(prev) {
				t.Fatalf("due 非单调: events[%d] due=%v < prev=%v", i, st.DueAt, prev)
			}
			prev = st.DueAt
		}
	}
	_ = s
}

// 策略版本化并存：fsrs/1.0.0 与 fixed-interval/1.0.0 独立产出，互不干扰.
func TestPolicyVersionCoexistence(t *testing.T) {
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-calc"),
		event("e2", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true)),
		event("e3", "iv-1", fsrsBase.AddDate(0, 0, 5), boolPtr(true)),
	}
	fsrsStates, err := RebuildQueueFSRS(events, NewFSRSPolicy())
	if err != nil {
		t.Fatalf("fsrs: %v", err)
	}
	legacyStates, err := RebuildQueue(events, DefaultIntervalsDays())
	if err != nil {
		t.Fatalf("legacy: %v", err)
	}
	if len(fsrsStates) != len(legacyStates) {
		t.Fatalf("条数应一致: fsrs=%d legacy=%d", len(fsrsStates), len(legacyStates))
	}
	// 两种策略到期日不同（FSRS 非固定间隔），但都有效（晚于入队）
	fsrsDue := fsrsStates["iv-1"].DueAt
	legacyDue := legacyStates["iv-1"].DueAt
	if fsrsDue.Equal(legacyDue) {
		t.Logf("fsrs due=%v legacy due=%v（相等属巧合，非错误）", fsrsDue, legacyDue)
	}
	if !fsrsDue.After(fsrsBase) || !legacyDue.After(fsrsBase) {
		t.Fatalf("两策略 due 均应晚于入队: fsrs=%v legacy=%v", fsrsDue, legacyDue)
	}
	// 队列条目可装配为 fsrs/1.0.0 版本
	entry := NewReviewEntry("entry-1", "stu-1", "iv-1", FSRSPolicyID, FSRSPolicyVersion, fsrsStates["iv-1"])
	if entry.PolicyID != "fsrs" || entry.PolicyVersion != "1.0.0" {
		t.Fatalf("策略版本装配错: %s/%s", entry.PolicyID, entry.PolicyVersion)
	}
	if entry.Stage != fsrsStates["iv-1"].Stage {
		t.Fatalf("stage 装配错")
	}
}

// 答对/未知事件从不产生条目（与 legacy 同判据）.
func TestRebuildQueueFSRSNoEntryOnCorrectOrUnknown(t *testing.T) {
	pol := NewFSRSPolicy()
	events := []ReviewEventView{
		event("c1", "iv-1", fsrsBase, boolPtr(true)),
		event("c2", "iv-2", fsrsBase, nil),
	}
	states, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("RebuildQueueFSRS: %v", err)
	}
	if len(states) != 0 {
		t.Fatalf("答对/未知不应产生条目: %+v", states)
	}
}

// 多题独立：两题事件交错，各自状态机独立演进.
func TestRebuildQueueFSRSMultiItem(t *testing.T) {
	pol := NewFSRSPolicy()
	events := []ReviewEventView{
		event("e1", "iv-1", fsrsBase, boolPtr(false), "e-A"),
		event("e2", "iv-2", fsrsBase, boolPtr(false), "e-B"),
		event("e3", "iv-1", fsrsBase.AddDate(0, 0, 1), boolPtr(true)),
		event("e4", "iv-2", fsrsBase.AddDate(0, 0, 3), boolPtr(true)),
	}
	states, err := RebuildQueueFSRS(events, pol)
	if err != nil {
		t.Fatalf("RebuildQueueFSRS: %v", err)
	}
	if len(states) != 2 {
		t.Fatalf("应两条: %d", len(states))
	}
	// 各答对一次 → stage 均为 1
	if states["iv-1"].Stage != 1 || states["iv-2"].Stage != 1 {
		t.Fatalf("stage 应为 1: iv-1=%d iv-2=%d", states["iv-1"].Stage, states["iv-2"].Stage)
	}
	if states["iv-1"].SourceErrorTypeID != "e-A" || states["iv-2"].SourceErrorTypeID != "e-B" {
		t.Fatalf("归因错")
	}
}
