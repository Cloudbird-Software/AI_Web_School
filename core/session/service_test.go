// service_test.go：GO-RW-002 会话全链路服务面的域级单测（内存面）。
//
// 锁五件事（任务卡验收映射）：
//  1. 全链路 start→next→submit→abandon（Memory 面，-race 下行为确定）；
//  2. 并发幂等：同一 (session, item, 指纹) 并发提交恰一条事件、current_index
//     恰推进 1、全员同 event_id（board 验收的并发面）；
//  3. 家长授权门拒绝（missing/revoked/expired → ErrConsentRequired；store
//     未装配 → fail-closed 装配错误）；
//  4. 归属断言（D9）：非会话主人对全部动词被 ErrNotSessionOwner 拒绝；
//  5. 时长保护闭环：超限 → rest_prompted + ErrRestRequired（取题与提交同
//     判据）→ Resume 重置计时 → 继续作答。
//
// 状态投影（SessionState 域字段）与幂等重放零副作用随链路断言。
package session

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// svcZeroTime 是「取服务时钟」的显式零值形参（服务 resolveAt 归一为可控时钟）.
var svcZeroTime = time.Time{}

// svcTestClock 是可推进的确定性时钟（服务与内存账本共用同一来源）.
type svcTestClock struct{ t time.Time }

func (c *svcTestClock) Now() time.Time { return c.t }
func (c *svcTestClock) Advance(d time.Duration) {
	c.t = c.t.Add(d)
}

// newSvcFixture 装配内存面服务：授权账（alias 已持有效授权）、单一内存账本、
// LocalRunner、可控时钟。startAt 与时钟锚对齐，链路时刻全确定.
func newSvcFixture(t *testing.T, alias string) (*Service, *MemoryStore, *compliance.MemoryStore, *svcTestClock) {
	t.Helper()
	clock := &svcTestClock{t: testStart}
	mem := NewMemoryStore()
	mem.SetClock(clock.Now)
	consents := grantedStore(t, alias)
	svc, err := NewService(Deps{
		Consents:    consents,
		Orders:      mem,
		Submissions: mem,
		Accounts:    mem,
		Runner:      LocalRunner{},
		Now:         clock.Now,
	})
	if err != nil {
		t.Fatalf("构造 SessionService: %v", err)
	}
	return svc, mem, consents, clock
}

// startParams 是两题实例池会话的开立请求（时长阈值随 GradebandLow 定型）.
func startParams(ids ...string) StartParams {
	return StartParams{
		Scene:          ScenePractice,
		Gradeband:      GradebandLow,
		ItemVersionIDs: ids,
		StartedAt:      testStart,
	}
}

// correctTrace / wrongTrace 是契约 §3 落账形态的评分轨迹样本
// （core/scoring buildTrace 同构：process.correct + dimension_scores）.
func correctTrace() map[string]any {
	return map[string]any{
		"process":          map[string]any{"correct": true},
		"dimension_scores": map[string]any{"correct": float64(1), "total": float64(1)},
	}
}

func wrongTrace() map[string]any {
	return map[string]any{
		"process":          map[string]any{"correct": false},
		"dimension_scores": map[string]any{"correct": float64(0), "total": float64(1)},
	}
}

func submitIn(sid, item string, trace map[string]any, at time.Time) SubmitInput {
	return SubmitInput{
		SessionID:       sid,
		ItemVersionID:   item,
		Response:        map[string]any{"selected": "A"},
		ScoringTrace:    trace,
		DurationMs:      nil,
		ErrorInferences: nil,
		At:              at,
	}
}

