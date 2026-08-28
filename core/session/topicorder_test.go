// T-W5-004：会话题序写入/读取面的域级单测。
// 锁三件事：①题序固化幂等语义（同 session 重放=幂等成功，不同=明确冲突）；
// ②seq 唯一冲突映射（应用层哨兵 + 23505 语义判读，异常不泄漏）；③「题序行
// UPDATE/DELETE 被拒」在 Go 面的结构等价物——查询面只有 INSERT/SELECT（静态
// 守卫 + fakeTx 捕获），改写语句无查询面可写；真库触发器行为由 CI
// migrate-go-check 全量 cycle 复核（本机无 Docker/PG，如实声明）。
package session

import (
	"context"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── 测试数据 ────────────────────────────────────────────────────────────────

const (
	testOrderAlias   = "11111111-2222-4333-8444-555555555555"
	testOrderSession = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
)

var testStart = time.Unix(1_700_000_000, 0).UTC()

// seqEntries 把调用方给定顺序的题目 id 装配为 seq=1..N 的题序（冻结 pool 路径
// enumerate 同形；placement_token 实例池路径为 nil）.
func seqEntries(ids ...string) []TopicEntry {
	entries := make([]TopicEntry, len(ids))
	for i, id := range ids {
		entries[i] = TopicEntry{Seq: i + 1, ItemVersionID: id}
	}
	return entries
}

func startInput(entries []TopicEntry) StartInput {
	return StartInput{
		SessionID:      testOrderSession,
		StudentAliasID: testOrderAlias,
		Scene:          ScenePractice,
		Gradeband:      GradebandLow,
		Entries:        entries,
		StartedAt:      testStart,
	}
}

// ── prepareStart：校验矩阵（内存/PG 两实现共用的前置管线，判据单一来源）──────

func TestPrepareStart_Validation(t *testing.T) {
	good := seqEntries("iv-1", "iv-2")
	cases := []struct {
		name    string
		mutate  func(*StartInput)
		wantErr error
	}{
		{"scene越域", func(i *StartInput) { i.Scene = "measurement" }, ErrInvalidSessionStart},
		{"gradeband越域", func(i *StartInput) { i.Gradeband = "X" }, ErrInvalidSessionStart},
		{"alias非法", func(i *StartInput) { i.StudentAliasID = "not-a-uuid" }, ErrInvalidSessionStart},
		{"session_id非法", func(i *StartInput) { i.SessionID = "nope" }, ErrInvalidSessionStart},
		{"题序为空", func(i *StartInput) { i.Entries = nil }, ErrInvalidTopicOrder},
		{"条目id为空", func(i *StartInput) { i.Entries[0].ItemVersionID = "" }, ErrInvalidTopicOrder},
		{"seq越下界", func(i *StartInput) { i.Entries[0].Seq = 0 }, ErrInvalidTopicOrder},
		{"seq重复", func(i *StartInput) { i.Entries[1].Seq = 1 }, ErrInvalidTopicOrder},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := startInput(good)
			tc.mutate(&in)
			_, err := prepareStart(in, func() time.Time { return testStart })
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("want %v, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestPrepareStart_Normalization(t *testing.T) {
	// 空场景回落 practice（冻结 start_session 默认值）；seq 升序规整；创建形态
	// 与冻结 INSERT 同构（status/进度/计数/wrong_marks/时长阈值定型）.
	in := startInput([]TopicEntry{
		{Seq: 2, ItemVersionID: "iv-2"},
		{Seq: 1, ItemVersionID: "iv-1", PlacementToken: ptr("tok-1")},
	})
	in.Scene = ""
	prepared, err := prepareStart(in, func() time.Time { return testStart })
	if err != nil {
		t.Fatalf("prepareStart: %v", err)
	}
	if prepared.params.Scene != ScenePractice {
		t.Fatalf("空场景应回落 practice，got %q", prepared.params.Scene)
	}
	if prepared.entries[0].Seq != 1 || prepared.entries[1].Seq != 2 {
		t.Fatalf("题序应按 seq 升序规整: %+v", prepared.entries)
	}
	if prepared.params.Status != "active" || prepared.params.CurrentIndex != 0 ||
		prepared.params.AnsweredCount != 0 || prepared.params.CorrectCount != 0 ||
		string(prepared.params.WrongMarks) != "[]" || prepared.params.CompletedAt.Valid {
		t.Fatalf("创建形态与冻结 start_session 不符: %+v", prepared.params)
	}
	if prepared.params.TimeLimitSec != GradebandTimeLimitSec[GradebandLow] {
		t.Fatalf("时长阈值应建会话时定型: %d", prepared.params.TimeLimitSec)
	}
	if !prepared.params.StartedAt.Valid || !prepared.params.StartedAt.Time.Equal(testStart) {
		t.Fatalf("started_at 应取 StartedAt: %+v", prepared.params.StartedAt)
	}
	if string(prepared.params.ItemSequence) != `[{"item_version_id":"iv-1","placement_token":"tok-1","item_number":1},{"item_version_id":"iv-2","placement_token":null,"item_number":2}]` {
		t.Fatalf("题序账面键名应与冻结 sequence dict 逐字一致: %s", prepared.params.ItemSequence)
	}
}

func ptr(s string) *string { return &s }

// ── MemoryStore：幂等 / 冲突 / 稳定读 ────────────────────────────────────────

func TestMemoryStore_Create_IdempotentOrConflict(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()

	first, err := s.Create(ctx, nil, startInput(seqEntries("iv-1", "iv-2")))
	if err != nil {
		t.Fatalf("首次固化: %v", err)
	}

	// 同 session 重复生成 + 输入顺序不同 + JSON 无关的语义相等 → 幂等成功.
	replayed, err := s.Create(ctx, nil, startInput([]TopicEntry{
		{Seq: 2, ItemVersionID: "iv-2"},
		{Seq: 1, ItemVersionID: "iv-1", PlacementToken: nil},
	}))
	if err != nil {
		t.Fatalf("同题序重放应幂等成功: %v", err)
	}
	if !equalEntries(replayed.Entries, first.Entries) {
		t.Fatalf("幂等成功应返回存量题序: %+v vs %+v", replayed.Entries, first.Entries)
	}

	// 同 session 不同题序 → 明确错误（绝不静默改写）.
	_, err = s.Create(ctx, nil, startInput(seqEntries("iv-1", "iv-9")))
	if !errors.Is(err, ErrTopicOrderConflict) {
		t.Fatalf("不同题序应 ErrTopicOrderConflict，got %v", err)
	}

	// 幂等重放后账面仍只有最初那份.
	stored, err := s.Read(ctx, nil, testOrderSession)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if !equalEntries(stored.Entries, first.Entries) {
		t.Fatalf("账面被冲突重放污染: %+v", stored.Entries)
	}
}

func TestMemoryStore_Read_SortedAndNotFound(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()

	if _, err := s.Read(ctx, nil, testOrderSession); !errors.Is(err, ErrSessionNotFound) {
		t.Fatalf("未固化会话应 ErrSessionNotFound，got %v", err)
	}

	in := startInput([]TopicEntry{
		{Seq: 3, ItemVersionID: "iv-3"},
		{Seq: 1, ItemVersionID: "iv-1"},
		{Seq: 2, ItemVersionID: "iv-2"},
	})
	if _, err := s.Create(ctx, nil, in); err != nil {
		t.Fatalf("Create: %v", err)
	}
	got, err := s.Read(ctx, nil, testOrderSession)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	for i, e := range got.Entries {
		if e.Seq != i+1 {
			t.Fatalf("读取面应按 seq 升序稳定读: pos=%d seq=%d", i, e.Seq)
		}
	}

	// 返回即不可变：改写出参不得回写内部账.
	got.Entries[0].ItemVersionID = "tampered"
	again, err := s.Read(ctx, nil, testOrderSession)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if again.Entries[0].ItemVersionID == "tampered" {
		t.Fatal("出参改写回写了内部账（返回应深拷贝）")
	}
}

// ── fakeTx：最小 pgx.Tx 替身（core/events、core/scoring 同惯例）──────────────

type capturedStmt struct {
	sql  string
	args []any
}

type fakeTx struct {
	mu       sync.Mutex
	stmts    []capturedStmt
	failExec error    // 非空：下一次 Exec 返回该错误（23505 等驱动错误替身）
	row      *fakeRow // QueryRow 的脚本化返回
	rowCalls int
}

var _ Executor = (*fakeTx)(nil)

func (f *fakeTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]any, len(args))
	copy(cp, args)
	f.stmts = append(f.stmts, capturedStmt{sql: sql, args: cp})
	if f.failExec != nil {
		err := f.failExec
		f.failExec = nil
		return pgconn.CommandTag{}, err
	}
	return pgconn.CommandTag{}, nil
}

func (f *fakeTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("session fake: 本套件只走 Exec/QueryRow")
}

func (f *fakeTx) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rowCalls++
	if f.row == nil {
		panic("session fake: QueryRow 未脚本化")
	}
	return f.row
}

