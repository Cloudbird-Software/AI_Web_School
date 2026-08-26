package events

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 本套件以 fakeTx 承载 T-W5-017 的全部可本地验证语义（无 Docker/PG，PG 运行时
// 行为不在此宣称覆盖）：
// - fail-closed：无显式事务面的写调用一律 ErrNoTransaction（验收 #1）；
// - 回滚一致性：事件语句与会话状态更新同处外层事务，任一步失败 → 显式
//   Rollback → response_event 计数不变（验收 #3）；
// - 契约字段逐列映射到类型安全参数；nil 可空项 → SQL NULL、空推断集 → []。
// append-only 的 DB 物理强制由迁移 0003 触发器 + Python 侧既有测试覆盖；
// Go 侧无 UPDATE/DELETE 查询面可写（SQL-2），纪律守卫见 guard_test.go。

// errStepFailed 是注入失败的替身错误：扮演「评分失败 / 会话更新失败」这类
// 促使最外层调用方整体回滚的下游故障.
var errStepFailed = errors.New("fake: 下游步骤失败（评分/会话更新替身）")

// errTxClosed 模拟 pgx.Tx 终结后操作的失败语义.
var errTxClosed = errors.New("fake: 事务已终结（Commit/Rollback 之后）")

// stmtKind 已发出语句的分类：insert_event 只来自 dbgen 生成的入账查询；
// other 扮演会话状态更新等他域步骤.
type stmtKind string

const (
	kindInsertEvent stmtKind = "insert_event"
	kindOther       stmtKind = "other"
)

// stmt 是一条已捕获的语句快照（SQL 文本 + 参数副本）.
type stmt struct {
	sql  string
	args []any
}

func (s stmt) kind() stmtKind {
	if strings.Contains(s.sql, "INSERT INTO response_event") {
		return kindInsertEvent
	}
	return kindOther
}

// fakeTx 以最小状态机模拟一个「最外层调用方持有的 pgx.Tx」：Exec 落入 pending
// （未决），Commit 才并入 applied 账，Rollback 丢弃 pending——response_event
// 计数以 applied 计，与真实 DB「未提交不可见」同构。它同时是 D11 边界归属的
// 示范：Commit/Rollback 在这里被调用，因为 fake 正是最外层调用方本人.
type fakeTx struct {
	mu         sync.Mutex
	pending    []stmt
	applied    []stmt
	failNext   bool // 置位后下一次 Exec 返回 errStepFailed 且不入 pending
	done       bool
	committed  bool
	rolledBack bool
}

// 编译期锚定：fake 必须满足本域 Executor——Writer 能装配任意合法执行面.
var _ Executor = (*fakeTx)(nil)

func (f *fakeTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return pgconn.CommandTag{}, errTxClosed
	}
	if f.failNext {
		f.failNext = false
		return pgconn.CommandTag{}, errStepFailed
	}
	cp := make([]any, len(args))
	copy(cp, args)
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	return pgconn.CommandTag{}, nil
}

func (f *fakeTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("events fake: 本套件只走 Exec 写路径")
}

func (f *fakeTx) QueryRow(context.Context, string, ...any) pgx.Row {
	panic("events fake: 本套件只走 Exec 写路径")
}

// Commit 最外层调用方提交：pending 并入 applied 账（事务终结后复用报错）.
func (f *fakeTx) Commit() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errTxClosed
	}
	f.done, f.committed = true, true
	f.applied = append(f.applied, f.pending...)
	f.pending = nil
	return nil
}

// Rollback 最外层调用方回滚：丢弃 pending——已发出的 INSERT 随之消失.
func (f *fakeTx) Rollback() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errTxClosed
	}
	f.done, f.rolledBack = true, true
	f.pending = nil
	return nil
}

// countApplied 按 kind 统计 applied 账中某类语句条数.
func (f *fakeTx) countApplied(t *testing.T, want stmtKind) int {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, s := range f.applied {
		if s.kind() == want {
			n++
		}
	}
	return n
}

// kindsApplied 返回 applied 账的语句序分类切片（断言两步在同一事务内同序在场）.
func (f *fakeTx) kindsApplied(t *testing.T) []stmtKind {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]stmtKind, 0, len(f.applied))
	for _, s := range f.applied {
		out = append(out, s.kind())
	}
	return out
}

// lastPending 取最后一条未决语句（参数映射断言用）.
func (f *fakeTx) lastPending(t *testing.T) stmt {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.pending) == 0 {
		t.Fatal("pending 无语句可检查")
	}
	return f.pending[len(f.pending)-1]
}

