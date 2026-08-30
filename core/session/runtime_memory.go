package session

import (
	"context"
	"fmt"
	"time"
)

// runtime_memory.go 是 RuntimeStore（会话运行态账服务面，接口声明见
// service.go）的进程内实现：方法挂在共享 MemoryStore 上，与题序固化面
// （topicorder_memory.go）/作答提交面（memory.go）共用同一把互斥锁——
// 「会话域单写者」在内存投影里就是这一把锁，状态迁移（休息确认/放弃/
// 时长保护置位）与提交推进因此天然串行化.
//
// 语义基准是 Python 冻结实现 src/core/session/service.py 的 resume_session /
// abandon_session / _check_time_protection；PG 实现见 runtime_pg.go，两实现
// 共享 service.go 的判定口径（拒绝条件、时刻语义），不存在判据漂移面.

// RuntimeState 实现 RuntimeStore：会话运行态只读投影（深拷贝；题序条目取自
// 同门题序账以携带 placement_token，seed 面直开的会话回落纯序列条目）.
func (m *MemoryStore) RuntimeState(_ context.Context, _ Executor, sessionID string) (*SessionRuntime, error) {
	key, err := normalizeSessionID(sessionID)
	if err != nil {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[key]
	if !ok {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	return m.runtimeLocked(s), nil
}

// Resume 实现 RuntimeStore：休息确认（rest_prompted/active → active，计时
// 锚点重置）。completed/abandoned 拒绝（Python resume_session 同判据）；
// 拒绝零副作用.
func (m *MemoryStore) Resume(_ context.Context, _ Executor, sessionID string, at time.Time) (*SessionRuntime, error) {
	key, err := normalizeSessionID(sessionID)
	if err != nil {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[key]
	if !ok {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	switch s.status {
	case StatusCompleted, StatusAbandoned:
		return nil, fmt.Errorf("%w: 会话已 %s，不能休息确认", ErrSessionState, s.status)
	}
	if at.IsZero() {
		at = m.nowFn()()
	}
	s.status = StatusActive
	s.lastResumeAt = at
	s.lastActivityAt = at
	return m.runtimeLocked(s), nil
}

// Abandon 实现 RuntimeStore：放弃会话（completed 拒绝；已作答事件保留在
// 账内——内存账同 append-only，零删除）.
func (m *MemoryStore) Abandon(_ context.Context, _ Executor, sessionID string, at time.Time) (*SessionRuntime, error) {
	key, err := normalizeSessionID(sessionID)
	if err != nil {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[key]
	if !ok {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	if s.status == StatusCompleted {
		return nil, fmt.Errorf("%w: 会话已完成，不能放弃", ErrSessionState)
	}
	if at.IsZero() {
		at = m.nowFn()()
	}
	s.status = StatusAbandoned
	s.lastActivityAt = at
	return m.runtimeLocked(s), nil
}

// MarkRestPrompted 实现 RuntimeStore：时长保护置位（零事件写入，Python
// _check_time_protection 同语义；resume 是恢复作答的唯一出口）.
func (m *MemoryStore) MarkRestPrompted(_ context.Context, _ Executor, sessionID string) error {
	key, err := normalizeSessionID(sessionID)
	if err != nil {
		return fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[key]
	if !ok {
		return fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	s.status = StatusRestPrompted
	return nil
}

// runtimeLocked 装配运行态投影（调用方持锁）。题序条目从同门题序账读出
// （Seq 升序 canonical，含 placement_token）；题序账无行的 seed 直开会话
// 回落纯序列条目（token=nil）——两本账由 Create 开立面保证同键，此处只做
// 防御性回落而非账损判定（seed 面是测试装配口，不承载生产键序）.
func (m *MemoryStore) runtimeLocked(s *memorySession) *SessionRuntime {
	rt := &SessionRuntime{
		SessionID:      s.sessionID,
		StudentAliasID: s.studentAliasID,
		Scene:          s.scene,
		Gradeband:      s.gradeband,
		PaperID:        cloneStringPtr(s.paperID),
		Status:         s.status,
		RetestWrong:    s.retestWrong,
		CurrentIndex:   s.currentIndex,
		AnsweredCount:  s.answeredCount,
		CorrectCount:   s.correctCount,
		WrongMarks:     cloneInferences(s.wrongMarks),
		TimeLimitSec:   s.timeLimitSec,
		StartedAt:      s.startedAt,
		LastResumeAt:   s.lastResumeAt,
		LastActivityAt: s.lastActivityAt,
	}
	if s.completedAt != nil {
		t := *s.completedAt
		rt.CompletedAt = &t
	}
	entries, ok := m.orders[s.sessionID]
	if !ok {
		entries = make([]TopicEntry, len(s.sequence))
		for i, id := range s.sequence {
			entries[i] = TopicEntry{Seq: i + 1, ItemVersionID: id}
		}
	}
	rt.Entries = cloneEntries(entries)
	return rt
}

// newWrongMark 装配错题标记（Python submit_answer 的 mark dict 同构：
// item_version_id/item_number/error_type_ids/first_seen_at/retest_status）；
// 调用方持锁且 currentIndex 尚未推进，item_number 取推进前的题位.
func newWrongMark(p *preparedSubmit, s *memorySession) map[string]any {
	status := "off"
	if s.retestWrong {
		status = "pending"
	}
	return map[string]any{
		"item_version_id": p.itemVersionID,
		"item_number":     s.currentIndex + 1,
		"error_type_ids":  inferenceErrorTypeIDs(p.inferences),
		"first_seen_at":   p.at,
		"retest_status":   status,
	}
}

// traceCorrect 从评分轨迹提取显式对错判定（契约 §3 trace.process.correct，
// core/scoring buildTrace 的落账形态）。返回 explicit=false 表示轨迹不含
// 该键——调用方两账都不动，不猜对错.
func traceCorrect(trace map[string]any) (explicit bool, correct bool) {
	process, ok := trace["process"].(map[string]any)
	if !ok {
		return false, false
	}
	c, ok := process["correct"].(bool)
	if !ok {
		return false, false
	}
	return true, c
}

// inferenceErrorTypeIDs 抽取错误推断的 error_type_id 列表（错题标记的归因
// 摘要；nil 推断记空表——JSON 序列化后为 []，与契约「可为空数组」同形）.
func inferenceErrorTypeIDs(inferences []map[string]any) []string {
	out := make([]string, 0, len(inferences))
	for _, inf := range inferences {
		if id, ok := inf["error_type_id"].(string); ok {
			out = append(out, id)
		}
	}
	return out
}

// cloneStringPtr 拷贝可空字符串（nil 语义保持；输出面交独立指针）.
func cloneStringPtr(s *string) *string {
	if s == nil {
		return nil
	}
	c := *s
	return &c
}
