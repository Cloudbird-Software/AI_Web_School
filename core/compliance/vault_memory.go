package compliance

import (
	"context"
	"sort"
	"sync"
	"time"
)

// MemoryVaultStore 是 VaultStore 的进程内实现：互斥锁承载「读改写」原子性，
// 供单测（go test -race 并发测试）与单体进程内嵌使用；PG 生产实现在 W6
// 服务化时接线（与 ConsentStore 双实现惯例同构）。
//
// 为什么内存实现也要严格模拟 DB 两层保证：本卡验收的独立事务语义必须在
// 不依赖真实 PG 的前提下可验证——
//   - identities 账与 auditLog 账是两份独立状态：审计追加只进 auditLog，
//     业务回滚（ identities 快照恢复）不可能波及审计行——这正是「审计走
//     独立事务/独立连接」在内存侧的结构化表达；
//   - PK 唯一性在写入点直接判定（唯一索引的内存等效），重复写报
//     ErrIdentityExists，与 PG 实现对同一输入同一条哨兵错误.
type MemoryVaultStore struct {
	mu sync.Mutex
	// identities 密文账（键=alias 十六字节）；行内字节面出入皆深拷贝.
	identities map[[16]byte]IdentityCiphertext
	// auditLog 审计账（append 序；读出按 accessed_at,access_id 升序投影）.
	auditLog []AccessLogEntry
	now      func() time.Time
}

// NewMemoryVaultStore 构造空的内存 vault 存储.
func NewMemoryVaultStore() *MemoryVaultStore {
	return &MemoryVaultStore{
		identities: make(map[[16]byte]IdentityCiphertext),
		now:        time.Now,
	}
}

// WriteIdentity 实现 VaultStore：alias 已存在即 ErrIdentityExists（唯一索引
// 防线的内存等效）。行内字节面在入账前深拷贝——调用方后续改写自己的切片
// 不可能波及账面.
func (m *MemoryVaultStore) WriteIdentity(_ context.Context, _ Executor, row IdentityCiphertext) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if _, exists := m.identities[row.StudentAliasID]; exists {
		return ErrIdentityExists
	}
	m.identities[row.StudentAliasID] = cloneCiphertext(row)
	return nil
}

// ReadIdentity 实现 VaultStore：无记录即 ErrIdentityNotFound（不让「查找
// 失败」以别的形态泄漏）。行内字节面出账前深拷贝——读面拿到的密文是独立
// 拷贝，外部改写不影响账（-race 干净的结构前提）.
func (m *MemoryVaultStore) ReadIdentity(_ context.Context, _ Executor, alias [16]byte) (*IdentityCiphertext, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	row, ok := m.identities[alias]
	if !ok {
		return nil, ErrIdentityNotFound
	}
	out := cloneCiphertext(row)
	return &out, nil
}

// AppendAccessLog 实现 VaultStore：审计账只追加（append-only 的内存面），
// 条目值语义拷贝入账.
func (m *MemoryVaultStore) AppendAccessLog(_ context.Context, _ Executor, entry AccessLogEntry) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.auditLog = append(m.auditLog, entry)
	return nil
}

// ListAccessLog 实现 VaultStore：该 alias 的审计只读投影，按 accessed_at、
// access_id 升序（与 PG ListVaultAccessLog 的 ORDER BY 严格同义）.
func (m *MemoryVaultStore) ListAccessLog(_ context.Context, _ Executor, alias [16]byte) ([]AccessLogEntry, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make([]AccessLogEntry, 0, len(m.auditLog))
	for _, e := range m.auditLog {
		if e.StudentAliasID == alias {
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if !out[i].AccessedAt.Equal(out[j].AccessedAt) {
			return out[i].AccessedAt.Before(out[j].AccessedAt)
		}
		return bytesLess(out[i].AccessID, out[j].AccessID)
	})
	return out, nil
}

// snapshotIdentitiesLocked 复制整本密文账（测试用：模拟业务事务回滚——
// 快照恢复 identities 而审计账独立存活）。必须在持锁态调用.
func (m *MemoryVaultStore) snapshotIdentitiesLocked() map[[16]byte]IdentityCiphertext {
	out := make(map[[16]byte]IdentityCiphertext, len(m.identities))
	for k, v := range m.identities {
		out[k] = cloneCiphertext(v)
	}
	return out
}

// restoreIdentitiesLocked 用快照整表覆盖密文账（业务回滚的内存等效）；
// 审计账不被触碰——独立事务语义的结构保证。必须在持锁态调用.
func (m *MemoryVaultStore) restoreIdentitiesLocked(snap map[[16]byte]IdentityCiphertext) {
	m.identities = make(map[[16]byte]IdentityCiphertext, len(snap))
	for k, v := range snap {
		m.identities[k] = cloneCiphertext(v)
	}
}

// cloneCiphertext 深拷贝密文行（全部字节面独立分配）.
func cloneCiphertext(row IdentityCiphertext) IdentityCiphertext {
	out := row
	out.NameCiphertext = cloneBytes(row.NameCiphertext)
	out.NameNonce = cloneBytes(row.NameNonce)
	out.PhoneCiphertext = cloneBytes(row.PhoneCiphertext)
	out.PhoneNonce = cloneBytes(row.PhoneNonce)
	out.AddressCiphertext = cloneBytes(row.AddressCiphertext)
	out.AddressNonce = cloneBytes(row.AddressNonce)
	out.ParentContactCiphertext = cloneBytes(row.ParentContactCiphertext)
	out.ParentContactNonce = cloneBytes(row.ParentContactNonce)
	return out
}

// cloneBytes 字节面独立拷贝（nil 保持 nil）.
func cloneBytes(b []byte) []byte {
	if b == nil {
		return nil
	}
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

// bytesLess 十六字节字典序（审计投影同刻多行的确定性次序键）.
func bytesLess(a, b [16]byte) bool {
	for i := range a {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return false
}
