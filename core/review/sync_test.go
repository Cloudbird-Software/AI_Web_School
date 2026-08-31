// sync_test.go：复习队列同步写面的语句级测试（无 DB——fake 执行面捕获
// SQL 参数，真库运行时行为由 E2E 覆盖）。
//
// 锁定的语义：
//   - 错题事件 → 恰一条 upsert（stage=0/pending，due = 事件时刻 + intervals[0]）；
//   - 答对事件（未在队）→ 零 upsert；
//   - 事件排序与对错判定经 SQL 投影 + DeriveCorrectness 喂入 RebuildQueue；
//   - 策略缺失 → ErrPolicyNotFound（fail-closed，不猜间隔）；
//   - 事件账 JSONB 损坏 → ErrLedgerCorrupted（fail-closed，不带病排程）。
package review

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// fakeSyncDB 是同步面的 fake 执行面：QueryRow 只服务 GetReviewPolicy，
// Query 只服务 ListStudentReviewEvents，Exec 捕获全部 upsert 参数.
type fakeSyncDB struct {
	policyRow    []any   // GetReviewPolicy 的 Scan 桩（nil = ErrNoRows）
	eventRows    [][]any // 事件投影行（event_id, ivid, created_at, trace, inferences）
	execCalls    [][]any // UpsertReviewQueueEntry 参数快照（按序）
	execFailWith error   // 命中 Exec 即返回该错误
}

func (f *fakeSyncDB) Exec(_ context.Context, _ string, args ...any) (pgconn.CommandTag, error) {
	f.execCalls = append(f.execCalls, args)
	if f.execFailWith != nil {
		return pgconn.CommandTag{}, f.execFailWith
	}
	return pgconn.CommandTag{}, nil
}

func (f *fakeSyncDB) Query(_ context.Context, sql string, _ ...any) (pgx.Rows, error) {
	if reflect.TypeOf(sql).Kind() != reflect.String {
		return nil, errors.New("unreachable")
	}
	// ListStudentReviewEvents 是本面唯一的 Query 语句
	return &fakeSyncRows{rows: f.eventRows}, nil
}

func (f *fakeSyncDB) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	if f.policyRow == nil {
		return errSyncRow{}
	}
	return &scanSyncRow{cells: f.policyRow}
}

// scanSyncRow 用反射按序回填 Scan 目标（GetReviewPolicy：string/string/[]byte）.
type scanSyncRow struct{ cells []any }

func (r *scanSyncRow) Scan(dest ...any) error {
	if len(dest) != len(r.cells) {
		return fmt.Errorf("sync fake: Scan 目标数 %d ≠ 桩 %d", len(dest), len(r.cells))
	}
	for i := range r.cells {
		reflect.ValueOf(dest[i]).Elem().Set(reflect.ValueOf(r.cells[i]))
	}
	return nil
}

type errSyncRow struct{}

func (errSyncRow) Scan(...any) error { return pgx.ErrNoRows }

// fakeSyncRows 最小 pgx.Rows（同 core/knowledge/pg_sink_test.go 惯例）.
type fakeSyncRows struct {
	rows [][]any
	i    int
}

func (r *fakeSyncRows) Next() bool {
	if r.i < len(r.rows) {
		r.i++
		return true
	}
	return false
}

func (r *fakeSyncRows) Scan(dest ...any) error {
	row := r.rows[r.i-1]
	if len(dest) != len(row) {
		return fmt.Errorf("sync fake: scan 目标 %d 列 ≠ 桩 %d 列", len(dest), len(row))
	}
	for j, d := range dest {
		if err := scanSyncCell(d, row[j]); err != nil {
			return err
		}
	}
	return nil
}

func scanSyncCell(dest, cell any) error {
	switch d := dest.(type) {
	case *pgtype.UUID:
		d.Valid = true
		copy(d.Bytes[:], cell.([]byte))
	case *string:
		*d = cell.(string)
	case *pgtype.Timestamptz:
		d.Time = cell.(time.Time)
		d.Valid = true
	case *[]byte:
		*d = cell.([]byte)
	default:
		return fmt.Errorf("sync fake: 不支持的 scan 目标 %T", dest)
	}
	return nil
}

func (r *fakeSyncRows) Close()                                       {}
func (r *fakeSyncRows) Err() error                                   { return nil }
func (r *fakeSyncRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *fakeSyncRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *fakeSyncRows) RawValues() [][]byte                          { return nil }
func (r *fakeSyncRows) Values() ([]any, error)                       { return nil, errors.New("sync fake: 未用") }
func (r *fakeSyncRows) Conn() *pgx.Conn                              { return nil }

// mkUUID 构造可扫描的 16 字节 UUID 桩.
func mkUUID(seed byte) []byte {
	b := make([]byte, 16)
	for i := range b {
		b[i] = seed
	}
	return b
}

// eventRow 装配事件投影桩（trace correct 显式判定；inferences 空）.
func eventRow(eventSeed byte, ivid string, at time.Time, correct *bool, typeIDs []string) []any {
	trace := map[string]any{}
	if correct != nil {
		trace["process"] = map[string]any{"correct": *correct}
	}
	traceJSON, _ := json.Marshal(trace)
	inf := []any{}
	for _, id := range typeIDs {
		inf = append(inf, map[string]any{"error_type_id": id})
	}
	infJSON, _ := json.Marshal(inf)
	return []any{mkUUID(eventSeed), ivid, at, traceJSON, infJSON}
}

