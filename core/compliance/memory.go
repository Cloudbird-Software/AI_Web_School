package compliance

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// ledgerRow 是内存账的内部行形态：scope 以原始 JSON 字节承载，读出时再反序列化
// 成独立 map——「交出深拷贝」由此获得结构性保证（Scope 永不共享底层引用），这是
// -race 并发测试干净的前提.
type ledgerRow struct {
	consentID  [16]byte
	eventType  EventType
	scopeJSON  []byte
	validFrom  *time.Time
	validUntil *time.Time
	version    int
	recordedBy string
	createdAt  time.Time
}

// toEvent 把内部行转为输出面事件（含一次全新的 scope 反序列化拷贝）.
func (r *ledgerRow) toEvent(studentAliasID string) (*ConsentEvent, error) {
	scope, err := decodeScope(r.scopeJSON)
	if err != nil {
		return nil, err
	}
	return &ConsentEvent{
		ConsentID:      formatUUID(r.consentID),
		StudentAliasID: studentAliasID,
		EventType:      r.eventType,
		Scope:          scope,
		ValidFrom:      cloneTime(r.validFrom),
		ValidUntil:     cloneTime(r.validUntil),
		Version:        r.version,
		RecordedBy:     r.recordedBy,
		CreatedAt:      r.createdAt,
	}, nil
}

// view 返回不含 Scope 的标量投影（供状态判定纯函数核 stateAt 使用；scope 与
// 授权有效性判定无关，免去做一次完整反序列化的开销）.
func (r *ledgerRow) view(studentAliasID string) *ConsentEvent {
	return &ConsentEvent{
		StudentAliasID: studentAliasID,
		EventType:      r.eventType,
		ValidFrom:      cloneTime(r.validFrom),
		ValidUntil:     cloneTime(r.validUntil),
		Version:        r.version,
	}
}

// canonicalUUID 是授权链身份的归一书写（uuid 十六字节小写连字形式）：同一 UUID 的
// 不同大小写书写在 DB 侧天然等值，内存侧同样归一——否则两实现会对同一链给出两本账.
func canonicalUUID(b [16]byte) string { return formatUUID(b) }

// MemoryStore 是 ConsentStore 的进程内实现：互斥锁临界区完整承载「校验链顶 →
// 分配下一版本 → 追加新事件」的原子语义，供单测（go test -race 并发测试）与
// 单体进程内嵌使用；PG 生产实现在 W6 服务化时接线。
//
// 为什么内存实现也要严格模拟 DB 两层保证中的「应用层临界区」：本卡验收 #2 的
// 并发正确性必须在不依赖真实 PG 的前提下可验证——互斥锁即 advisory xact lock
// 的内存等效物；唯一索引防线由「链内 version 严格连续无重」的结构不变量模拟.
type MemoryStore struct {
	mu sync.Mutex
	// ledgers 每条授权链一个只追加切片（append 序 = version 升序）；
	// 从不原地改写历史行——append-only 在内存侧的结构化表达.
	ledgers map[chainKey][]*ledgerRow
	now     func() time.Time
}

// chainKey 是授权链在内存账里的身份键.
type chainKey struct {
	canon   string
	purpose string
}

// NewMemoryStore 构造空的内存授权账.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{ledgers: make(map[chainKey][]*ledgerRow), now: time.Now}
}

