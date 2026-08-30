// 复习域只读查询服务（GO-RW-006 服务化接线 / 审计 #155）：
// GET /review/due 的全部 DB 取证经本服务，api 层零 SQL、零行归零知识。
//
// 装配纪律与 core/report.WeaknessQueryService 同构：不持有连接、不开事务；
// db 为 nil 构造不报错但全部查询立即返回 ErrNoExecutor（fail-closed）。
package review

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// ErrNoExecutor 是查询面未装配（db 为 nil）的哨兵。
var ErrNoExecutor = errors.New("review: 查询面未装配")

// Executor 是只读执行面最小接口（pgxpool 连接 / pgx.Tx 均满足）.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// DueEntryProjection 是到期复习条目的服务化投影（字段与冻结契约
// ReviewQueueEntryPydantic 一一对应；UUID/时刻以文本/UTC 承载）.
type DueEntryProjection struct {
	EntryID           string
	StudentAliasID    string
	ItemVersionID     string
	PolicyID          string
	PolicyVersion     string
	Stage             int
	Status            string
	SourceErrorTypeID *string
	EnqueuedAt        time.Time
	DueAt             time.Time
}

// DueQueryService 是复习到期取题的只读取证面。
type DueQueryService struct {
	db Executor
	qs *dbgen.Queries
}

// NewDueQueryService 把只读执行面绑定为复习查询服务。
func NewDueQueryService(db Executor) *DueQueryService {
	return &DueQueryService{db: db, qs: dbgen.New(db)}
}

// DueEntries 取某学生已到期的在队复习条目（最逾期优先；判定与
// DueReviews 纯函数同口径：status=pending 且 due_at <= now）.
func (s *DueQueryService) DueEntries(ctx context.Context, studentAliasID string, now time.Time, limit int) ([]DueEntryProjection, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	alias, err := parseUUID(studentAliasID)
	if err != nil {
		return nil, err
	}
	rows, err := s.qs.ListDueReviewEntries(ctx, dbgen.ListDueReviewEntriesParams{
		StudentAliasID: alias,
		DueAt:          pgtype.Timestamptz{Time: now.UTC(), Valid: true},
		Limit:          int32(limit),
	})
	if err != nil {
		return nil, fmt.Errorf("review: list due entries: %w", err)
	}
	out := make([]DueEntryProjection, 0, len(rows))
	for _, row := range rows {
		p := DueEntryProjection{
			EntryID:        uuidText(row.EntryID),
			StudentAliasID: uuidText(row.StudentAliasID),
			ItemVersionID:  row.ItemVersionID,
			PolicyID:       row.PolicyID,
			PolicyVersion:  row.PolicyVersion,
			Stage:          int(row.Stage),
			Status:         row.Status,
			EnqueuedAt:     row.EnqueuedAt.Time.UTC(),
			DueAt:          row.DueAt.Time.UTC(),
		}
		if row.SourceErrorTypeID.Valid {
			v := row.SourceErrorTypeID.String
			p.SourceErrorTypeID = &v
		}
		out = append(out, p)
	}
	return out, nil
}

func parseUUID(s string) (pgtype.UUID, error) {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil {
		return pgtype.UUID{}, fmt.Errorf("review: student_alias_id 非法 UUID: %w", err)
	}
	return u, nil
}

func uuidText(u pgtype.UUID) string {
	if !u.Valid {
		return ""
	}
	return formatUUID(u.Bytes)
}

// formatUUID 与 core/compliance.formatUUID 同构（RFC 4122 连字符形态）.
func formatUUID(b [16]byte) string {
	var buf [36]byte
	hex.Encode(buf[0:8], b[0:4])
	buf[8] = '-'
	hex.Encode(buf[9:13], b[4:6])
	buf[13] = '-'
	hex.Encode(buf[14:18], b[6:8])
	buf[18] = '-'
	hex.Encode(buf[19:23], b[8:10])
	buf[23] = '-'
	hex.Encode(buf[24:36], b[10:16])
	return string(buf[:])
}
