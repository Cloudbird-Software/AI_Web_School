package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// 编译期锚定：PG 实现兑现 RuntimeStore（内存实现锚定见 runtime_memory.go
// 与 service.go）.
var _ RuntimeStore = (*PGStore)(nil)

// runtime_pg.go 是 RuntimeStore（会话运行态账服务面）的 PG 实现：语句全部
// 来自 db/queries/session.sql 的类型安全生成方法（SQL-2，本文件零 SQL 字符串）
// ——运行态读取面 GetPracticeSessionRuntime、休息确认 ResumeSessionAfterRest、
// 放弃 AbandonSessionByID、时长保护置位 MarkSessionRestPrompted。
//
// 事务纪律（D11）：本实现不自 begin/commit；q 为调用方（SessionService 的
// TxRunner）传入的显式事务执行面，nil 即 ErrNoTransaction fail-closed。拒绝
// 类语义（completed/abandoned 不能休息/放弃）由本实现读态判定后拒绝——拒绝
// 发生在写之前，UPDATE 语句不带状态谓词、不在写中半途失败.

// RuntimeState 实现 RuntimeStore：运行态只读投影（归属断言/取题判定/状态
// 投影的取数前提；普通读不锁行——写互斥由各写语句面承担）.
func (s *PGStore) RuntimeState(ctx context.Context, q Executor, sessionID string) (*SessionRuntime, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	row, err := dbgen.New(q).GetPracticeSessionRuntime(ctx, parseSessionUUID(sessionID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: session_id=%s", ErrSessionNotFound, sessionID)
		}
		return nil, fmt.Errorf("session/pg load session runtime: %w", err)
	}
	return runtimeFromGen(&row)
}

// Resume 实现 RuntimeStore：读态拒绝（completed/abandoned）→ 计时锚点重置
// UPDATE（RETURNING * 就地装配投影，免二次读）.
func (s *PGStore) Resume(ctx context.Context, q Executor, sessionID string, at time.Time) (*SessionRuntime, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	current, err := s.RuntimeState(ctx, q, sessionID)
	if err != nil {
		return nil, err
	}
	switch current.Status {
	case StatusCompleted, StatusAbandoned:
		return nil, fmt.Errorf("%w: 会话已 %s，不能休息确认", ErrSessionState, current.Status)
	}
	ts := resumeAtOr(at, time.Now)
	row, err := dbgen.New(q).ResumeSessionAfterRest(ctx, dbgen.ResumeSessionAfterRestParams{
		SessionID:    parseSessionUUID(sessionID),
		LastResumeAt: tsTZ(ts),
	})
	if err != nil {
		return nil, fmt.Errorf("session/pg resume session: %w", err)
	}
	return runtimeFromGen(&row)
}

// Abandon 实现 RuntimeStore：completed 拒绝 → abandoned 置位（已作答事件
// 保留在 response_event 账——append-only 无删除面）.
func (s *PGStore) Abandon(ctx context.Context, q Executor, sessionID string, at time.Time) (*SessionRuntime, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	current, err := s.RuntimeState(ctx, q, sessionID)
	if err != nil {
		return nil, err
	}
	if current.Status == StatusCompleted {
		return nil, fmt.Errorf("%w: 会话已完成，不能放弃", ErrSessionState)
	}
	ts := resumeAtOr(at, time.Now)
	row, err := dbgen.New(q).AbandonSessionByID(ctx, dbgen.AbandonSessionByIDParams{
		SessionID:      parseSessionUUID(sessionID),
		LastActivityAt: tsTZ(ts),
	})
	if err != nil {
		return nil, fmt.Errorf("session/pg abandon session: %w", err)
	}
	return runtimeFromGen(&row)
}

// MarkRestPrompted 实现 RuntimeStore：时长保护置位（零事件写入）.
func (s *PGStore) MarkRestPrompted(ctx context.Context, q Executor, sessionID string) error {
	if q == nil {
		return ErrNoTransaction
	}
	if err := dbgen.New(q).MarkSessionRestPrompted(ctx, parseSessionUUID(sessionID)); err != nil {
		return fmt.Errorf("session/pg rest prompted: %w", err)
	}
	return nil
}

// parseSessionUUID 把会话 id 解析为生成层形参形状（非法 id 即不存在的会话——
// 与内存实现按键未命中同一条哨兵语义，实现间无漂移面）.
func parseSessionUUID(sessionID string) pgtype.UUID {
	var sid pgtype.UUID
	if err := sid.Scan(sessionID); err != nil || !sid.Valid {
		return pgtype.UUID{}
	}
	return sid
}

// resumeAtOr 归一迁移时刻（零值回落实现的时钟）.
func resumeAtOr(at time.Time, now func() time.Time) time.Time {
	if at.IsZero() {
		return now()
	}
	return at
}

// runtimeFromGen 把 practice_session 行装配为服务域运行态投影：题序经包内
// decodeEntries（Seq 升序 canonical，含 placement_token）；错题标记从账面
// JSONB 反序列化为独立拷贝（调用方改不动内部账）.
func runtimeFromGen(row *dbgen.PracticeSession) (*SessionRuntime, error) {
	entries, err := decodeEntries(row.ItemSequence)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrLedgerCorrupted, err)
	}
	marks, err := decodeWrongMarks(row.WrongMarks)
	if err != nil {
		return nil, err
	}
	rt := &SessionRuntime{
		SessionID:      formatUUID(row.SessionID.Bytes),
		StudentAliasID: formatUUID(row.StudentAliasID.Bytes),
		Scene:          row.Scene,
		Gradeband:      row.Gradeband,
		Status:         row.Status,
		RetestWrong:    row.RetestWrong,
		Entries:        entries,
		CurrentIndex:   int(row.CurrentIndex),
		AnsweredCount:  int(row.AnsweredCount),
		CorrectCount:   int(row.CorrectCount),
		WrongMarks:     marks,
		TimeLimitSec:   int(row.TimeLimitSec),
		StartedAt:      row.StartedAt.Time,
		LastResumeAt:   row.LastResumeAt.Time,
		LastActivityAt: row.LastActivityAt.Time,
	}
	if row.PaperID.Valid {
		pid := row.PaperID.String
		rt.PaperID = &pid
	}
	if row.CompletedAt.Valid {
		t := row.CompletedAt.Time
		rt.CompletedAt = &t
	}
	return rt, nil
}

// decodeWrongMarks 反序列化错题标记账面（空账/nil → nil 投影；反序列化失败
// 按账损处理——fail-closed，不带病投影）.
func decodeWrongMarks(raw []byte) ([]map[string]any, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var out []map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("%w: wrong_marks 反序列化失败（账损防御路径）: %w", ErrLedgerCorrupted, err)
	}
	return out, nil
}
