// scheduler.go 承载复习排程纯函数核（W3 S6；Python 冻结实现
// src/core/review/scheduler.py 的 Go 重锚定）。
//
// 全部函数无副作用、无 IO：输入 = 按时间序的作答事件视图 + 固定间隔表，
// 输出 = 每题一条的队列状态机。同一事件流 + 同一策略版本重放必得同态，
// 这是「队列版本可重建」（R-Z-07 / 架构 §4.4）的实现根基。
//
// 状态机语义（每 学生×题目 一条，与冻结实现逐条对齐）：
//   - 答错（含错误推断的事件）→ 入队或重置：stage=0，due = 事件时刻 +
//     intervals[0]；enqueued_at 保留首次入队时刻（重置不改入队时间）；
//   - 答对（在队 pending）→ 推进：stage+1，due = 事件时刻 + intervals[stage+1]；
//     越过最后一个间隔 → done（出队；stage 与 due_at 保持原值——冻结实现
//     replace 只改 status/last_event_id）；
//   - 答对但不在队 / 已 done → 忽略（不重新入队——答对不是错题）；
//   - 对错无法判定（Correct=nil）→ 忽略（评分轨迹缺 correctness 且无任何
//     错误推断时，v1 不做猜测性归因——宁可不排程也不伪造证据）。
package review

import (
	"errors"
	"fmt"
	"sort"
	"time"
)

// ErrInvalidIntervals 是策略间隔表非法的哨兵错误.
var ErrInvalidIntervals = errors.New("review: 策略间隔表非法")

// 策略默认种子（迁移 0010 内置的 v1 策略标识，与冻结实现常量一致）.
const (
	DefaultPolicyID      = "fixed-interval"
	DefaultPolicyVersion = "1.0.0"
)

// 队列状态域（与迁移 0010 ck_review_queue_entry_status_domain 对齐）.
const (
	StatusPending = "pending"
	StatusDone    = "done"
)

// DefaultIntervalsDays 返回 v1 策略默认间隔表（[1, 3, 7, 21] 天）的副本，
// 防调用方改写共享底层数组.
func DefaultIntervalsDays() []int {
	return []int{1, 3, 7, 21}
}

// ReviewEventView 是作答事件的排程视图（response_event 的最小投影，
// 对应冻结实现 ReviewEventView dataclass）。
//
// Correct=nil 表示本事件无法判定对错，ApplyEvent 将忽略之。
// ErrorTypeIDs 仅用于记录入队/重置时的主要归因（取首个），不参与判定。
// EventID 为 UUID 文本形态（Python UUID 类型的序列化等价物）。
// CreatedAt 为事件时刻（UTC；对应 DB DateTime(timezone=True) 列）。
type ReviewEventView struct {
	EventID       string
	ItemVersionID string
	CreatedAt     time.Time
	Correct       *bool
	ErrorTypeIDs  []string
}

// EntryState 是单题队列状态（值语义——状态迁移产出新实例，便于纯函数测试；
// 对应冻结实现 frozen dataclass EntryState）。
type EntryState struct {
	Stage             int
	Status            string // StatusPending | StatusDone
	EnqueuedAt        time.Time
	DueAt             time.Time
	SourceErrorTypeID string // 空 = 无归因（事件未带错误推断）
	LastEventID       string // UUID 文本
}

// DeriveCorrectness 从契约 §3/§4 结构推导事件对错（nil=无法判定）。
//
// 优先级：scoring_trace.process.correct（显式 bool）> 错误推断非空 ⇒ 答错
// > nil（未知，不猜）。与冻结实现 derive_correctness 同判据：process 存在但
// correct 非 bool 时继续向下判（不视为显式证据）。
func DeriveCorrectness(scoringTrace map[string]any, errorInferences []map[string]any) *bool {
	if process, ok := scoringTrace["process"].(map[string]any); ok {
		if correct, ok := process["correct"].(bool); ok {
			return &correct
		}
	}
	if len(errorInferences) > 0 {
		wrong := false
		return &wrong
	}
	return nil
}

// dueAt 到期时刻 = 基准时刻 + intervals[stage] 天。
// 用 AddDate 按日历日推进（与 Python timedelta(days=N) 在 UTC 时轴上等价）.
func dueAt(base time.Time, stage int, intervalsDays []int) time.Time {
	return base.AddDate(0, 0, intervalsDays[stage])
}