// sampleInput 构造契约 §1 全要素齐全的合法输入（13 列全非空场景），mutate
// 注入变体；固定 uuid/时刻保证断言确定性.
func sampleInput(mutate func(*Input)) Input {
	duration := int32(12345)
	testlet := "testlet-001"
	session := "b2222222-3333-4444-8555-666666666666"
	in := Input{
		EventID:        "a1111111-2222-4333-8444-555555555555",
		StudentAliasID: "c3333333-4444-4555-8666-777777777777",
		ItemVersionID:  "sha256:item-v1",
		Scene:          ScenePractice,
		RawPayload:     map[string]any{"selected_option": "A"},
		DurationMs:     &duration,
		ScoringTrace: map[string]any{
			"scorer_id":      "exact_match",
			"scorer_version": "1.0.0+sha256:abc123",
			"process":        map[string]any{"note": "命中点判定"},
			"confidence":     map[string]any{"recognition": 0.0, "scoring": 1.0},
		},
		ErrorInferences: []map[string]any{{
			"error_type_id": "math.decimal.digits_more_is_larger",
			"confidence":    0.85,
			"rule_version":  "1.2.0",
			"evidence":      map[string]any{"selected_option": "B"},
		}},
		TestletID:       &testlet,
		SessionID:       &session,
		AudioPlayEvents: []map[string]any{{"play_count": float64(2)}},
		SourceRef:       map[string]any{"assembly_run_id": "run-abc"},
		CreatedAt:       time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC),
	}
	if mutate != nil {
		mutate(&in)
	}
	return in
}

// mustUUID 解析 uuid 字符串为 pgtype.UUID；解析失败即用例缺陷.
func mustUUID(t *testing.T, s string) pgtype.UUID {
	t.Helper()
	u, err := uuidArg("", s)
	if err != nil {
		t.Fatalf("uuidArg(%q): %v", s, err)
	}
	return u
}

// mustRecord 录入成功即返回 event_id；任何失败都视为用例缺陷.
func mustRecord(t *testing.T, w *Writer, in Input) string {
	t.Helper()
	id, err := w.Record(context.Background(), in)
	if err != nil {
		t.Fatalf("Record 意外失败: %v", err)
	}
	return id
}

// TestWriteWithoutExplicitTransactionIsRejected 是验收 #1 的 fail-closed 面：
// 三种「无显式事务执行面」形态（WithTx(nil)、零值 Writer、nil 接收者）的全部
// 写调用都直接 ErrNoTransaction——非事务上下文里事件写不进去.
func TestWriteWithoutExplicitTransactionIsRejected(t *testing.T) {
	cases := []struct {
		name string
		w    *Writer
	}{
		{"WithTx(nil)", WithTx(nil)},
		{"零值 Writer", &Writer{}},
		{"nil Writer", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tc.w.Record(context.Background(), sampleInput(nil))
			if !errors.Is(err, ErrNoTransaction) {
				t.Fatalf("err = %v, want ErrNoTransaction", err)
			}
		})
	}
}