// RecordGrant 实现 ConsentStore：临界区语义见接口注释。q 为内存实现未使用的
// 事务执行面（契约见 Executor），传 nil 即可.
func (m *MemoryStore) RecordGrant(_ context.Context, _ Executor, in GrantInput) (*ConsentEvent, error) {
	p, err := prepareGrant(in, m.now)
	if err != nil {
		return nil, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	id, err := randomUUIDV4()
	if err != nil {
		return nil, err
	}
	vuntil := p.vuntil // 独立指针：内部行不得引用调用方值语义之外的可变别名
	row := &ledgerRow{
		consentID:  id,
		eventType:  EventGrant,
		scopeJSON:  p.scope,
		validFrom:  cloneTime(&p.vfrom),
		validUntil: &vuntil,
		version:    m.nextVersionLocked(p.sid, in.Purpose),
		recordedBy: p.actor,
		createdAt:  p.at,
	}
	key := chainKey{canon: canonicalUUID(p.sid), purpose: in.Purpose}
	m.ledgers[key] = append(m.ledgers[key], row)
	return row.toEvent(p.rawSID)
}

// Revoke 实现 ConsentStore：撤回前置校验与追加在同一锁内完成（Python 冻结实现的
// check 后写两步在这里不可被并发交织——否则会出现「两个撤回都看到 granted」的
// 双花式撕裂），失败路径零副作用、不烧版本号.
func (m *MemoryStore) Revoke(_ context.Context, _ Executor, in RevokeInput) (*ConsentEvent, error) {
	p, err := prepareRevoke(in, m.now)
	if err != nil {
		return nil, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	top, ok := m.topLocked(p.sid, in.Purpose)
	status := stateAt(p.rawSID, in.Purpose, topOrNil(top, ok, p.rawSID), p.at)
	if !ok || !status.IsValid {
		return nil, fmt.Errorf("%w: 当前状态 %q（学生 %s 的 %q 授权）",
			ErrNoActiveConsent, status.State, in.StudentAliasID, in.Purpose)
	}

	id, err := randomUUIDV4()
	if err != nil {
		return nil, err
	}
	row := &ledgerRow{
		consentID:  id,
		eventType:  EventRevoke,
		scopeJSON:  p.scope,
		validFrom:  nil, // CHECK 约束 ck_..._time_consistency：revoke 行两个时刻列必须 NULL
		validUntil: nil,
		version:    status.Version + 1,
		recordedBy: p.actor,
		createdAt:  p.at,
	}
	key := chainKey{canon: canonicalUUID(p.sid), purpose: in.Purpose}
	m.ledgers[key] = append(m.ledgers[key], row)
	return row.toEvent(p.rawSID)
}

// CheckConsent 实现 ConsentStore：永远取链顶（append 切片末位 = 最大版本）
// 判定，结构与 PG 的 ORDER BY version DESC LIMIT 1 严格同义且确定性一致.
func (m *MemoryStore) CheckConsent(_ context.Context, _ Executor, studentAliasID, purpose string, now *time.Time) (*ConsentStatus, error) {
	sid, err := validateChainKey(studentAliasID, purpose)
	if err != nil {
		return nil, err
	}
	ts := currentOr(now, m.now)

	m.mu.Lock()
	defer m.mu.Unlock()

	top, _ := m.topLocked(sid, purpose)
	return stateAt(studentAliasID, purpose, topOrNil(top, true, studentAliasID), ts), nil
}

// topOrNil 把「链顶行 + 存在性」折叠成 stateAt 的可空视图（无行 → nil → missing）.
func topOrNil(top *ledgerRow, ok bool, studentAliasID string) *ConsentEvent {
	if !ok || top == nil {
		return nil
	}
	return top.view(studentAliasID)
}

// History 实现 ConsentStore：全量账升序只读投影，每行都是新鲜反序列化的独立
// 拷贝——外部改不动历史.
func (m *MemoryStore) History(_ context.Context, _ Executor, studentAliasID, purpose string) ([]ConsentEvent, error) {
	sid, err := validateChainKey(studentAliasID, purpose)
	if err != nil {
		return nil, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	rows := m.ledgers[chainKey{canon: canonicalUUID(sid), purpose: purpose}]
	out := make([]ConsentEvent, 0, len(rows))
	for _, r := range rows {
		ev, err := r.toEvent(studentAliasID)
		if err != nil {
			return nil, err
		}
		out = append(out, *ev)
	}
	return out, nil
}

// topLocked 返回链顶内部行（append 序末位）；无行时 (nil, false)。必须在持锁态调用.
func (m *MemoryStore) topLocked(sid [16]byte, purpose string) (*ledgerRow, bool) {
	rows := m.ledgers[chainKey{canon: canonicalUUID(sid), purpose: purpose}]
	if len(rows) == 0 {
		return nil, false
	}
	return rows[len(rows)-1], true
}

// nextVersionLocked 分配链内下一版本号（无记录则 1）。互斥锁保证同一链的分配
// 串行化——这正是验收 #2「版本号连续无重复」的结构来源。必须在持锁态调用.
func (m *MemoryStore) nextVersionLocked(sid [16]byte, purpose string) int {
	top, ok := m.topLocked(sid, purpose)
	if !ok {
		return 1
	}
	return top.version + 1
}

// currentOr 归一可选判定时刻（nil 回落 now()）.
func currentOr(now *time.Time, fallback func() time.Time) time.Time {
	if now != nil {
		return *now
	}
	return fallback()
}