// ApplyEvent 单事件状态迁移（纯函数）。
//
// state: 当前队列状态；nil=该题未在队。
// event: 排程视图事件（调用方保证按 CreatedAt 升序喂入）。
// intervalsDays: 策略固定间隔表（天），非空——空表返回 ErrInvalidIntervals
// （与冻结实现 ValueError 同判据）。
//
// 返回迁移后状态；nil 表示仍未在队（答对/未知且原本不在队）。
func ApplyEvent(state *EntryState, event ReviewEventView, intervalsDays []int) (*EntryState, error) {
	if len(intervalsDays) == 0 {
		return nil, fmt.Errorf("%w: intervals_days 不能为空（策略间隔表至少一个间隔）", ErrInvalidIntervals)
	}
	if event.Correct == nil {
		// 无法判定对错：不迁移（不猜）
		return state, nil
	}

	if !*event.Correct {
		// 答错：入队或重置回 stage 0（含错误推断的事件自动入队，S6 要求）。
		// enqueued_at 保留原值（重置不改首次入队时刻，与冻结实现一致）。
		source := ""
		if len(event.ErrorTypeIDs) > 0 {
			source = event.ErrorTypeIDs[0]
		}
		next := &EntryState{
			Stage:             0,
			Status:            StatusPending,
			EnqueuedAt:        event.CreatedAt,
			DueAt:             dueAt(event.CreatedAt, 0, intervalsDays),
			SourceErrorTypeID: source,
			LastEventID:       event.EventID,
		}
		if state != nil {
			next.EnqueuedAt = state.EnqueuedAt
		}
		return next, nil
	}

	// 答对：仅在队 pending 时推进；不在队/已 done 忽略
	if state == nil || state.Status == StatusDone {
		return state, nil
	}
	next := *state
	nextStage := state.Stage + 1
	if nextStage >= len(intervalsDays) {
		// 走完最后一个间隔 → 出队（stage/due_at 保持原值，与冻结实现
		// replace(state, status=done) 的字段选择一致）
		next.Status = StatusDone
		next.LastEventID = event.EventID
		return &next, nil
	}
	next.Stage = nextStage
	next.DueAt = dueAt(event.CreatedAt, nextStage, intervalsDays)
	next.LastEventID = event.EventID
	return &next, nil
}

// RebuildQueue 事件流全量重放 → 每题队列状态（可重建性的权威实现）。
//
// events: 单学生的排程视图事件流；调用方按 (CreatedAt, EventID) 升序供给
// （乱序输入会破坏状态机语义，本函数不重复排序以防掩盖上游 bug）。
// 返回 {item_version_id: EntryState}——只含曾在队的题（答对/未知从不会产生条目）。
func RebuildQueue(events []ReviewEventView, intervalsDays []int) (map[string]EntryState, error) {
	states := map[string]EntryState{}
	for i, event := range events {
		var current *EntryState
		if s, ok := states[event.ItemVersionID]; ok {
			current = &s
		}
		next, err := ApplyEvent(current, event, intervalsDays)
		if err != nil {
			return nil, fmt.Errorf("events[%d]: %w", i, err)
		}
		// ApplyEvent 仅在原不在队且事件为答对/未知时返回 nil——不落键
		if next != nil {
			states[event.ItemVersionID] = *next
		}
	}
	return states, nil
}

// DueReview 是到期取题视图（get_due_reviews 的纯函数投影：到期过滤 + 排序）。
type DueReview struct {
	ItemVersionID string
	State         EntryState
}

// DueReviews 返回已到期的在队复习条目（最逾期优先）。
//
// 判定与冻结实现 get_due_reviews 同判据：status=pending 且 due_at <= now
// （now 恰等于 due_at 视为已到期——「今天到期今天取」）。排序按 due_at 升序，
// 以 item_version_id 作次序兜底保证确定性（冻结实现的次序兜底键是 entry_id，
// 纯函数核无 entry_id，用 item_version_id 承担同一确定性职责）。
func DueReviews(states map[string]EntryState, now time.Time) []DueReview {
	due := make([]DueReview, 0, len(states))
	for id, state := range states {
		if state.Status == StatusPending && !state.DueAt.After(now) {
			due = append(due, DueReview{ItemVersionID: id, State: state})
		}
	}
	sort.Slice(due, func(i, j int) bool {
		if !due[i].State.DueAt.Equal(due[j].State.DueAt) {
			return due[i].State.DueAt.Before(due[j].State.DueAt)
		}
		return due[i].ItemVersionID < due[j].ItemVersionID
	})
	return due
}