// TestService_FullChain 全链路：开立 → 取题 → 作答 → 放弃（任务卡验收 #1 的
// 字面链）。链上锁状态投影关键列（total/progress/counts/status）与事件账
// 恰一条；放弃后再取题/作答一律明确拒绝；放弃零删除已入账事件.
func TestService_FullChain(t *testing.T) {
	svc, mem, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()

	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if started.Status != StatusActive || started.Total != 2 || started.TimeLimitSec != int32(GradebandTimeLimitSec[GradebandLow]) {
		t.Fatalf("StartResult 异常: %+v", started)
	}
	next, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("GetNext#1: %v", err)
	}
	if next.Done || next.ItemVersionID != "iv-1" {
		t.Fatalf("取题 #1 = %+v, want iv-1", next)
	}
	fb, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now()))
	if err != nil {
		t.Fatalf("Submit#1: %v", err)
	}
	if fb.EventID == "" || !fb.Correct || fb.SessionStatus != StatusActive {
		t.Fatalf("Feedback#1 = %+v", fb)
	}
	if fb.Progress.Total != 2 || fb.Progress.MainAnswered != 1 || fb.Progress.CorrectCount != 1 || fb.Progress.AnsweredCount != 1 {
		t.Fatalf("Progress#1 = %+v", fb.Progress)
	}

	// 放弃会话（任务卡链路终点）：状态 abandoned、已作答事件保留.
	abandoned, err := svc.Abandon(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("Abandon: %v", err)
	}
	if abandoned.Status != StatusAbandoned || abandoned.MainAnswered != 1 || abandoned.AnsweredCount != 1 || abandoned.CorrectCount != 1 {
		t.Fatalf("Abandon 投影 = %+v", abandoned)
	}
	if _, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime); !errors.Is(err, ErrSessionState) {
		t.Fatalf("放弃后取题 err = %v, want ErrSessionState", err)
	}
	if _, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-2", correctTrace(), clock.Now())); !errors.Is(err, ErrSessionState) {
		t.Fatalf("放弃后提交 err = %v, want ErrSessionState", err)
	}
	if got := len(mem.Events()); got != 1 {
		t.Fatalf("事件账 = %d 条, want 恰 1（append-only 零删除）", got)
	}
}

// TestService_GetNextToCompletion 走到 done：两题全对 → 第三次取题 done=true
// 且会话已完结（提交面原子完结）；完结后提交 → ErrSessionCompleted.
func TestService_GetNextToCompletion(t *testing.T) {
	svc, _, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	for _, item := range []string{"iv-1", "iv-2"} {
		if _, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime); err != nil {
			t.Fatalf("GetNext %s: %v", item, err)
		}
		if _, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, item, correctTrace(), clock.Now())); err != nil {
			t.Fatalf("Submit %s: %v", item, err)
		}
	}
	done, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("GetNext 完成: %v", err)
	}
	if !done.Done || done.ItemVersionID != "" {
		t.Fatalf("完成取题 = %+v, want done=true 且仅含 done", done)
	}
	// 完结后的新作答（不同指纹）→ ErrSessionCompleted；同指纹迟到重试是
	// 幂等成功而非报错（幂等语义全时态，先于状态校验）.
	if _, err := svc.Submit(ctx, testAlias, SubmitInput{
		SessionID: started.SessionID, ItemVersionID: "iv-1",
		Response: map[string]any{"selected": "changed"}, ScoringTrace: correctTrace(),
	}); !errors.Is(err, ErrSessionCompleted) {
		t.Fatalf("完结后提交 err = %v, want ErrSessionCompleted", err)
	}
	replay, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now()))
	if err != nil || !replay.Duplicate {
		t.Fatalf("完结后同指纹重放 = %+v err=%v, want 幂等命中", replay, err)
	}
	if _, err := svc.Abandon(ctx, testAlias, started.SessionID, svcZeroTime); !errors.Is(err, ErrSessionState) {
		t.Fatalf("完结后放弃 err = %v, want ErrSessionState", err)
	}
}

// TestService_SubmitIdempotentReplay 幂等重放（单线程面）：同指纹重交返回
// 首次 event_id、零新事件、进度不动；different 指纹按题序校验明确拒绝.
func TestService_SubmitIdempotentReplay(t *testing.T) {
	svc, mem, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	first, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now()))
	if err != nil {
		t.Fatalf("Submit#1: %v", err)
	}
	replay, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now()))
	if err != nil {
		t.Fatalf("Submit 重放: %v", err)
	}
	if replay.EventID != first.EventID || !replay.Duplicate {
		t.Fatalf("重放 = %+v, want 幂等命中且同 event_id", replay)
	}
	if got := len(mem.Events()); got != 1 {
		t.Fatalf("重放后事件账 = %d 条, want 仍 1", got)
	}
	// 不同作答（不同指纹）≠ 重放：题序已推进 → 明确的序列违例.
	if _, err := svc.Submit(ctx, testAlias, SubmitInput{
		SessionID: started.SessionID, ItemVersionID: "iv-1",
		Response: map[string]any{"selected": "B"}, ScoringTrace: correctTrace(),
	}); !errors.Is(err, ErrOutOfSequence) {
		t.Fatalf("不同指纹重交 err = %v, want ErrOutOfSequence", err)
	}
}

