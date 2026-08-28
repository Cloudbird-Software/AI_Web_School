// pg_submit_test.go：PG 提交面的语句面与参数级测试（无 Docker/PG——以脚本化
// pgx.Tx 替身捕获语句序与形参，真库运行时行为不在此宣称覆盖，留给 CI）。
//
// 锁定的语义：
//   - 临界区语句序恒定：advisory lock → 幂等判定（QueryRow）→ [命中即返回，
//     零写入] / 会话行 FOR UPDATE → 校验 → 事件 INSERT → 幂等登记 INSERT →
//     推进 UPDATE；
//   - 幂等命中：只发锁语句与幂等判定，事件/登记/推进零语句（验收 #2）；
//   - 时长保护：rest_prompted 置位语句 + ErrRestRequired，零事件写入；
//   - 题序违例：仅锁语句，零写入。
package session

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// submitScriptTx 是按序脚本的 pgx.Tx 替身：Exec 捕获语句（可按语句内容注入
// 驱动错误），QueryRow 按脚本顺序逐个消费（幂等判定 → 会话行，恰好两次）。
// core/events、core/scoring 与本包题序面测试（fakeTx）同惯例；因提交路径含
// 两次 QueryRow，需要按序多行脚本，故独立成不与 fakeTx 混用的替身.
type submitScriptTx struct {
	mu        sync.Mutex
	execStmts []capturedStmt         // 已执行的 Exec 语句（按序）
	failIf    func(sql string) error // 命中即返回该错误（23505 定位注入用）
	rows      []pgx.Row              // QueryRow 脚本（按序消费）
	rowCalls  int
}

var _ Executor = (*submitScriptTx)(nil)

func (f *submitScriptTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]any, len(args))
	copy(cp, args)
	f.execStmts = append(f.execStmts, capturedStmt{sql: sql, args: cp})
	if f.failIf != nil {
		if err := f.failIf(sql); err != nil {
			return pgconn.CommandTag{}, err
		}
	}
	return pgconn.CommandTag{}, nil
}

func (f *submitScriptTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("submit fake: 本套件只走 Exec/QueryRow")
}

func (f *submitScriptTx) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.rowCalls >= len(f.rows) {
		panic(fmt.Sprintf("submit fake: 第 %d 次 QueryRow 未脚本化", f.rowCalls+1))
	}
	r := f.rows[f.rowCalls]
	f.rowCalls++
	return r
}

// scriptedRow 用反射按序回填 Scan 目标（类型必须与生成层逐位一致）.
func scriptedRow(cells ...any) pgx.Row {
	return &fakeRow{scan: func(dest ...any) error {
		if len(dest) != len(cells) {
			return fmt.Errorf("submit fake: Scan 目标数 %d ≠ 脚本 %d", len(dest), len(cells))
		}
		for i := range cells {
			reflect.ValueOf(dest[i]).Elem().Set(reflect.ValueOf(cells[i]))
		}
		return nil
	}}
}

// errRow 恒返回驱动语义错误（pgx.ErrNoRows = 幂等未命中/会话不存在的脚本）.
func errRow(err error) pgx.Row {
	return &fakeRow{scan: func(dest ...any) error { return err }}
}

// scriptedSessionRow 装配一条 practice_session 行（列序与生成层 Scan 逐位一致）.
func scriptedSessionRow(t *testing.T, status string, seq []string, currentIndex int32, timeLimitSec int32, lastResume time.Time) pgx.Row {
	t.Helper()
	seqJSON := `[{"item_version_id":"` + strings.Join(seq, `"},{"item_version_id":"`) + `"}]`
	if len(seq) == 0 {
		seqJSON = "[]"
	}
	sid := mustUUIDBytes(t, subSession)
	alias := mustUUIDBytes(t, subAlias)
	return scriptedRow(
		pgtype.UUID{Bytes: sid, Valid: true},
		pgtype.UUID{Bytes: alias, Valid: true},
		ScenePractice, GradebandMid, status,
		pgtype.Text{}, // paper_id NULL（实例池路径）
		[]byte(seqJSON),
		currentIndex,
		false, // retest_wrong
		[]byte("[]"),
		timeLimitSec, int32(0), int32(0),
		tsTZ(lastResume), tsTZ(lastResume), tsTZ(lastResume),
		pgtype.Timestamptz{}, // completed_at NULL
		tsTZ(lastResume),
	)
}

// scriptDedupHit 装配幂等命中行（event_id + event_created_at 复合回指）.
func scriptDedupHit(t *testing.T, eventID string, at time.Time) pgx.Row {
	t.Helper()
	return scriptedRow(
		pgtype.UUID{Bytes: mustUUIDBytes(t, eventID), Valid: true},
		tsTZ(at),
	)
}