type fakeRow struct {
	scan func(dest ...any) error
}

func (r *fakeRow) Scan(dest ...any) error { return r.scan(dest...) }

func uniqueViolationErr(constraint string) error {
	return &pgconn.PgError{
		Code:           sqlStateUniqueViolation,
		Message:        "duplicate key value violates unique constraint " + constraint,
		ConstraintName: constraint,
	}
}

// ── PGStore：语句面捕获 + 23505 幂等判读 + fail-closed ──────────────────────

func TestPGStore_Create_StatementSurfaceAndIdempotency(t *testing.T) {
	ctx := context.Background()
	entries := seqEntries("iv-1", "iv-2")
	storedJSON := []byte(`[{"item_version_id":"iv-1","placement_token":null,"item_number":1},{"item_version_id":"iv-2","placement_token":null,"item_number":2}]`)

	t.Run("首次固化只发 INSERT", func(t *testing.T) {
		f := &fakeTx{}
		if _, err := NewPGStore().Create(ctx, f, startInput(entries)); err != nil {
			t.Fatalf("Create: %v", err)
		}
		if len(f.stmts) != 1 {
			t.Fatalf("应恰好一条语句: %d", len(f.stmts))
		}
		assertNoRewriteStatement(t, f.stmts)
		if !strings.Contains(strings.ToUpper(f.stmts[0].sql), "INSERT INTO PRACTICE_SESSION") {
			t.Fatalf("语句应为题序固化 INSERT: %s", f.stmts[0].sql)
		}
	})

	t.Run("撞PK后同题序幂等成功", func(t *testing.T) {
		f := &fakeTx{failExec: uniqueViolationErr("pk_practice_session")}
		f.row = &fakeRow{scan: func(dest ...any) error {
			*(dest[0].(*[]byte)) = storedJSON
			return nil
		}}
		got, err := NewPGStore().Create(ctx, f, startInput(entries))
		if err != nil {
			t.Fatalf("同题序重放应幂等成功: %v", err)
		}
		if got.SessionID != testOrderSession || !equalEntries(got.Entries, entries) {
			t.Fatalf("幂等返回应为存量题序: %+v", got)
		}
		if f.rowCalls != 1 {
			t.Fatalf("撞 PK 后应恰好读一次存量: %d", f.rowCalls)
		}
		assertNoRewriteStatement(t, f.stmts)
	})

	t.Run("撞PK后不同题序明确冲突", func(t *testing.T) {
		f := &fakeTx{failExec: uniqueViolationErr("pk_practice_session")}
		f.row = &fakeRow{scan: func(dest ...any) error {
			*(dest[0].(*[]byte)) = []byte(`[{"item_version_id":"iv-1","placement_token":null,"item_number":1}]`)
			return nil
		}}
		_, err := NewPGStore().Create(ctx, f, startInput(entries))
		if !errors.Is(err, ErrTopicOrderConflict) {
			t.Fatalf("不同题序应 ErrTopicOrderConflict，got %v", err)
		}
		assertNoRewriteStatement(t, f.stmts)
	})

	t.Run("冲突对象不可读按题序冲突处理", func(t *testing.T) {
		f := &fakeTx{failExec: uniqueViolationErr("uq_session_topic_order_seq")}
		f.row = &fakeRow{scan: func(dest ...any) error { return pgx.ErrNoRows }}
		_, err := NewPGStore().Create(ctx, f, startInput(entries))
		if !errors.Is(err, ErrTopicOrderConflict) {
			t.Fatalf("非会话行唯一冲突应映射 ErrTopicOrderConflict，got %v", err)
		}
	})

	t.Run("非唯一冲突原样上抛", func(t *testing.T) {
		boom := errors.New("connection refused")
		f := &fakeTx{failExec: boom}
		_, err := NewPGStore().Create(ctx, f, startInput(entries))
		if !errors.Is(err, boom) {
			t.Fatalf("真故障不得吞：want wrap %v，got %v", boom, err)
		}
	})

	t.Run("非法题序零语句", func(t *testing.T) {
		f := &fakeTx{}
		in := startInput([]TopicEntry{
			{Seq: 1, ItemVersionID: "iv-1"},
			{Seq: 1, ItemVersionID: "iv-2"}, // seq 重复
		})
		if _, err := NewPGStore().Create(ctx, f, in); !errors.Is(err, ErrInvalidTopicOrder) {
			t.Fatalf("seq 重复应 ErrInvalidTopicOrder（seq 唯一冲突映射），got %v", err)
		}
		if len(f.stmts) != 0 {
			t.Fatalf("前置拒绝不得发语句: %+v", f.stmts)
		}
	})
}