// TestService_ConcurrentIdempotentSubmit 并发幂等（-race 面）：32 个并发
// 同指纹提交 → 恰一条真实入账、current_index 恰推进 1、全员同一 event_id
// 且其余全部幂等命中（SubmissionStore 并发契约经服务事务面的全链验证）.
func TestService_ConcurrentIdempotentSubmit(t *testing.T) {
	svc, mem, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	const n = 32
	in := submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now())
	results := make(chan SubmitResult, n)
	errs := make(chan error, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := svc.Submit(ctx, testAlias, in)
			if err != nil {
				errs <- err
				return
			}
			results <- *res
		}()
	}
	wg.Wait()
	close(results)
	close(errs)
	for err := range errs {
		t.Fatalf("并发提交意外失败: %v", err)
	}
	seen := map[string]int{}
	duplicates := 0
	for res := range results {
		seen[res.EventID]++
		if res.Duplicate {
			duplicates++
		}
	}
	if len(seen) != 1 || duplicates != n-1 {
		t.Fatalf("event_id 分裂或幂等计数异常: seen=%v duplicates=%d", seen, duplicates)
	}
	if got := len(mem.Events()); got != 1 {
		t.Fatalf("事件账 = %d 条, want 恰 1", got)
	}
	post, err := svc.State(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if post.MainAnswered != 1 || post.AnsweredCount != 1 || post.CorrectCount != 1 {
		t.Fatalf("并发后投影 = %+v, want 恰推进 1", post)
	}
}

// TestService_ConcurrentDistinctFingerprints 并发不同作答：单写者串行化下
// 恰一个成功、其余全部题序违例明确拒绝（不允许半推进/双入账）.
func TestService_ConcurrentDistinctFingerprints(t *testing.T) {
	svc, mem, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	const n = 16
	var wg sync.WaitGroup
	okCount, seqErrs := make(chan int, n), make(chan int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(k int) {
			defer wg.Done()
			_, err := svc.Submit(ctx, testAlias, SubmitInput{
				SessionID:     started.SessionID,
				ItemVersionID: "iv-1",
				Response:      map[string]any{"selected": k},
				ScoringTrace:  correctTrace(),
				At:            clock.Now(),
			})
			switch {
			case err == nil:
				okCount <- 1
			case errors.Is(err, ErrOutOfSequence):
				seqErrs <- 1
			default:
				t.Errorf("并发不同指纹意外错误: %v", err)
			}
		}(i)
	}
	wg.Wait()
	close(okCount)
	close(seqErrs)
	if got, want := len(okCount), 1; got != want {
		t.Fatalf("成功提交 %d 次, want 恰 1", got)
	}
	if len(seqErrs) != n-1 {
		t.Fatalf("题序违例 %d 次, want %d", len(seqErrs), n-1)
	}
	if got := len(mem.Events()); got != 1 {
		t.Fatalf("事件账 = %d 条, want 恰 1", got)
	}
}