func mustUUIDBytes(t *testing.T, s string) [16]byte {
	t.Helper()
	var u pgtype.UUID
	if err := u.Scan(s); err != nil || !u.Valid {
		t.Fatalf("%q 不是合法 UUID: %v", s, err)
	}
	return u.Bytes
}

// stmtKinds 把已执行语句归类为可读序（断言临界区语句序恒定的断言面）.
func stmtKinds(stmts []capturedStmt) []string {
	kinds := make([]string, len(stmts))
	for i, s := range stmts {
		switch {
		case strings.Contains(s.sql, "pg_advisory_xact_lock"):
			kinds[i] = "advisory-lock"
		case strings.Contains(s.sql, "INSERT INTO response_event"):
			kinds[i] = "insert-event"
		case strings.Contains(s.sql, "INSERT INTO response_submission"):
			kinds[i] = "insert-submission"
		case strings.Contains(s.sql, "rest_prompted"):
			kinds[i] = "rest-prompted"
		case strings.Contains(s.sql, "UPDATE practice_session"):
			kinds[i] = "advance"
		default:
			kinds[i] = "other:" + s.sql
		}
	}
	return kinds
}

func wantKinds(t *testing.T, got, want []string) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("语句序漂移:\n got %v\nwant %v", got, want)
	}
}

// TestPGStore_Submit_IdempotentHitStatementSurface 幂等命中：锁 + 幂等判定
// 之外零语句（事件/登记/推进零写入），返回首次事件 id.
func TestPGStore_Submit_IdempotentHitStatementSurface(t *testing.T) {
	firstAt := subBase
	firstID := "44444444-5555-4666-8777-888888888888"
	tx := &submitScriptTx{rows: []pgx.Row{scriptDedupHit(t, firstID, firstAt)}}

	id, dup, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            subBase.Add(time.Minute),
	})
	if err != nil {
		t.Fatalf("幂等命中必须成功: %v", err)
	}
	if !dup || id != firstID {
		t.Fatalf("命中应返回首次结果: dup=%v id=%s", dup, id)
	}
	wantKinds(t, stmtKinds(tx.execStmts), []string{"advisory-lock"})
	if tx.rowCalls != 1 {
		t.Fatalf("命中路径不得触碰会话行: QueryRow×%d", tx.rowCalls)
	}
}

// TestPGStore_Submit_FullPathStatementOrderAndParams 未命中的完整提交：
// 语句序 = 锁 → 事件入账 → 幂等登记 → 推进；登记行形参（三元组 + 事件回指 +
// 时刻）与指纹口径逐位锚定.
func TestPGStore_Submit_FullPathStatementOrderAndParams(t *testing.T) {
	tx := &submitScriptTx{rows: []pgx.Row{
		errRow(pgx.ErrNoRows), // 幂等未命中
		scriptedSessionRow(t, StatusActive, []string{"item-aaa", "item-bbb"}, 0, 1800, subBase),
	}}
	at := subBase.Add(30 * time.Second)
	id, dup, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		DurationMs:    ptrInt32(9000),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            at,
	})
	if err != nil || dup {
		t.Fatalf("未命中提交必须真实入账: dup=%v err=%v", dup, err)
	}
	wantKinds(t, stmtKinds(tx.execStmts), []string{"advisory-lock", "insert-event", "insert-submission", "advance"})
	if _, err := uuidArgCheck(id); err != nil {
		t.Fatalf("返回事件 id 必须是合法 UUID: %v", err)
	}

	// 幂等登记行形参逐位锚定（三元组键 + 事件复合回指 + 双时刻）.
	ins := tx.execStmts[2]
	arg := dbgen.InsertResponseSubmissionParams{
		SessionID:      pgtype.UUID{Bytes: mustUUIDBytes(t, subSession), Valid: true},
		ItemVersionID:  "item-aaa",
		AnswerDigest:   mustSubmitFingerprint(t, "item-aaa", answer("B")),
		EventID:        pgtype.UUID{Bytes: mustUUIDBytes(t, id), Valid: true},
		EventCreatedAt: tsTZ(at),
		CreatedAt:      tsTZ(at),
	}
	if !reflect.DeepEqual(ins.args, []any{
		arg.SessionID, arg.ItemVersionID, arg.AnswerDigest, arg.EventID, arg.EventCreatedAt, arg.CreatedAt,
	}) {
		t.Fatalf("幂等登记形参漂移:\n got %v\nwant %v", ins.args, arg)
	}
	// 事件入账形参抽查：题目/场景/会话归属取会话行（学生身份不信任请求面）.
	ev := tx.execStmts[1]
	if ev.args[2] != "item-aaa" || ev.args[3] != dbgen.ResponseEventSceneEnum(ScenePractice) {
		t.Fatalf("事件入账题目/场景漂移: %v / %v", ev.args[2], ev.args[3])
	}
	wantSID := mustUUIDBytes(t, subSession)
	if sid, ok := ev.args[9].(pgtype.UUID); !ok || !sid.Valid || string(sid.Bytes[:]) != string(wantSID[:]) {
		t.Fatalf("事件入账 session_id 漂移: %v", ev.args[9])
	}
}