func TestPGStore_Read_SortedAndNotFound(t *testing.T) {
	ctx := context.Background()

	// 账面乱序（防御性构造）→ 读取面仍按 seq 升序稳定读.
	f := &fakeTx{row: &fakeRow{scan: func(dest ...any) error {
		*(dest[0].(*[]byte)) = []byte(`[{"item_version_id":"iv-2","placement_token":null,"item_number":2},{"item_version_id":"iv-1","placement_token":null,"item_number":1}]`)
		return nil
	}}}
	got, err := NewPGStore().Read(ctx, f, testOrderSession)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(got.Entries) != 2 || got.Entries[0].Seq != 1 || got.Entries[1].Seq != 2 {
		t.Fatalf("读取应按 seq 升序: %+v", got.Entries)
	}
	assertNoRewriteStatement(t, f.stmts)

	missing := &fakeTx{row: &fakeRow{scan: func(dest ...any) error { return pgx.ErrNoRows }}}
	if _, err := NewPGStore().Read(ctx, missing, testOrderSession); !errors.Is(err, ErrSessionNotFound) {
		t.Fatalf("会话不存在应 ErrSessionNotFound，got %v", err)
	}

	// 非法 UUID 与内存实现同一条哨兵（实现间无漂移面）.
	if _, err := NewPGStore().Read(ctx, &fakeTx{}, "not-a-uuid"); !errors.Is(err, ErrSessionNotFound) {
		t.Fatalf("非法 id 应 ErrSessionNotFound，got %v", err)
	}
}

