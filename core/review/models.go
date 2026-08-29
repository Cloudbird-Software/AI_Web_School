// models.go 承载复习队列条目类型（W3 S6；Python 冻结实现 src/core/review/
// models.py 的 ReviewQueueEntryPydantic 的 Go 重锚定）。
//
// ReviewQueueEntry 是派生队列（非三本账，允许 UPDATE）——正确性由
// RebuildQueue 的「事件流 × 策略版本」纯函数重放保证；本包只定义类型与
// 派生助手，ORM/落库面（sqlc 生成物）住在 db 层。
//
// 宪法 A5：本模块不 import 任何学科包/学段包。
package review

import "time"

// ReviewEntry 是复习队列条目（派生队列行 / 到期取题接口的响应元素，
// 字段与冻结实现 ReviewQueueEntryPydantic 一一对应）。
//
// UNIQUE(student_alias_id, item_version_id, policy_id, policy_version)：
// 一个学生的一道题在同一策略版本下至多一条在队记录。
type ReviewEntry struct {
	EntryID           string // UUID 文本（应用层生成）
	StudentAliasID    string // UUID 文本（假名身份，P0 匿名化）
	ItemVersionID     string
	PolicyID          string
	PolicyVersion     string
	Stage             int    // >= 0（ck_review_queue_entry_stage_nonnegative）
	Status            string // StatusPending | StatusDone
	SourceErrorTypeID string // 空 = 入队事件未带错误推断
	LastEventID       string // UUID 文本；空 = 无（不出现于 RebuildQueue 产物）
	EnqueuedAt        time.Time
	DueAt             time.Time
}

// NewReviewEntry 由纯函数核的 EntryState 装配队列条目（对应冻结实现
// service.sync_review_queue 的构造路径；entry_id 由调用方供给——UUID 生成
// 属应用层，纯函数核不做随机源）.
func NewReviewEntry(entryID, studentAliasID, itemVersionID, policyID, policyVersion string, state EntryState) ReviewEntry {
	return ReviewEntry{
		EntryID:           entryID,
		StudentAliasID:    studentAliasID,
		ItemVersionID:     itemVersionID,
		PolicyID:          policyID,
		PolicyVersion:     policyVersion,
		Stage:             state.Stage,
		Status:            state.Status,
		SourceErrorTypeID: state.SourceErrorTypeID,
		LastEventID:       state.LastEventID,
		EnqueuedAt:        state.EnqueuedAt,
		DueAt:             state.DueAt,
	}
}

// IsDue 报告条目在 now 时刻是否到期（get_due_reviews 的行级判据：
// status=pending 且 due_at <= now——「今天到期今天取」）.
func (e ReviewEntry) IsDue(now time.Time) bool {
	return e.Status == StatusPending && !e.DueAt.After(now)
}