// TestEventAndSessionStateRollBackTogether 是验收 #3 主用例：事件语句与他域
// 步骤（会话更新）在同一外层事务里先后发出，任一后续失败 → 显式 Rollback →
// response_event 计数不变（applied 账零新增）。旧 Python 实现在 Record 内部
// commit 时，事件此刻已被永久落账、无法随失败回滚（账实分裂）；Go 侧因写入
// 从不自行 commit 而整体消失.
func TestEventAndSessionStateRollBackTogether(t *testing.T) {
	const sessionUpdateSQL = "UPDATE practice_session SET current_index = current_index + 1 WHERE session_id = $1"

	cases := []struct {
		name string
		fail func(f *fakeTx) error // 外层业务流程中的失败步
	}{
		{
			name: "评分失败导致外层回滚",
			fail: func(f *fakeTx) error {
				f.failNext = true // 下一条语句（会话更新）失败，模拟评分链路炸裂后的续步
				_, err := f.Exec(context.Background(), sessionUpdateSQL, "sess-1")
				return err
			},
		},
		{
			name: "会话更新失败导致外层回滚",
			fail: func(f *fakeTx) error {
				if _, err := f.Exec(context.Background(), sessionUpdateSQL, "sess-1"); err != nil {
					t.Fatalf("预埋成功步不应失败: %v", err)
				}
				f.failNext = true
				if _, err := f.Exec(context.Background(), sessionUpdateSQL, "sess-1"); !errors.Is(err, errStepFailed) {
					t.Fatalf("失败步应返回替身错误: %v", err)
				}
				return errStepFailed
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeTx{}
			w := WithTx(f)

			evID := mustRecord(t, w, sampleInput(func(in *Input) { in.EventID = "d4444444-5555-4666-8777-888888888888" }))
			if evID == "" {
				t.Fatal("Record 应返回 event_id")
			}
			if len(f.pending) != 1 {
				t.Fatalf("事件应恰好发出一条未决语句: pending=%d", len(f.pending))
			}
			// 关键差异点：此刻 response_event 尚不可见——写入服务未自作主张提交.

			if ferr := tc.fail(f); ferr == nil {
				t.Fatal("注入的失败步必须真的失败")
			}
			// 最外层调用方感知失败后统一回滚（D11 的边界归属）.
			if err := f.Rollback(); err != nil {
				t.Fatal(err)
			}
			if got := f.countApplied(t, kindInsertEvent); got != 0 {
				t.Fatalf("回滚后 response_event 计数应不变（applied=%d）", got)
			}
			if len(f.pending) != 0 || !f.rolledBack || f.committed {
				t.Fatalf("回滚态不干净: pending=%d rolledBack=%v committed=%v", len(f.pending), f.rolledBack, f.committed)
			}
		})
	}
}

// TestOwnerCommitPersistsBothSteps 对照支：同一流程无失败 → 最外层 Commit 后
// 两类语句按发出序完整进账（事件+会话状态同进退的正向面）；事务终结后继续使用
// 得到明确失败（真实 pgx.Tx 语义，外层面不再是可用写通道）.
func TestOwnerCommitPersistsBothSteps(t *testing.T) {
	f := &fakeTx{}
	w := WithTx(f)

	ctx := context.Background()
	mustRecord(t, w, sampleInput(nil))
	if _, err := f.Exec(ctx, "UPDATE practice_session SET current_index = current_index + 1 WHERE session_id = $1", "sess-1"); err != nil {
		t.Fatal(err)
	}
	if err := f.Commit(); err != nil {
		t.Fatal(err)
	}

	kinds := f.kindsApplied(t)
	if len(kinds) != 2 || kinds[0] != kindInsertEvent || kinds[1] != kindOther {
		t.Fatalf("applied 序应为 [insert_event other]: %v", kinds)
	}
	if n := f.countApplied(t, kindInsertEvent); n != 1 {
		t.Fatalf("Commit 后 response_event 计数应为 1，实际 %d", n)
	}
	if _, err := f.Exec(ctx, "SELECT 1"); !errors.Is(err, errTxClosed) {
		t.Fatalf("tx closed 后继续使用应报错: %v", err)
	}
	if err := f.Rollback(); !errors.Is(err, errTxClosed) {
		t.Fatalf("重复终结应报错: %v", err)
	}
}

// TestWriterNeverIssuesTransactionControlStatements 补充断言：Writer 发出的每条
// 语句都不是 BEGIN/COMMIT/ROLLBACK/SAVEPOINT——事务控制只属于最外层调用方
// （D11 包级红线「本域禁止 Commit/Rollback」的运行时投影；静态面见 guard_test.go）.
func TestWriterNeverIssuesTransactionControlStatements(t *testing.T) {
	f := &fakeTx{}
	w := WithTx(f)
	mustRecord(t, w, sampleInput(nil))
	head := strings.ToUpper(strings.TrimSpace(strings.SplitN(f.lastPending(t).sql, " ", 2)[0]))
	if head == "BEGIN" || head == "COMMIT" || head == "ROLLBACK" || head == "SAVEPOINT" {
		t.Fatalf("写入器发出了事务控制语句 %q（D11 违例）", head)
	}
}