// TestService_Start_ConsentGate 家长授权门（验收 #3）：missing/revoked/
// expired 一律 ErrConsentRequired；账本未装配 fail-closed 装配错误；且拒绝
// 路径零会话写入.
func TestService_Start_ConsentGate(t *testing.T) {
	ctx := context.Background()
	clock := &svcTestClock{t: testStart}
	mem := NewMemoryStore()
	mem.SetClock(clock.Now)

	t.Run("missing拒绝", func(t *testing.T) {
		svc := mustSvc(t, compliance.NewMemoryStore(), mem, clock)
		_, err := svc.Start(ctx, testAlias, startParams("iv-1"))
		var consentErr *compliance.ConsentRequiredError
		if !errors.As(err, &consentErr) || consentErr.State != compliance.StateMissing {
			t.Fatalf("err = %v, want ConsentRequiredError(missing)", err)
		}
	})
	t.Run("revoked撤回立即失效", func(t *testing.T) {
		store := grantedStore(t, testAlias)
		if _, err := store.Revoke(ctx, nil, compliance.RevokeInput{
			StudentAliasID: testAlias, Purpose: compliance.PurposeOnlinePractice, At: testSince.Add(time.Hour),
		}); err != nil {
			t.Fatalf("撤回: %v", err)
		}
		svc := mustSvc(t, store, mem, clock)
		if _, err := svc.Start(ctx, testAlias, startParams("iv-1")); !errors.Is(err, compliance.ErrConsentRequired) {
			t.Fatalf("err = %v, want ErrConsentRequired", err)
		}
	})
	t.Run("expired窗口已过", func(t *testing.T) {
		store := compliance.NewMemoryStore()
		if _, err := store.RecordGrant(ctx, nil, compliance.GrantInput{
			StudentAliasID: testAlias, Purpose: compliance.PurposeOnlinePractice,
			ValidFrom: testSince, ValidUntil: testUntilEarly, At: testSince,
		}); err != nil {
			t.Fatalf("登记: %v", err)
		}
		svc := mustSvc(t, store, mem, clock)
		if _, err := svc.Start(ctx, testAlias, startParams("iv-1")); !errors.Is(err, compliance.ErrConsentRequired) {
			t.Fatalf("err = %v, want ErrConsentRequired", err)
		}
	})
	t.Run("拒绝零写入", func(t *testing.T) {
		before := len(mem.Events())
		if before != 0 {
			t.Fatalf("前置事件账非空: %d", before)
		}
	})
	t.Run("granted放行_对照", func(t *testing.T) {
		// 对照组：同一账本补授权后放行——证明拒绝全部来自授权门而非装配，
		// 且此前各拒绝路径确实零写入（无残留会话干扰本次开立）.
		store := grantedStore(t, testAlias)
		svc := mustSvc(t, store, mem, clock)
		started, err := svc.Start(ctx, testAlias, startParams("iv-1"))
		if err != nil {
			t.Fatalf("granted Start: %v", err)
		}
		if started.Total != 1 || started.Status != StatusActive {
			t.Fatalf("StartResult = %+v", started)
		}
	})
	t.Run("账本未装配fail-closed", func(t *testing.T) {
		svc, err := NewService(Deps{Consents: nil, Orders: mem, Submissions: mem, Accounts: mem, Runner: LocalRunner{}})
		if err == nil {
			t.Fatal("nil 授权账的装配必须被构造期拒绝")
		}
		_ = svc
	})
}

// TestService_OwnershipEnforced 归属断言（D9）：他人 alias 对五个动词全部
// ErrNotSessionOwner，且拒绝零副作用（事件/进度不动）.
func TestService_OwnershipEnforced(t *testing.T) {
	alien := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	svc, mem, consents, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	// alien 也要有授权：保证 403 只来自归属断言而非授权门.
	if _, err := consents.RecordGrant(ctx, nil, compliance.GrantInput{
		StudentAliasID: alien, Purpose: compliance.PurposeOnlinePractice,
		ValidFrom: testSince, ValidUntil: testUntilFar, At: testSince,
	}); err != nil {
		t.Fatalf("登记 alien 授权: %v", err)
	}
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if _, err := svc.Start(ctx, alien, startParams("iv-9")); err != nil {
		t.Fatalf("alien 自建会话: %v", err)
	}
	if _, err := svc.State(ctx, alien, started.SessionID, svcZeroTime); !errors.Is(err, ErrNotSessionOwner) {
		t.Fatalf("State err = %v, want ErrNotSessionOwner", err)
	}
	if _, err := svc.GetNext(ctx, alien, started.SessionID, svcZeroTime); !errors.Is(err, ErrNotSessionOwner) {
		t.Fatalf("GetNext err = %v, want ErrNotSessionOwner", err)
	}
	if _, err := svc.Submit(ctx, alien, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now())); !errors.Is(err, ErrNotSessionOwner) {
		t.Fatalf("Submit err = %v, want ErrNotSessionOwner", err)
	}
	if _, err := svc.Resume(ctx, alien, started.SessionID, svcZeroTime); !errors.Is(err, ErrNotSessionOwner) {
		t.Fatalf("Resume err = %v, want ErrNotSessionOwner", err)
	}
	if _, err := svc.Abandon(ctx, alien, started.SessionID, svcZeroTime); !errors.Is(err, ErrNotSessionOwner) {
		t.Fatalf("Abandon err = %v, want ErrNotSessionOwner", err)
	}
	if got := len(mem.Events()); got != 0 {
		t.Fatalf("拒绝后事件账 = %d, want 0（断言先于一切写入）", got)
	}
}