func TestSyncQueue_WrongAnswerEnqueues(t *testing.T) {
	at := time.Date(2026, 8, 31, 10, 0, 0, 0, time.UTC)
	wrong := false
	db := &fakeSyncDB{
		policyRow: []any{"fixed-interval", "1.0.0", []byte(`[1,3,7,21]`)},
		eventRows: [][]any{eventRow(1, "iv-1", at, &wrong, []string{"err.cmp.dec.reverse-order"})},
	}
	svc := NewSyncService(db)
	n, err := svc.SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", DefaultPolicyID, DefaultPolicyVersion, at)
	if err != nil {
		t.Fatalf("SyncQueue: %v", err)
	}
	if n != 1 {
		t.Fatalf("在队条目数 = %d, want 1", n)
	}
	if len(db.execCalls) != 1 {
		t.Fatalf("upsert 次数 = %d, want 1", len(db.execCalls))
	}
	// 参数序与 UpsertReviewQueueEntryParams 字段序一致（sqlc 生成面）
	args := db.execCalls[0]
	if got := args[2].(string); got != "iv-1" {
		t.Fatalf("item_version_id = %q, want iv-1", got)
	}
	stage := args[5].(int32)
	if stage != 0 {
		t.Fatalf("stage = %d, want 0", stage)
	}
	if got := args[6].(string); got != StatusPending {
		t.Fatalf("status = %q, want pending", got)
	}
	if got := args[7].(pgtype.Text).String; got != "err.cmp.dec.reverse-order" {
		t.Fatalf("source_error_type_id = %q", got)
	}
	due := args[10].(pgtype.Timestamptz).Time.UTC()
	wantDue := at.AddDate(0, 0, 1)
	if !due.Equal(wantDue) {
		t.Fatalf("due_at = %v, want %v（事件时刻 + intervals[0]）", due, wantDue)
	}
}

func TestSyncQueue_CorrectOnlyNoEntries(t *testing.T) {
	at := time.Now().UTC()
	correct := true
	db := &fakeSyncDB{
		policyRow: []any{"fixed-interval", "1.0.0", []byte(`[1,3,7,21]`)},
		eventRows: [][]any{eventRow(1, "iv-1", at, &correct, nil)},
	}
	n, err := NewSyncService(db).SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", DefaultPolicyID, DefaultPolicyVersion, at)
	if err != nil {
		t.Fatalf("SyncQueue: %v", err)
	}
	if n != 0 {
		t.Fatalf("答对且未在队：条目数 = %d, want 0（答对不是错题，不重新入队）", n)
	}
	if len(db.execCalls) != 0 {
		t.Fatalf("答对且未在队：upsert 次数 = %d, want 0", len(db.execCalls))
	}
}

func TestSyncQueue_AdvanceOnCorrectAfterWrong(t *testing.T) {
	at := time.Date(2026, 8, 31, 10, 0, 0, 0, time.UTC)
	wrong, right := false, true
	db := &fakeSyncDB{
		policyRow: []any{"fixed-interval", "1.0.0", []byte(`[1,3,7,21]`)},
		eventRows: [][]any{
			eventRow(1, "iv-1", at, &wrong, []string{"err.x"}),
			eventRow(2, "iv-1", at.Add(time.Hour), &right, nil),
		},
	}
	n, err := NewSyncService(db).SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", DefaultPolicyID, DefaultPolicyVersion, at.Add(2*time.Hour))
	if err != nil {
		t.Fatalf("SyncQueue: %v", err)
	}
	if n != 1 || len(db.execCalls) != 1 {
		t.Fatalf("条目 %d / upsert %d, want 1/1", n, len(db.execCalls))
	}
	args := db.execCalls[0]
	if got := args[5].(int32); got != 1 {
		t.Fatalf("答对推进 stage = %d, want 1", got)
	}
	due := args[10].(pgtype.Timestamptz).Time.UTC()
	wantDue := at.Add(time.Hour).AddDate(0, 0, 3) // 答对时刻 + intervals[1]
	if !due.Equal(wantDue) {
		t.Fatalf("due_at = %v, want %v", due, wantDue)
	}
}

func TestSyncQueue_PolicyMissing(t *testing.T) {
	db := &fakeSyncDB{} // policyRow nil → ErrNoRows
	_, err := NewSyncService(db).SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", "no-such", "9.9.9", time.Now().UTC())
	if !errors.Is(err, ErrPolicyNotFound) {
		t.Fatalf("err = %v, want ErrPolicyNotFound", err)
	}
}

func TestSyncQueue_LedgerCorrupted(t *testing.T) {
	at := time.Now().UTC()
	db := &fakeSyncDB{
		policyRow: []any{"fixed-interval", "1.0.0", []byte(`[1,3,7,21]`)},
		eventRows: [][]any{{mkUUID(1), "iv-1", at, []byte(`{not-json`), []byte(`[]`)}},
	}
	_, err := NewSyncService(db).SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", DefaultPolicyID, DefaultPolicyVersion, at)
	if !errors.Is(err, ErrLedgerCorrupted) {
		t.Fatalf("err = %v, want ErrLedgerCorrupted", err)
	}
}

func TestSyncQueue_NoExecutorFailClosed(t *testing.T) {
	svc := NewSyncService(nil)
	if _, err := svc.SyncQueue(context.Background(), "e5f120c2-5976-4e84-b26a-54ec46658375", DefaultPolicyID, DefaultPolicyVersion, time.Now().UTC()); !errors.Is(err, ErrNoExecutor) {
		t.Fatalf("err = %v, want ErrNoExecutor", err)
	}
}