func TestPGStore_NilExecutorFailClosed(t *testing.T) {
	s := NewPGStore()
	if _, err := s.Create(context.Background(), nil, startInput(seqEntries("iv-1"))); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("无事务面 Create 应 ErrNoTransaction，got %v", err)
	}
	if _, err := s.Read(context.Background(), nil, testOrderSession); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("无事务面 Read 应 ErrNoTransaction，got %v", err)
	}
}

func assertNoRewriteStatement(t *testing.T, stmts []capturedStmt) {
	t.Helper()
	for _, s := range stmts {
		up := strings.ToUpper(s.sql)
		if strings.Contains(up, "UPDATE ") || strings.Contains(up, "DELETE ") {
			t.Fatalf("题序语句面出现改写语句（UPDATE/DELETE 无查询面可写）: %s", s.sql)
		}
	}
}

// ── 并发契约：同 session 并发重复生成（-race 下互斥串行化）───────────────────

func TestMemoryStore_Create_ConcurrentSameSession(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	in := startInput(seqEntries("iv-1", "iv-2", "iv-3"))

	const n = 12
	var wg sync.WaitGroup
	errs := make([]error, n)
	var mu sync.Mutex
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(k int) {
			defer wg.Done()
			_, err := s.Create(ctx, nil, in)
			mu.Lock()
			errs[k] = err
			mu.Unlock()
		}(i)
	}
	wg.Wait()
	for k, err := range errs {
		if err != nil {
			t.Fatalf("并发同题序重放应全部幂等成功: goroutine %d: %v", k, err)
		}
	}
	stored, err := s.Read(ctx, nil, testOrderSession)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if len(stored.Entries) != 3 {
		t.Fatalf("账面应恰一份题序: %+v", stored.Entries)
	}

	// 并发冲击后仍拒绝不同题序（不产生第二条款序）.
	if _, err := s.Create(ctx, nil, startInput(seqEntries("iv-1", "iv-2", "iv-X"))); !errors.Is(err, ErrTopicOrderConflict) {
		t.Fatalf("并发后不同题序仍应冲突，got %v", err)
	}
}

// ── 静态守卫：题序行 UPDATE/DELETE 无查询面可写 + D11 无事务终结 ─────────────