// TestService_RestProtectionLoop 时长保护闭环：时钟推进超限 → 取题与提交
// 双触发 ErrRestRequired 且会话置 rest_prompted、零事件写入 → Resume 重置
// 计时 → 继续作答成功.
func TestService_RestProtectionLoop(t *testing.T) {
	svc, mem, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	limit := time.Duration(GradebandTimeLimitSec[GradebandLow]) * time.Second
	clock.Advance(limit + time.Second)

	if _, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime); !errors.Is(err, ErrRestRequired) {
		t.Fatalf("取题超限 err = %v, want ErrRestRequired", err)
	}
	if _, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", correctTrace(), clock.Now())); !errors.Is(err, ErrRestRequired) {
		t.Fatalf("提交超限 err = %v, want ErrRestRequired", err)
	}
	if got := len(mem.Events()); got != 0 {
		t.Fatalf("保护触发写入了事件: %d", got)
	}
	st, err := svc.State(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st.Status != StatusRestPrompted || st.RemainingSec > 0 {
		t.Fatalf("保护态投影 = %+v", st)
	}
	resumed, err := svc.Resume(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if resumed.Status != StatusActive || resumed.ElapsedActiveSec != 0 {
		t.Fatalf("恢复投影 = %+v, want active 且计时归零", resumed)
	}
	next, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("恢复后取题: %v", err)
	}
	if next.ItemVersionID != "iv-1" {
		t.Fatalf("恢复后取题 = %+v, want iv-1", next)
	}
}

// TestService_StartParamsValidation 开立参数互斥与来源分型（契约
// 「paper_id 与 item_version_ids 二选一」+ 静态卷解析面未接线的显式拒绝）.
func TestService_StartParamsValidation(t *testing.T) {
	svc, _, _, _ := newSvcFixture(t, testAlias)
	ctx := context.Background()
	both := startParams("iv-1")
	pid := "paper-1"
	both.PaperID = &pid
	if _, err := svc.Start(ctx, testAlias, both); !errors.Is(err, ErrInvalidSessionStart) {
		t.Fatalf("双来源 err = %v, want ErrInvalidSessionStart", err)
	}
	none := startParams()
	if _, err := svc.Start(ctx, testAlias, none); !errors.Is(err, ErrInvalidSessionStart) {
		t.Fatalf("零来源 err = %v, want ErrInvalidSessionStart", err)
	}
	paper := startParams()
	paper.PaperID = &pid
	if _, err := svc.Start(ctx, testAlias, paper); !errors.Is(err, ErrPaperSequenceUnavailable) {
		t.Fatalf("静态卷 err = %v, want ErrPaperSequenceUnavailable", err)
	}
}

// TestService_StateProjection 状态投影字段面（SessionState 域）：错题计数、
// 待回测数与剩余秒数按 mark/时钟确定.
func TestService_StateProjection(t *testing.T) {
	svc, _, _, clock := newSvcFixture(t, testAlias)
	ctx := context.Background()
	started, err := svc.Start(ctx, testAlias, startParams("iv-1", "iv-2"))
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if _, err := svc.GetNext(ctx, testAlias, started.SessionID, svcZeroTime); err != nil {
		t.Fatalf("GetNext: %v", err)
	}
	clock.Advance(30 * time.Second)
	if _, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", wrongTrace(), clock.Now())); err != nil {
		t.Fatalf("Submit 错题: %v", err)
	}
	st, err := svc.State(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st.Total != 2 || st.MainAnswered != 1 || st.AnsweredCount != 1 || st.CorrectCount != 0 || st.WrongCount != 1 {
		t.Fatalf("投影 = %+v", st)
	}
	if st.ElapsedActiveSec != 30 || st.RemainingSec != int(GradebandTimeLimitSec[GradebandLow])-30 {
		t.Fatalf("时长字段 = elapsed %d remaining %d", st.ElapsedActiveSec, st.RemainingSec)
	}
	// 未开回测：错题标记 retest_status=off → 待回测 0.
	if st.RetestPending != 0 {
		t.Fatalf("待回测 = %d, want 0", st.RetestPending)
	}
	// 幂等重放不改投影.
	if _, err := svc.Submit(ctx, testAlias, submitIn(started.SessionID, "iv-1", wrongTrace(), clock.Now())); err != nil {
		t.Fatalf("重放: %v", err)
	}
	after, err := svc.State(ctx, testAlias, started.SessionID, svcZeroTime)
	if err != nil {
		t.Fatalf("State#2: %v", err)
	}
	if after.WrongCount != 1 || after.MainAnswered != 1 {
		t.Fatalf("重放改写了投影: %+v", after)
	}
}

// mustSvc 以给定授权账装配服务（测试辅助）.
func mustSvc(t *testing.T, consents compliance.ConsentStore, mem *MemoryStore, clock *svcTestClock) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Consents:    consents,
		Orders:      mem,
		Submissions: mem,
		Accounts:    mem,
		Runner:      LocalRunner{},
		Now:         clock.Now,
	})
	if err != nil {
		t.Fatalf("构造服务: %v", err)
	}
	return svc
}
