package ai

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// CallStatus 是台账结果状态三值域（与 0026 ck_ai_call_ledger_status_domain 一致）：
// ok=出站成功且产物已交付；failed=出站已发生但失败；rejected=前置合规门拒绝、
// 零出站发生（X12：拒绝也是账面事实，不留暗数）.
type CallStatus string

// 台账状态三值域.
const (
	StatusOK       CallStatus = "ok"
	StatusFailed   CallStatus = "failed"
	StatusRejected CallStatus = "rejected"
)

// LedgerEntry 是一次 AI 调用的台账记录（对齐冻结实现 schemas.LedgerEntry +
// 0026 表列；prompt 原文永不入账，只有 sha256 前 16 hex 的 PromptHash）。
// 值类型语义：出库即快照，调用方可持有而不受 MemoryLedger 内部追加影响.
type LedgerEntry struct {
	CallID        string
	Modality      Modality
	TaskLevel     TaskLevel // 空串=NULL（未完成路由即被拒）
	TaskName      string
	Provider      string
	Model         string
	ModelVersion  string
	PromptHash    string
	PromptVersion string
	TokenIn       int
	TokenOut      int
	CostCNY       float64
	DurationMS    float64
	Status        CallStatus
	Reason        string // 固定短码（见 bus.go Reason* 常量）；ok 行为空
	Fallback      bool
	ArtifactRef   string
	CallerName    string
	CreatedAt     time.Time
	// Payload 是调用方附加的台账加性键（T-W5-015：TTS 的 char_count /
	// voice_fingerprint）。语义 = ai_call_ledger 的 JSONB payload（对齐冻结
	// 实现 raw_meta 的「必要子集、禁止含 PII」约束），对齐不扩 DB 列：0026
	// 暂无 payload 列，PG 实现暂不落库（W6 随 payload 列迁移接通），
	// MemoryLedger 全量保留.
	Payload map[string]string
}

// Ledger 是 AI 调用台账的存储契约。总线在交付产物前同步写败即调用失败
// （ErrLedgerWrite），因此实现方必须把「写入是否成功」当成权威答案返回，
// 禁止任何缓冲/异步/静默丢弃语义（D10 全覆盖的实现面前提）.
//
// 并发契约：Record 与 ByArtifact 必须可并发调用（实现内置同步）.
type Ledger interface {
	// Record 追加一行台账；主键冲突或存储故障如实返回 error.
	Record(ctx context.Context, e LedgerEntry) error
	// ByArtifact 按 artifact_ref 升序返回该产物的全部台账行（成本归集键）.
	ByArtifact(ctx context.Context, artifactRef string) ([]LedgerEntry, error)
}

// ErrDuplicateCallID 表示台账主键撞车（idgen 故障面）；Memory/PG 实现共用判定.
var ErrDuplicateCallID = errors.New("ai/ledger: call_id 重复")

// MemoryLedger 是 Ledger 的进程内实现：互斥锁保护的只追加切片，供单测
// （go test -race 并发用例）与单体进程内嵌使用；PG 生产实现在 ledger_pg.go
// （0026 ai_call_ledger 表 + append-only 触发器）.
//
// 为什么内存实现也保守地判重主键：台账全覆盖的可信度建立在「一行一调用」上，
// idgen 异常导致的碰撞必须在最近的落点显式失败而不是悄悄叠行.
type MemoryLedger struct {
	mu      sync.Mutex
	entries []LedgerEntry
	ids     map[string]struct{}
}

// NewMemoryLedger 构造空台账.
func NewMemoryLedger() *MemoryLedger {
	return &MemoryLedger{ids: make(map[string]struct{})}
}

// Record 实现 Ledger：持锁内做主键判重 + 只追加。ctx 在内存实现中不参与持久化
// （无外部 IO），保留参数以维持接口同构（与 estimator.MemoryStore 的 Executor
// 参数同理念：契约统一，装配方无须分支）.
func (m *MemoryLedger) Record(_ context.Context, e LedgerEntry) error {
	if e.CallID == "" {
		return fmt.Errorf("%w: 空 call_id", ErrInvalidRequest)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, dup := m.ids[e.CallID]; dup {
		return fmt.Errorf("%w: %s", ErrDuplicateCallID, e.CallID)
	}
	m.ids[e.CallID] = struct{}{}
	m.entries = append(m.entries, e)
	return nil
}

// ByArtifact 实现 Ledger：线性过滤并按 CreatedAt 升序返回（行是值拷贝）.
func (m *MemoryLedger) ByArtifact(_ context.Context, artifactRef string) ([]LedgerEntry, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []LedgerEntry
	for _, e := range m.entries {
		if e.ArtifactRef == artifactRef {
			out = append(out, e)
		}
	}
	sortEntries(out)
	return out, nil
}

// Snapshot 返回全部台账行的拷贝（升序）。测试与进程内审计面板消费；
// 生产路径请走 PG 实现的 ListAICallLedgerByArtifact（sqlc 生成面）.
func (m *MemoryLedger) Snapshot() []LedgerEntry {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]LedgerEntry, len(m.entries))
	copy(out, m.entries)
	sortEntries(out)
	return out
}

// sortEntries 按 CreatedAt 稳定升序（同时刻保插入序=时间线不乱序）.
func sortEntries(es []LedgerEntry) {
	for i := 1; i < len(es); i++ {
		for j := i; j > 0 && es[j].CreatedAt.Before(es[j-1].CreatedAt); j-- {
			es[j], es[j-1] = es[j-1], es[j]
		}
	}
}
