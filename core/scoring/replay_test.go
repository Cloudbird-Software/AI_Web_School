package scoring

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/events"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// 本套件承载 T-W5-016 验收 #4 与回滚一致性（照 core/events 惯例，无 Docker/PG，
// PG 运行时行为不在此宣称覆盖——真库留给 CI）：
// - 可回放断言：同输入同 scorer 版本 → 同输出同 trace（逐字节）；
// - 模型版本切换重判：同题同作答在新模型版本下产生新 trace，历史 trace 不可变
//   且旧版本可定位（D6/D10：重判写平行账，原轨迹永不变）；
// - 回滚一致性：trace 随 response_event 在同一外层事务落账，任一步失败 →
//   显式 Rollback → 落账计数不变（评分与事件同进同退，D11）。

// TestSameInputSameVersionSameOutputAndTrace 是可回放的判定式断言：固定时钟下
// 同输入同版本的两次评分，结果与 trace 逐字节一致——重放同一作答必须得到
// 完全相同的账面，任何抖动（键序、摘要、耗时口径）都在此变红.
func TestSameInputSameVersionSameOutputAndTrace(t *testing.T) {
	in := RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{"q1": "B"}}}
	run1 := mustRun(t, mustRunner(t, newTable(t, exactStub()), fixedClock(t)), in)
	run2 := mustRun(t, mustRunner(t, newTable(t, exactStub()), fixedClock(t)), in)

	if run1.Result != run2.Result {
		t.Fatalf("同输入同版本结果必须相同: %+v vs %+v", run1.Result, run2.Result)
	}
	if traceJSON(t, run1.Trace) != traceJSON(t, run2.Trace) {
		t.Fatalf("同输入同版本 trace 必须逐字节一致:\n%s\n%s", traceJSON(t, run1.Trace), traceJSON(t, run2.Trace))
	}
}

// TestModelVersionSwitchRerunKeepsHistoryLocatable 是验收 #4 的主用例：
// 同一作答在模型版本升级后重判 → 产生携带新 model_version 的新 trace；
// 重判前的历史 trace 保持原样（map 为出库快照，Runner 不回写）且旧版本
// 可定位——平行 score_run 语义的 Go 面（R-D-05：原始事件序列永不变更）.
func TestModelVersionSwitchRerunKeepsHistoryLocatable(t *testing.T) {
	ai := aiStub()
	r := mustRunner(t, newTable(t, ai), fixedClock(t))
	in := RunInput{ScorerID: "ai_rubric", Answer: "同一篇作文"}

	historical := mustRun(t, r, in)
	oldVersion := historical.Trace["model_version"]

	// 模型升级：同一评分器条目（id/version 不变），背后的模型版本前移.
	ai.res.ModelVersion = "2026-08"
	rerun := mustRun(t, r, in)

	if rerun.Trace["model_version"] != "2026-08" {
		t.Fatalf("重判 trace 应携带新模型版本: %#v", rerun.Trace["model_version"])
	}
	if historical.Trace["model_version"] != oldVersion || oldVersion != "2026-06" {
		t.Fatalf("历史 trace 不可变且旧版本可定位: %#v (want 2026-06)", historical.Trace["model_version"])
	}
	if traceJSON(t, historical.Trace) == traceJSON(t, rerun.Trace) {
		t.Fatal("模型版本切换后的重判 trace 必须是新账（与历史 trace 可区分）")
	}
	if historical.Trace["input_digest"] != rerun.Trace["input_digest"] {
		t.Fatal("同作答重判的输入摘要必须相同（重放定位的前提）")
	}
}

// ── fakeTx：最小状态机模拟「最外层调用方持有的 pgx.Tx」（core/events 同惯例）──
// Exec 落 pending（未决），Commit 才并入 applied，Rollback 丢弃 pending——
// 未提交不可见与真实 DB 同构.

type capturedStmt struct {
	sql  string
	args []any
}

type fakeTx struct {
	mu         sync.Mutex
	pending    []capturedStmt
	applied    []capturedStmt
	failNext   bool
	done       bool
	rolledBack bool
}

var _ events.Executor = (*fakeTx)(nil)

func (f *fakeTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return pgconn.CommandTag{}, errors.New("fake: 事务已终结")
	}
	if f.failNext {
		f.failNext = false
		return pgconn.CommandTag{}, errors.New("fake: 下游步骤失败（会话状态更新替身）")
	}
	cp := make([]any, len(args))
	copy(cp, args)
	f.pending = append(f.pending, capturedStmt{sql: sql, args: cp})
	return pgconn.CommandTag{}, nil
}

func (f *fakeTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("scoring fake: 本套件只走 Exec 写路径")
}

func (f *fakeTx) QueryRow(context.Context, string, ...any) pgx.Row {
	panic("scoring fake: 本套件只走 Exec 写路径")
}

func (f *fakeTx) commit() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errors.New("fake: 事务已终结")
	}
	f.done = true
	f.applied = append(f.applied, f.pending...)
	f.pending = nil
	return nil
}

func (f *fakeTx) rollback() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errors.New("fake: 事务已终结")
	}
	f.done, f.rolledBack = true, true
	f.pending = nil
	return nil
}

