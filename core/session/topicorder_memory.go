package session

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// MemoryStore 是会话域双面（题序固化 T-W5-004 / 作答提交 T-W5-018）共用的
// 进程内实现（W6 服务化前的装配面与并发语义测试替身：互斥锁串行化同一
// session 的全部写入，与 PGStore 的唯一约束 23505 临界冲撞同一契约——consent
// 门同惯例）。同一把锁串行化两个写入面，正是「会话域单写者」的内存投影.
//
// 内部账只存 canonical 形态（题序 Seq 升序深拷贝）；Create/Read/SubmitAnswer
// 返回的快照均为新深拷贝，调用方不可能经返回值改写内部账（-race 干净的结构前提）.
type MemoryStore struct {
	mu     sync.Mutex
	orders map[string][]TopicEntry // sessionID → canonical（Seq 升序）题序

	// ── 作答提交面专属字段（T-W5-018；仅 memory.go/submit.go 读写）──
	// now 可注入时钟（测试确定性）；零值安全回落 time.Now.
	now func() time.Time
	// sessions 会话运行态账（提交推进的唯一写入口在临界区内）.
	sessions map[string]*memorySession
	// submissions 幂等登记账：键 = (session, item, 作答指纹)，恰一次真实
	// 提交占一个键位——重复提交在键命中处直接取回首次结果（幂等语义）.
	submissions map[submissionKey]submissionRecord
	// ledger 是 response_event 的内存投影账（append-only，只在持锁临界区内
	// 追加，从不改写历史条目）.
	ledger []EventRecord
}

// 编译期锚定：内存实现兑现 TopicOrderStore（见 topicorder.go 锚定三）与
// SubmissionStore（见 pg.go 锚定三）.

// NewMemoryStore 构造空账（双面字段全部就位；提交面另设 initSubmitState
// 兜底零值装配）.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		orders:      make(map[string][]TopicEntry),
		now:         time.Now,
		sessions:    make(map[string]*memorySession),
		submissions: make(map[submissionKey]submissionRecord),
	}
}

// Create 实现 TopicOrderStore：前置校验（与 PG 实现共用 prepareStart，非法输入
// 必然同一条哨兵错误）→ 已固化则语义比对（相同=幂等成功返回存量，不同=
// ErrTopicOrderConflict）→ 未固化则入账.
func (m *MemoryStore) Create(_ context.Context, _ Executor, in StartInput) (*TopicOrder, error) {
	prepared, err := prepareStart(in, time.Now)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if existing, ok := m.orders[prepared.sessionID]; ok {
		if !equalEntries(existing, prepared.entries) {
			return nil, fmt.Errorf("%w: session_id=%s", ErrTopicOrderConflict, prepared.sessionID)
		}
		return &TopicOrder{SessionID: prepared.sessionID, Entries: cloneEntries(existing)}, nil
	}
	m.orders[prepared.sessionID] = cloneEntries(prepared.entries)
	return &TopicOrder{SessionID: prepared.sessionID, Entries: cloneEntries(prepared.entries)}, nil
}

// Read 实现 TopicOrderStore：按 Seq 升序稳定读出（内部账恒 canonical，出账深拷贝）；
// id 归一与 PG 实现同源（normalizeSessionID），非法 id 与未固化同落 ErrSessionNotFound.
func (m *MemoryStore) Read(_ context.Context, _ Executor, sessionID string) (*TopicOrder, error) {
	key, err := normalizeSessionID(sessionID)
	if err != nil {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, ok := m.orders[key]
	if !ok {
		return nil, fmt.Errorf("%w: session_id=%q", ErrSessionNotFound, sessionID)
	}
	return &TopicOrder{SessionID: key, Entries: cloneEntries(entries)}, nil
}
