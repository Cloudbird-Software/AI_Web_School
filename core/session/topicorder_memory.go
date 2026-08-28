package session

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// MemoryStore 是会话题序账的进程内实现（W6 服务化前的装配面与并发语义测试
// 替身：互斥锁串行化同一 session 的全部写入，与 PGStore 的 PK 23505 临界冲撞
// 同一契约——consent 门同惯例）.
//
// 内部账只存 canonical 形态（Seq 升序深拷贝）；Create/Read 返回的快照均为新
// 深拷贝，调用方不可能经返回值改写内部账（-race 干净的结构前提）.
type MemoryStore struct {
	mu     sync.Mutex
	orders map[string][]TopicEntry // sessionID → canonical（Seq 升序）题序
}

// 编译期锚定：内存实现兑现 TopicOrderStore（见 topicorder.go 锚定三）.

// NewMemoryStore 构造空账.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{orders: make(map[string][]TopicEntry)}
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