// TestTopicOrderNoRewriteSurface 是「题序不可变」在 Go 侧的结构等价物：
//  1. TopicOrderStore 方法集恰为 {Create, Read}——契约层不存在任何改写/删除面；
//  2. 本包产品源码（非测试）零 .Commit( / .Rollback(（D11：事务归最外层调用方）；
//  3. 本包产品源码 + db/queries/practice_session.sql + db/gen 生成物中不出现
//     "UPDATE practice_session" / "DELETE FROM practice_session" 语句文本；
//  4. db/gen/practice_session.sql.go 不生成任何 Update*/Delete* 方法。
//
// 守卫自己防「门空转」：包源码/查询文件/生成文件任一扫不到即 Fatal（GO-1 教训）.
func TestTopicOrderNoRewriteSurface(t *testing.T) {
	if got := reflect.TypeOf((*TopicOrderStore)(nil)).Elem().NumMethod(); got != 2 {
		t.Fatalf("TopicOrderStore 应恰有 Create/Read 两方法（无改写面），实际 %d", got)
	}

	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位测试源文件目录")
	}
	pkgDir := filepath.Dir(thisFile)

	entries, err := os.ReadDir(pkgDir)
	if err != nil {
		t.Fatalf("读取包目录失败: %v", err)
	}
	scanned := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		path := filepath.Join(pkgDir, name)
		body, rerr := os.ReadFile(path)
		if rerr != nil {
			t.Fatalf("读 %s: %v", name, rerr)
		}
		text := string(body)
		for _, forbidden := range []string{"UPDATE practice_session", "DELETE FROM practice_session"} {
			if strings.Contains(text, forbidden) {
				t.Errorf("%s: 出现 %q 语句文本（题序不可变：改写无查询面可写）", name, forbidden)
			}
		}
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			t.Fatalf("解析 %s 失败: %v", name, perr)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, isCall := n.(*ast.CallExpr)
			if !isCall {
				return true
			}
			sel, isSel := call.Fun.(*ast.SelectorExpr)
			if !isSel {
				return true
			}
			switch sel.Sel.Name {
			case "Commit", "Rollback":
				pos := fset.Position(call.Pos())
				t.Errorf("%s:%d: 领域源码出现 .%s( 调用（D11：本域禁止 Commit/Rollback）",
					name, pos.Line, sel.Sel.Name)
			}
			return true
		})
		scanned++
	}
	if scanned == 0 {
		t.Fatal("未扫描到任何包源码文件——守卫失效")
	}

	queriesPath := filepath.Join(pkgDir, "..", "..", "db", "queries", "practice_session.sql")
	qbody, err := os.ReadFile(queriesPath)
	if err != nil {
		t.Fatalf("读取查询面失败: %v", err)
	}
	qText := strings.ToUpper(string(qbody))
	for _, forbidden := range []string{"UPDATE ", "DELETE "} {
		if strings.Contains(qText, forbidden) {
			t.Errorf("db/queries/practice_session.sql 出现 %q（查询面只有 INSERT/SELECT）", forbidden)
		}
	}

	genPath := filepath.Join(pkgDir, "..", "..", "db", "gen", "practice_session.sql.go")
	gbody, err := os.ReadFile(genPath)
	if err != nil {
		t.Fatalf("读取生成层失败: %v", err)
	}
	gText := strings.ToUpper(string(gbody))
	for _, forbidden := range []string{"UPDATE PRACTICE_SESSION", "DELETE FROM PRACTICE_SESSION"} {
		if strings.Contains(gText, forbidden) {
			t.Errorf("db/gen/practice_session.sql.go 出现 %q（生成物与查询面同源）", forbidden)
		}
	}
	gfset := token.NewFileSet()
	gfile, perr := parser.ParseFile(gfset, genPath, nil, 0)
	if perr != nil {
		t.Fatalf("解析生成层失败: %v", perr)
	}
	genMethods := 0
	ast.Inspect(gfile, func(n ast.Node) bool {
		fn, isFn := n.(*ast.FuncDecl)
		if !isFn || fn.Recv == nil {
			return true
		}
		genMethods++
		if strings.HasPrefix(fn.Name.Name, "Update") || strings.HasPrefix(fn.Name.Name, "Delete") {
			t.Errorf("db/gen 生成 %s：会话题序不得有改写方法（sqlc 查询面只有 INSERT/SELECT）", fn.Name.Name)
		}
		return true
	})
	if genMethods == 0 {
		t.Fatal("生成层未扫描到任何方法——守卫失效")
	}
}