// countAppliedEvent 统计已入账的 response_event INSERT 条数.
func (f *fakeTx) countAppliedEvent(t *testing.T) int {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, s := range f.applied {
		if strings.Contains(s.sql, "INSERT INTO response_event") {
			n++
		}
	}
	return n
}

// lastAppliedEvent 取最后一条已入账事件语句（落账内容比对用）.
func (f *fakeTx) lastAppliedEvent(t *testing.T) capturedStmt {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := len(f.applied) - 1; i >= 0; i-- {
		if strings.Contains(f.applied[i].sql, "INSERT INTO response_event") {
			return f.applied[i]
		}
	}
	t.Fatal("applied 账无事件语句")
	return capturedStmt{}
}

// eventInput 把一次评分产出装配为 response_event 入账输入（作答事件与 trace
// 同账的装配面；uuid/时刻固定保证断言确定性）.
func eventInput(t *testing.T, run *Run) events.Input {
	t.Helper()
	return events.Input{
		EventID:        "a1111111-2222-4333-8444-555555555555",
		StudentAliasID: "c3333333-4444-4555-8666-777777777777",
		ItemVersionID:  "sha256:item-v1",
		Scene:          events.ScenePractice,
		RawPayload:     map[string]any{"answer": "B"},
		ScoringTrace:   run.Trace,
		CreatedAt:      mustTime(t, fixedNow),
	}
}

func mustTime(t *testing.T, s string) time.Time {
	t.Helper()
	ts, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatal(err)
	}
	return ts
}

// TestScoringTraceRollsBackWithOuterTransaction：trace 经 events.Writer 在调用方
// 显式事务内落账，评分之后的下游步骤（会话状态更新）失败 → 最外层 Rollback →
// response_event 计数不变——评分与事件同进同退，不存在「trace 已落、业务回滚」
// 的账实分裂（D11；照 core/events 的 fakeTx 惯例）.
func TestScoringTraceRollsBackWithOuterTransaction(t *testing.T) {
	run := mustRun(t, mustRunner(t, newTable(t, exactStub()), fixedClock(t)),
		RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{"q1": "B"}}})

	f := &fakeTx{}
	w := events.WithTx(f)
	ctx := context.Background()
	if _, err := w.Record(ctx, eventInput(t, run)); err != nil {
		t.Fatalf("事件入账意外失败: %v", err)
	}
	if len(f.pending) != 1 {
		t.Fatalf("事件应恰好发出一条未决语句: pending=%d", len(f.pending))
	}

	// 评分之后的下游步骤失败（会话状态更新替身），最外层调用方统一回滚.
	f.failNext = true
	if _, err := f.Exec(ctx, "UPDATE practice_session SET current_index = current_index + 1 WHERE session_id = $1", "sess-1"); err == nil {
		t.Fatal("注入的失败步必须真的失败")
	}
	if err := f.rollback(); err != nil {
		t.Fatal(err)
	}
	if got := f.countAppliedEvent(t); got != 0 {
		t.Fatalf("回滚后 response_event 计数应不变（applied=%d）", got)
	}
	if len(f.pending) != 0 || !f.rolledBack {
		t.Fatalf("回滚态不干净: pending=%d rolledBack=%v", len(f.pending), f.rolledBack)
	}
}

// TestScoringTracePersistsWithCommit 是对照支：无失败 → 最外层 Commit 后事件
// 连同可回放 trace 完整入账；落账 JSONB 里 model/prompt 版本与判定依据键
// 原样可读（十年后审账直读面）.
func TestScoringTracePersistsWithCommit(t *testing.T) {
	run := mustRun(t, mustRunner(t, newTable(t, aiStub()), fixedClock(t)),
		RunInput{ScorerID: "ai_rubric", Answer: "作文"})

	f := &fakeTx{}
	w := events.WithTx(f)
	if _, err := w.Record(context.Background(), eventInput(t, run)); err != nil {
		t.Fatalf("事件入账意外失败: %v", err)
	}
	if err := f.commit(); err != nil {
		t.Fatal(err)
	}
	if got := f.countAppliedEvent(t); got != 1 {
		t.Fatalf("Commit 后 response_event 计数应为 1，实际 %d", got)
	}

	// scoring_trace 是第 7 个位置参数（$7，契约 §1 列序）；校验落账原文.
	traceArg, ok := f.lastAppliedEvent(t).args[6].([]byte)
	if !ok {
		t.Fatalf("arg[6] 应为 scoring_trace 字节: %#v", f.lastAppliedEvent(t).args[6])
	}
	var landed map[string]any
	if err := json.Unmarshal(traceArg, &landed); err != nil {
		t.Fatal(err)
	}
	if landed["model_version"] != "2026-06" || landed["prompt_version"] != "v3" {
		t.Fatalf("落账 trace 缺 D10 回放要素: %s", traceArg)
	}
	proc, ok := landed["process"].(map[string]any)
	if !ok || proc["correct"] != true {
		t.Fatalf("落账 trace 缺判定依据键 process.correct: %s", traceArg)
	}
}