// TestContractFieldsMapToTypedParams 逐列锁死 §1 十三列的参数映射：uuid 解析、
// 场景枚举、毫秒时长、题组/会话文本与时间戳保真.
func TestContractFieldsMapToTypedParams(t *testing.T) {
	f := &fakeTx{}
	w := WithTx(f)
	in := sampleInput(nil)
	mustRecord(t, w, in)

	args := f.lastPending(t).args
	evID, ok := args[0].(pgtype.UUID)
	if !ok || evID.Bytes != mustUUID(t, in.EventID).Bytes || !evID.Valid {
		t.Fatalf("arg[0] 应为 event_id 的解析锚定: %#v", args[0])
	}
	scene, ok := args[3].(dbgen.ResponseEventSceneEnum)
	if !ok || scene != dbgen.ResponseEventSceneEnumPractice {
		t.Fatalf("arg[3] 应为 practice 枚举: %#v", args[3])
	}
	dur, ok := args[5].(pgtype.Int4)
	if !ok || !dur.Valid || dur.Int32 != 12345 {
		t.Fatalf("arg[5] 应为 duration_ms=12345: %#v", args[5])
	}
	tl, ok := args[8].(pgtype.Text)
	if !ok || !tl.Valid || tl.String != "testlet-001" {
		t.Fatalf("arg[8] 应为 testlet_id: %#v", args[8])
	}
	sid, ok := args[9].(pgtype.UUID)
	if !ok || sid.Bytes != mustUUID(t, *in.SessionID).Bytes {
		t.Fatalf("arg[9] 应为 session_id: %#v", args[9])
	}
	ts, ok := args[12].(pgtype.Timestamptz)
	if !ok || !ts.Valid || !ts.Time.Equal(in.CreatedAt) {
		t.Fatalf("arg[12] 应为 created_at 分区键原值: %#v", args[12])
	}
}

// TestNullableContractColumnsMapToSQLNull 可空列全部缺席时：毫秒/题组/会话 →
// pgtype Invalid（驱动送 SQL NULL），音频/来源 → nil 字节（SQL NULL），
// error_inferences → "[]" 空数组而非 null（§1「可为空数组」）.
func TestNullableContractColumnsMapToSQLNull(t *testing.T) {
	f := &fakeTx{}
	w := WithTx(f)
	mustRecord(t, w, sampleInput(func(in *Input) {
		in.DurationMs = nil
		in.TestletID = nil
		in.SessionID = nil
		in.AudioPlayEvents = nil
		in.SourceRef = nil
		in.ErrorInferences = nil
	}))

	args := f.lastPending(t).args
	if d := args[5].(pgtype.Int4); d.Valid {
		t.Fatalf("duration_ms 缺席应为 NULL: %#v", d)
	}
	if tl := args[8].(pgtype.Text); tl.Valid {
		t.Fatalf("testlet_id 缺席应为 NULL: %#v", tl)
	}
	if sid := args[9].(pgtype.UUID); sid.Valid {
		t.Fatalf("session_id 缺席应为 NULL: %#v", sid)
	}
	if audio, ok := args[10].([]byte); !ok || audio != nil {
		t.Fatalf("audio_play_events 缺席应传 SQL NULL（nil 字节）: %#v", args[10])
	}
	if src, ok := args[11].([]byte); !ok || src != nil {
		t.Fatalf("source_ref 缺席应传 SQL NULL（nil 字节）: %#v", args[11])
	}
	if ei := string(args[7].([]byte)); ei != "[]" {
		t.Fatalf("error_inferences 应记空数组: %q", ei)
	}
}

// TestJSONBPayloadRoundTrip JSONB 四字段的序列化保真：结构往返一致、Unicode 与
// HTML 字符按原文序列化不转义（SetEscapeHTML(false)，对齐 Python 冻结实现的
// ensure_ascii=False）——原始保存的可读面，人工审账直读原文（R-D-01）.
func TestJSONBPayloadRoundTrip(t *testing.T) {
	f := &fakeTx{}
	w := WithTx(f)
	raw := map[string]any{
		"note": "<错题>回顾", // HTML 与中文混排：不得转义成 \u003c 也不得 ASCII 化
		"nest": map[string]any{"deep": true},
	}
	mustRecord(t, w, sampleInput(func(in *Input) { in.RawPayload = raw }))

	blob := f.lastPending(t).args[4].([]byte)
	var back map[string]any
	if err := json.Unmarshal(blob, &back); err != nil {
		t.Fatal(err)
	}
	if back["note"] != raw["note"] {
		t.Fatalf("raw_payload 往返失真: %#v vs %#v", back["note"], raw["note"])
	}
	nested, _ := back["nest"].(map[string]any)
	if nested["deep"] != true {
		t.Fatalf("嵌套结构丢失: %#v", back["nest"])
	}
	if strings.Contains(string(blob), "\\u003c") {
		t.Fatalf("JSONB 不当转义（应为原文 <）: %s", blob)
	}
}
