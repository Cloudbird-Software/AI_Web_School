package estimator

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sync"
	"time"
)

// MemoryStore 是 ActivePointerStore 的进程内实现：互斥锁临界区完整承载
// 「退役旧活跃 + 登记新活跃 + 追加留痕」的原子语义，供单测（go test -race
// 并发测试）与单体进程内嵌使用；PG 生产实现在 W6 服务化时接线.
//
// 为什么内存实现也要严格模拟 DB 三层保证中的「应用层临界区」：本卡验收 #2
// 的并发正确性必须在不依赖真实 PG 的前提下可验证——互斥锁即 advisory xact
// lock 的内存等效物；偏唯一索引防线由「每 scope 恰一条 retired_at IS NULL
// 行」的结构不变量模拟.
type MemoryStore struct {
	mu sync.Mutex
	// active 每 scope 至多一条活跃指针（结构上保证偏唯一语义）.
	active map[PurposeScope]*EstimatorRun
	// runs 是 scope 无关的全量行账（插入序）：时间回溯要扫到被顶替的历史行，
	// 只看 active 会丢退役链（D6 回溯的实证面）.
	runs []*EstimatorRun
	// trail 是 append-only 切换留痕账：只在持锁临界区内追加，从不改写历史条目.
	trail []SwitchRecord
	// now 可注入时钟（测试确定性）；零值安全默认 time.Now.
	now func() time.Time
}

// NewMemoryStore 构造空的内存指针存储.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{active: make(map[PurposeScope]*EstimatorRun), now: time.Now}
}

// SetActive 实现 ActivePointerStore：整个读改写序列在单一互斥锁临界区内完成，
// 并发调用被串行化——不存在两条活跃指针交织出的中间态；q 为内存实现未使用的
// 事务执行面（契约见 Executor），传 nil 即可.
func (m *MemoryStore) SetActive(_ context.Context, _ Executor, in SetInput) (*EstimatorRun, bool, error) {
	if err := validateSetInput(in); err != nil {
		return nil, false, err
	}
	actor := in.ActivatedBy
	if actor == "" {
		actor = SystemActor
	}
	ts := in.ActivatedAt
	if ts.IsZero() {
		ts = m.now()
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	cur := m.active[in.PurposeScope]
	if cur != nil && cur.ModelVersion == in.ModelVersion {
		// 幂等命中：请求版本已是当前活跃版本——原样返回、不入账。
		// 幂等重放必须无副作用（A9/D11 对外写入端点幂等）.
		return cur.clone(), false, nil
	}

	var from string
	if cur != nil {
		from = cur.ModelVersion
		retired := ts
		cur.RetiredAt = &retired // 先退役旧行：与 PG 侧先 UPDATE 后 INSERT 的同序，保证偏唯一不冲突
	}
	run := &EstimatorRun{
		RunID:           newRunID(),
		PurposeScope:    in.PurposeScope,
		ModelVersion:    in.ModelVersion,
		CodeDigest:      in.CodeDigest,
		InputSnapshotID: in.InputSnapshotID,
		GraphReleaseID:  in.GraphReleaseID,
		ActivatedBy:     actor,
		ActivatedAt:     ts,
	}
	m.runs = append(m.runs, run)
	m.active[in.PurposeScope] = run
	m.trail = append(m.trail, SwitchRecord{
		Who: actor, Scope: in.PurposeScope, From: from, To: in.ModelVersion, At: ts,
	})
	return run.clone(), true, nil
}

// GetActive 实现 ActivePointerStore：返回内部状态的深拷贝。
// asOf=nil 直接取 map 槽位（每 scope 恰一条活跃指针的结构不变量，与 PG 的
// retired_at IS NULL 过滤严格同义）；给定 asOf 才扫全量行做时间回溯.
// 为什么当前态不走「按 activated_at 最新」扫描：物理时钟粒度有限（Windows 可
// 到 ~0.5ms），密集切换下多行并列同一时刻，「最新优先」会随机命中已退役行；
// 结构化取法不依赖时钟精度.
func (m *MemoryStore) GetActive(_ context.Context, _ Executor, scope PurposeScope, asOf *time.Time) (*EstimatorRun, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if asOf == nil {
		cur := m.active[scope]
		if cur == nil {
			return nil, nil
		}
		return cur.clone(), nil
	}

	var best *EstimatorRun
	for _, r := range m.runs {
		if r.PurposeScope != scope {
			continue
		}
		if r.ActivatedAt.After(*asOf) {
			continue // 在 asOf 之后才登记
		}
		if r.RetiredAt != nil && !r.RetiredAt.After(*asOf) {
			continue // 在 asOf 时已退役（Python 冻结实现语义：仅 retired_at > ts 存活）
		}
		if best == nil || r.ActivatedAt.After(best.ActivatedAt) {
			best = r
		}
	}
	if best == nil {
		return nil, nil
	}
	return best.clone(), nil
}

// SwitchTrail 实现 ActivePointerStore：返回留痕账的拷贝（升序），调用方改不动历史.
func (m *MemoryStore) SwitchTrail(_ context.Context, _ Executor, scope PurposeScope) ([]SwitchRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	var out []SwitchRecord
	for _, rec := range m.trail {
		if rec.Scope == scope {
			out = append(out, rec)
		}
	}
	return out, nil
}

// newRunID 生成 run_<随机 hex> 形式的行标识：crypto/rand 而非自增序号，
// 避免把「行写入序」泄漏成可猜测的业务标识（与 Python 侧 run_+ULID 同形不同源；
// 标识唯一性由随机熵承担）.
func newRunID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand 失败即系统熵源不可用：宁可不发号也不发可重复 ID.
		panic(fmt.Sprintf("estimator: 熵源不可用无法生成 run_id: %v", err))
	}
	return "run_" + hex.EncodeToString(b[:])
}