// TestPGStore_Submit_RestPromptedZeroEventWrites 时长保护：置位语句 + 哨兵，
// 零事件写入（Python _check_time_protection 同语义的语句面锚定）.
func TestPGStore_Submit_RestPromptedZeroEventWrites(t *testing.T) {
	tx := &submitScriptTx{rows: []pgx.Row{
		errRow(pgx.ErrNoRows),
		scriptedSessionRow(t, StatusActive, []string{"item-bbb"}, 0, 1800, subBase),
	}}
	_, _, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-bbb",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            subBase.Add(1801 * time.Second),
	})
	var rre *RestRequiredError
	if !errors.As(err, &rre) || !errors.Is(err, ErrRestRequired) {
		t.Fatalf("超时提交必须锚定时长保护哨兵: %v", err)
	}
	wantKinds(t, stmtKinds(tx.execStmts), []string{"advisory-lock", "rest-prompted"})
}

// TestPGStore_Submit_OutOfSequenceNoWrites 题序违例：锁之后零写语句，失败锚定
// 题序哨兵（异常不泄漏）.
func TestPGStore_Submit_OutOfSequenceNoWrites(t *testing.T) {
	tx := &submitScriptTx{rows: []pgx.Row{
		errRow(pgx.ErrNoRows),
		scriptedSessionRow(t, StatusActive, []string{"item-aaa", "item-bbb"}, 1, 1800, subBase),
	}}
	_, _, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            subBase,
	})
	if !errors.Is(err, ErrOutOfSequence) {
		t.Fatalf("异指纹跳答必须锚定题序哨兵: %v", err)
	}
	wantKinds(t, stmtKinds(tx.execStmts), []string{"advisory-lock"})
}

// TestPGStore_Submit_UniqueBackstopMapsSentinel 幂等登记 23505 →
// ErrSubmissionConflict（数据库层防线的明确失败信号，异常不泄漏）.
func TestPGStore_Submit_UniqueBackstopMapsSentinel(t *testing.T) {
	tx := &submitScriptTx{
		failIf: func(sql string) error {
			if strings.Contains(sql, "INSERT INTO response_submission") {
				return uniqueViolationErr("pk_response_submission")
			}
			return nil
		},
		rows: []pgx.Row{
			errRow(pgx.ErrNoRows),
			scriptedSessionRow(t, StatusActive, []string{"item-aaa"}, 0, 1800, subBase),
		},
	}
	_, _, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            subBase,
	})
	if !errors.Is(err, ErrSubmissionConflict) {
		t.Fatalf("登记 23505 必须映射为 ErrSubmissionConflict: %v", err)
	}
}

// TestPGStore_Submit_SessionNotFound 未脚本化的会话行 ErrNoRows →
// ErrSessionNotFound（锁已发、零写语句）.
func TestPGStore_Submit_SessionNotFound(t *testing.T) {
	tx := &submitScriptTx{rows: []pgx.Row{errRow(pgx.ErrNoRows), errRow(pgx.ErrNoRows)}}
	_, _, err := NewPGStore().SubmitAnswer(context.Background(), tx, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
	})
	if !errors.Is(err, ErrSessionNotFound) {
		t.Fatalf("不存在会话必须锚定哨兵: %v", err)
	}
	wantKinds(t, stmtKinds(tx.execStmts), []string{"advisory-lock"})
}

func mustSubmitFingerprint(t *testing.T, item string, response map[string]any) string {
	t.Helper()
	d, err := fingerprint(item, response)
	if err != nil {
		t.Fatalf("指纹计算: %v", err)
	}
	return d
}

// uuidArgCheck 校验字符串为合法 UUID 形（复用 pgtype 扫描口径）.
func uuidArgCheck(s string) (pgtype.UUID, error) {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil {
		return pgtype.UUID{}, err
	}
	return u, nil
}
