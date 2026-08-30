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
//
// GO-RW-002 服务域注记：practice_session 在 PG 是一行双职（题序列 + 运行态列），
// 内存投影因 T-W5-004/018 两波分账（orders/sessions 两张 map）；本面在题序固化
// 的同一临界区内补运行态开立（provision-if-absent——幂等重放不覆写已推进的
// 运行态），使 Create 与冻结 start_session 的 INSERT 形态对齐：一次调用即得
// 「带运行态的会话」，服务域无须感知内存分账的实现细节.
func (m *MemoryStore) Create(_ context.Context, _ Executor, in StartInput) (*TopicOrder, error) {
	prepared, err := prepareStart(in, time.Now)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.initSubmitState()
	if existing, ok := m.orders[prepared.sessionID]; ok {
		if !equalEntries(existing, prepared.entries) {
			return nil, fmt.Errorf("%w: session_id=%s", ErrTopicOrderConflict, prepared.sessionID)
		}
		return &TopicOrder{SessionID: prepared.sessionID, Entries: cloneEntries(existing)}, nil
	}
	m.orders[prepared.sessionID] = cloneEntries(prepared.entries)
	m.provisionRuntimeLocked(prepared, in.PaperID)
	return &TopicOrder{SessionID: prepared.sessionID, Entries: cloneEntries(prepared.entries)}, nil
}

// provisionRuntimeLocked 在题序固化成功后开立内存运行态行（调用方持锁；
// prepared 的身份/题序/时长已过 prepareStart 校验，此处按冻结 INSERT 形态
// 直取：status='active'、进度清零、三时刻同源）.
func (m *MemoryStore) provisionRuntimeLocked(prepared *preparedStart, paperID *string) {
	if _, exists := m.sessions[prepared.sessionID]; exists {
		return
	}
	p := prepared.params
	m.sessions[prepared.sessionID] = &memorySession{
		sessionID:      prepared.sessionID,
		studentAliasID: formatUUID(p.StudentAliasID.Bytes),
		scene:          p.Scene,
		gradeband:      p.Gradeband,
		paperID:        cloneStringPtr(paperID),
		status:         p.Status,
		sequence:       sequenceOf(prepared.entries),
		retestWrong:    p.RetestWrong,
		timeLimitSec:   int(p.TimeLimitSec),
		startedAt:      p.StartedAt.Time,
		lastResumeAt:   p.LastResumeAt.Time,
		lastActivityAt: p.LastActivityAt.Time,
	}
}

// sequenceOf 从题序条目抽取主序列（item_version_id 按 Seq 升序——entries 已
// canonical）.
func sequenceOf(entries []TopicEntry) []string {
	out := make([]string, len(entries))
	for i := range entries {
		out[i] = entries[i].ItemVersionID
	}
	return out
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
