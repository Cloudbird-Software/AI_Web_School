package ai

import (
	"context"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5/pgtype"
)

// PGLedger 是 Ledger 的 PG 生产实现：语句面全部来自 db/queries/ai_ledger.sql
// 的 sqlc 类型安全生成方法（SQL-2：不在 Go 拼 SQL）。append-only 不变量由
// 0026 迁移的 trg_ai_call_ledger_append_only 触发器在库端物理强制；本实现
// 只提供 INSERT 与只读投影，不存在改写语句面.
//
// 事务纪律（S4/D11）：本类型不持有连接、不自 begin/commit——台账写入是否并入
// 调用方事务由装配方决定（总线只要求 Record 同步且如实报错）；db 参数接受
// 任何满足 dbdbgen.DBTX 的执行面（pgx.Conn/pgx.Tx/pool）.
type PGLedger struct {
	q *dbgen.Queries
}

// 编译期锚定一：PGLedger 必须兑现 Ledger 契约（W6 装配直通的假设防线）.
var _ Ledger = (*PGLedger)(nil)

// NewPGLedger 构造 PG 台账.
func NewPGLedger(db dbgen.DBTX) *PGLedger {
	return &PGLedger{q: dbgen.New(db)}
}

// Record 实现 Ledger：InsertAICallLedger 直插一行。
// task_level/reason/artifact_ref/caller_name 的空值语义经 textNil 统一下探，
// created_at 留零交列默认 now()——与冻结实现「未显式传时刻取当前 UTC」一致.
// Payload 加性键 0026 无对应列，暂不入 PG 行（对齐不扩 schema；见 LedgerEntry.Payload）.
func (p *PGLedger) Record(ctx context.Context, e LedgerEntry) error {
	created := pgtype.Timestamptz{}
	if !e.CreatedAt.IsZero() {
		created = pgtype.Timestamptz{Time: e.CreatedAt, Valid: true}
	}
	if err := p.q.InsertAICallLedger(ctx, dbgen.InsertAICallLedgerParams{
		CallID:        e.CallID,
		Modality:      string(e.Modality),
		TaskLevel:     textNil(string(e.TaskLevel)),
		TaskName:      e.TaskName,
		Provider:      e.Provider,
		Model:         e.Model,
		ModelVersion:  e.ModelVersion,
		PromptHash:    e.PromptHash,
		PromptVersion: e.PromptVersion,
		TokenIn:       int32(e.TokenIn),
		TokenOut:      int32(e.TokenOut),
		CostCny:       e.CostCNY,
		DurationMs:    e.DurationMS,
		Status:        string(e.Status),
		Reason:        textNil(e.Reason),
		Fallback:      e.Fallback,
		ArtifactRef:   textNil(e.ArtifactRef),
		CallerName:    textNil(e.CallerName),
		CreatedAt:     created,
	}); err != nil {
		return fmt.Errorf("ai/ledger/pg insert: %w", err)
	}
	return nil
}

// ByArtifact 实现 Ledger：单产物全生命周期台账行升序投影（W6 成本归集消费面）.
func (p *PGLedger) ByArtifact(ctx context.Context, artifactRef string) ([]LedgerEntry, error) {
	rows, err := p.q.ListAICallLedgerByArtifact(ctx, pgtype.Text{String: artifactRef, Valid: true})
	if err != nil {
		return nil, fmt.Errorf("ai/ledger/pg by_artifact: %w", err)
	}
	out := make([]LedgerEntry, 0, len(rows))
	for _, r := range rows {
		e := LedgerEntry{
			CallID:        r.CallID,
			Modality:      Modality(r.Modality),
			TaskName:      r.TaskName,
			Provider:      r.Provider,
			Model:         r.Model,
			ModelVersion:  r.ModelVersion,
			PromptHash:    r.PromptHash,
			PromptVersion: r.PromptVersion,
			TokenIn:       int(r.TokenIn),
			TokenOut:      int(r.TokenOut),
			CostCNY:       r.CostCny,
			DurationMS:    r.DurationMs,
			Status:        CallStatus(r.Status),
			Fallback:      r.Fallback,
		}
		if r.TaskLevel.Valid {
			e.TaskLevel = TaskLevel(r.TaskLevel.String)
		}
		if r.Reason.Valid {
			e.Reason = r.Reason.String
		}
		if r.ArtifactRef.Valid {
			e.ArtifactRef = r.ArtifactRef.String
		}
		if r.CallerName.Valid {
			e.CallerName = r.CallerName.String
		}
		if r.CreatedAt.Valid {
			e.CreatedAt = r.CreatedAt.Time
		}
		out = append(out, e)
	}
	return out, nil
}

// textNil 把空串折叠为 NULL：task_level=未路由、reason/caller_name/artifact_ref
// 为空即无此维度，落 NULL 而非空串，保持与 alembic 列语义一致.
func textNil(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: s != ""}
}
