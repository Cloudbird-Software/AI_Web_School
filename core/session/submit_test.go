// submit_test.go：T-W5-018 作答提交幂等与并发安全的域级测试（Memory 实现，
// 无 Docker/PG——PG 运行时行为不在此宣称覆盖，真库并发语义留给 CI）。
//
// 锁定的契约面：
//   - 幂等：同一 (session, item, 作答指纹) 重复提交返回首次 event_id、零副作用
//     （不重复落账、不重复推进）；指纹规范化——键序/数值表示差异不产生新指纹；
//   - 并发：N 并发同指纹 → 恰一条入账、其余 duplicate=true 且事件 id 一致；
//     N 并发异指纹同题 → 恰一条入账、其余明确失败（ErrOutOfSequence），
//     current_index 恰推进 1（board 验收原文）；
//   - 纪律：题序/状态/时长保护拒绝路径零写入；交出副本（-race 干净前提）。
package session

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/events"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/sync/errgroup"
)

// 测试锚：固定会话/学生身份与确定性时钟（事件 created_at 与时长保护判定确定）.
var (
	subSession = "22222222-3333-4444-8555-666666666666"
	subAlias   = "11111111-2222-4333-8444-555555555555"
	subBase    = time.Unix(1_750_000_000, 0).UTC()
)

// newSubmitStore 返回注入固定时钟的空内存存储.
func newSubmitStore(t *testing.T) *MemoryStore {
	t.Helper()
	s := NewMemoryStore()
	s.SetClock(func() time.Time { return subBase })
	return s
}

// seedDefault 开立双题主序列的默认会话（时长阈值 30 分钟，计时起点=锚时刻）.
func seedDefault(t *testing.T, s *MemoryStore) {
	t.Helper()
	if err := s.SeedSession(SeedInput{
		SessionID:      subSession,
		StudentAliasID: subAlias,
		Scene:          "practice",
		Sequence:       []string{"item-aaa", "item-bbb"},
		TimeLimitSec:   1800,
		LastResumeAt:   subBase,
	}); err != nil {
		t.Fatalf("开立会话: %v", err)
	}
}

// submit 是 SubmitAnswer 的测试便捷形态（内存实现忽略事务执行面，传 nil）.
func submit(t *testing.T, s *MemoryStore, in SubmitInput) (string, bool, error) {
	t.Helper()
	return s.SubmitAnswer(context.Background(), nil, in)
}

// answer 构造单选作答载荷（键序随构造序变化，指纹必须对其不敏感）.
func answer(choice string) map[string]any {
	return map[string]any{"kind": "single_choice", "choice": choice}
}

// TestSubmitFirstThenIdempotentReplay 幂等主用例：首次真实入账；重放同内容
// 作答（键序重排 + 数值表示差异 + 不同耗时）必须返回同一事件 id 且零副作用
// ——时长/评分轨迹不入指纹，网络重试的合法差异不得误判为新作答.
func TestSubmitFirstThenIdempotentReplay(t *testing.T) {
	s := newSubmitStore(t)
	seedDefault(t, s)

	first := map[string]any{
		"choice": "B",
		"meta":   map[string]any{"elapsed": float64(3), "tags": []any{"a", "b"}},
	}
	id1, dup, err := submit(t, s, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      first,
		DurationMs:    ptrInt32(12000),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
		At:            subBase,
	})
	if err != nil || dup {
		t.Fatalf("首次提交必须真实入账: dup=%v err=%v", dup, err)
	}

	// 重放：键序重排 + int64 替代 float64 + 耗时不同——语义上仍是同一次作答.
	replay := map[string]any{
		"meta":   map[string]any{"tags": []any{"a", "b"}, "elapsed": int64(3)},
		"choice": "B",
	}
	id2, dup2, err := submit(t, s, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      replay,
		DurationMs:    ptrInt32(13500),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match", "replay": true},
		At:            subBase.Add(time.Second),
	})
	if err != nil {
		t.Fatalf("幂等重放必须成功（全时态幂等，含会话已推进后）: %v", err)
	}
	if !dup2 {
		t.Fatal("重放必须命中幂等分支（duplicate=true）")
	}
	if id1 != id2 {
		t.Fatalf("重放必须返回首次事件 id: %s vs %s", id1, id2)
	}

	// 零副作用：账面恰一条、进度恰推进 1、恰一次作答计数.
	if got := len(s.Events()); got != 1 {
		t.Fatalf("重放不得新增事件: 账面 %d 条", got)
	}
	snap, err := s.SessionSnapshot(subSession)
	if err != nil {
		t.Fatal(err)
	}
	if snap.CurrentIndex != 1 || snap.AnsweredCount != 1 {
		t.Fatalf("重放不得重复推进: currentIndex=%d answeredCount=%d", snap.CurrentIndex, snap.AnsweredCount)
	}
	// 事件账内容保真：别名/场景/会话归属/时刻/载荷逐项锚定（账本存的是
	// response 载荷本身，指纹口径比对与键序/表示无关）.
	ev := s.Events()[0]
	if ev.EventID != id1 || ev.StudentAliasID != subAlias || ev.SessionID != subSession {
		t.Fatalf("事件身份字段漂移: %+v", ev)
	}
	if ev.Scene != events.ScenePractice {
		t.Fatalf("scene 必须取自会话行（D5）: %q", ev.Scene)
	}
	if !ev.CreatedAt.Equal(subBase) {
		t.Fatalf("created_at 必须取提交时刻: %v", ev.CreatedAt)
	}
	if d, err := validators.ContentDigest(ev.RawPayload); err != nil || d != mustDigest(t, first) {
		t.Fatalf("raw_payload 载荷保真失败: %v %s", err, d)
	}
}

// TestConcurrentSameFingerprintExactlyOneEvent 验收主用例：N 并发提交同一题
// 同一作答内容 → 恰一条 response_event、current_index 恰推进 1、全部返回
// 一致的事件 id（恰一次 duplicate=false，其余 duplicate=true）.
func TestConcurrentSameFingerprintExactlyOneEvent(t *testing.T) {
	const n = 32
	s := newSubmitStore(t)
	seedDefault(t, s)

	var (
		ids        = make([]string, n)
		dups       = make([]bool, n)
		start      = make(chan struct{})
		readyCount = make(chan struct{}, n)
		eg         errgroup.Group
	)
	for i := range n {
		eg.Go(func() error {
			// 并发压力形态：各 goroutine 用独立 map 实例与不同耗时——
			// 同内容异实例必须收敛到同一指纹（幂等键归一的前提）.
			resp := map[string]any{"kind": "single_choice", "choice": "B"}
			readyCount <- struct{}{}
			<-start
			id, dup, err := s.SubmitAnswer(context.Background(), nil, SubmitInput{
				SessionID:     subSession,
				ItemVersionID: "item-aaa",
				Response:      resp,
				DurationMs:    ptrInt32(int32(1000 + i)),
				ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
			})
			if err != nil {
				return fmt.Errorf("并发同指纹提交意外失败: %w", err)
			}
			ids[i], dups[i] = id, dup
			return nil
		})
	}
	// 全员就绪后同时起跑（真实并发交织，非退化串行）.
	for range n {
		<-readyCount
	}
	close(start)
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	realCount := 0
	for i := range n {
		if !dups[i] {
			realCount++
		}
		if ids[i] != ids[0] {
			t.Fatalf("并发同指纹返回结果必须一致: [%d] %s vs %s", i, ids[i], ids[0])
		}
	}
	if realCount != 1 {
		t.Fatalf("同指纹并发只能有一次真实入账: %d 次", realCount)
	}
	if got := len(s.Events()); got != 1 {
		t.Fatalf("恰好一条 response_event: %d 条", got)
	}
	snap, err := s.SessionSnapshot(subSession)
	if err != nil {
		t.Fatal(err)
	}
	if snap.CurrentIndex != 1 || snap.AnsweredCount != 1 {
		t.Fatalf("current_index 必须恰推进 1: currentIndex=%d answeredCount=%d", snap.CurrentIndex, snap.AnsweredCount)
	}
}

// TestConcurrentDistinctAnswersSameItemExactlyOneWins：同题异答的并发提交
// ——指纹互异（幂等不命中），题序纪律决定恰一条越过推进点，其余得到明确
// 的 ErrOutOfSequence（异常不泄漏、失败可判型），账面仍恰一条、进度恰 +1.
func TestConcurrentDistinctAnswersSameItemExactlyOneWins(t *testing.T) {
	const n = 16
	s := newSubmitStore(t)
	seedDefault(t, s)

	var (
		outOfSeq   int
		mu         sync.Mutex
		start      = make(chan struct{})
		readyCount = make(chan struct{}, n)
		eg         errgroup.Group
	)
	for i := range n {
		eg.Go(func() error {
			readyCount <- struct{}{}
			<-start
			_, dup, err := s.SubmitAnswer(context.Background(), nil, SubmitInput{
				SessionID:     subSession,
				ItemVersionID: "item-aaa",
				Response:      answer(fmt.Sprintf("choice-%02d", i)),
				ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
			})
			if err != nil {
				if !errors.Is(err, ErrOutOfSequence) {
					return fmt.Errorf("异指纹失败必须锚定题序哨兵: %w", err)
				}
				var oos *OutOfSequenceError
				if !errors.As(err, &oos) || oos.Expected != "item-bbb" || oos.Got != "item-aaa" {
					return fmt.Errorf("题序载体应携带期望/实收题: %v", err)
				}
				mu.Lock()
				outOfSeq++
				mu.Unlock()
				return nil
			}
			if dup {
				t.Error("异指纹提交不得命中幂等分支")
			}
			return nil
		})
	}
	// 全员就绪后同时起跑（真实并发交织，非退化串行）.
	for range n {
		<-readyCount
	}
	close(start)
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	if outOfSeq != n-1 {
		t.Fatalf("异指纹并发应恰 %d 个题序拒绝: %d 个", n-1, outOfSeq)
	}
	if got := len(s.Events()); got != 1 {
		t.Fatalf("恰好一条 response_event: %d 条", got)
	}
	snap, err := s.SessionSnapshot(subSession)
	if err != nil {
		t.Fatal(err)
	}
	if snap.CurrentIndex != 1 {
		t.Fatalf("current_index 必须恰推进 1: %d", snap.CurrentIndex)
	}
}

// TestDistinctFingerprintsAllRecorded 「不同指纹 → 全入账」：指纹判重的
// 逆向健全性——互异内容绝不被幂等层误折叠。逐题顺序提交（题序纪律下同题
// 异答不可双入账），每题每答恰一条事件、进度逐次 +1、走完自动完结
// （未开回测，Python 同构）.
func TestDistinctFingerprintsAllRecorded(t *testing.T) {
	s := newSubmitStore(t)
	if err := s.SeedSession(SeedInput{
		SessionID:      subSession,
		StudentAliasID: subAlias,
		Scene:          "practice",
		Sequence:       []string{"item-aaa", "item-bbb", "item-ccc"},
		TimeLimitSec:   1800,
		LastResumeAt:   subBase,
	}); err != nil {
		t.Fatal(err)
	}
	items := []string{"item-aaa", "item-bbb", "item-ccc"}
	for i, item := range items {
		id, dup, err := submit(t, s, SubmitInput{
			SessionID:     subSession,
			ItemVersionID: item,
			Response:      answer(fmt.Sprintf("choice-%d", i)),
			ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
			At:            subBase.Add(time.Duration(i) * time.Second),
		})
		if err != nil || dup {
			t.Fatalf("第 %d 题互异提交必须真实入账: dup=%v err=%v", i, dup, err)
		}
		if id == "" {
			t.Fatalf("第 %d 题必须返回事件 id", i)
		}
	}
	if got := len(s.Events()); got != len(items) {
		t.Fatalf("互异作答全入账: %d/%d 条", got, len(items))
	}
	snap, err := s.SessionSnapshot(subSession)
	if err != nil {
		t.Fatal(err)
	}
	if snap.CurrentIndex != len(items) {
		t.Fatalf("current_index 应推进至序列末: %d", snap.CurrentIndex)
	}
	if snap.Status != StatusCompleted || snap.CompletedAt == nil {
		t.Fatalf("未开回测的会话走完应自动完结: status=%q completedAt=%v", snap.Status, snap.CompletedAt)
	}
	// 走完后再提交（新内容）→ 明确拒绝且零写入.
	_, _, err = submit(t, s, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("late"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
	})
	if !errors.Is(err, ErrSessionCompleted) {
		t.Fatalf("走完后的新提交必须锚定完结哨兵: %v", err)
	}
	if got := len(s.Events()); got != len(items) {
		t.Fatalf("拒绝路径零写入: 账面 %d 条", got)
	}
}

// TestConcurrentDistinctSessionsIndependent：并发落在不同会话上互不干扰、
// 全部入账——per-session 临界区不产生跨会话假串行或指纹跨会话误折叠.
func TestConcurrentDistinctSessionsIndependent(t *testing.T) {
	const n = 16
	s := newSubmitStore(t)
	for i := range n {
		if err := s.SeedSession(SeedInput{
			SessionID:      fmt.Sprintf("33333333-3333-4444-8555-%012d", i),
			StudentAliasID: subAlias,
			Scene:          "practice",
			Sequence:       []string{fmt.Sprintf("item-%02d", i)},
			TimeLimitSec:   1800,
			LastResumeAt:   subBase,
		}); err != nil {
			t.Fatal(err)
		}
	}
	var eg errgroup.Group
	for i := range n {
		eg.Go(func() error {
			_, dup, err := s.SubmitAnswer(context.Background(), nil, SubmitInput{
				SessionID:     fmt.Sprintf("33333333-3333-4444-8555-%012d", i),
				ItemVersionID: fmt.Sprintf("item-%02d", i),
				Response:      answer("same-everywhere"), // 同内容跨会话不构成重复
				ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
			})
			if err != nil || dup {
				return fmt.Errorf("会话 %02d 应独立入账: dup=%v err=%v", i, dup, err)
			}
			return nil
		})
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}
	if got := len(s.Events()); got != n {
		t.Fatalf("跨会话提交全入账: %d/%d 条", got, n)
	}
}

// TestSubmitSequenceDiscipline 锁题序纪律：非当前题拒绝（载体携带期望/实收
// 对）、跳答零写入；重放已答过的历史题在幂等面上成功、在异内容面上拒绝.
func TestSubmitSequenceDiscipline(t *testing.T) {
	s := newSubmitStore(t)
	seedDefault(t, s)

	// 跳答：当前应答是 item-aaa，直接交 item-bbb.
	_, _, err := submit(t, s, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-bbb",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
	})
	var oos *OutOfSequenceError
	if !errors.As(err, &oos) || !errors.Is(err, ErrOutOfSequence) {
		t.Fatalf("跳答必须锚定题序哨兵与载体: %v", err)
	}
	if oos.Expected != "item-aaa" || oos.Got != "item-bbb" {
		t.Fatalf("题序载体期望/实收漂移: %+v", oos)
	}
	if got := len(s.Events()); got != 0 {
		t.Fatalf("跳答零写入: %d 条", got)
	}

	// 正常答第一题后，异内容重提同题（不同指纹、非幂等）→ 题序拒绝.
	if _, _, err := submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"),
		ScoringTrace: map[string]any{"scorer_id": "exact_match"},
	}); err != nil {
		t.Fatal(err)
	}
	_, _, err = submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("C"),
		ScoringTrace: map[string]any{"scorer_id": "exact_match"},
	})
	if !errors.Is(err, ErrOutOfSequence) {
		t.Fatalf("推进后异内容重提同题必须题序拒绝: %v", err)
	}
	if got := len(s.Events()); got != 1 {
		t.Fatalf("拒绝路径零写入: %d 条", got)
	}
}

// TestSubmitTimeProtectionRestPrompt 时长保护（§4.8 用眼保护）：连续作答
// 超阈值 → 拒绝且会话置 rest_prompted、零事件写入；边界（恰等于阈值）放行.
func TestSubmitTimeProtectionRestPrompt(t *testing.T) {
	s := newSubmitStore(t)
	seedDefault(t, s)

	// 边界内：elapsed == limit（严格 > 才触发）放行.
	if _, dup, err := submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"),
		ScoringTrace: map[string]any{"scorer_id": "exact_match"},
		At:           subBase.Add(1800 * time.Second),
	}); err != nil || dup {
		t.Fatalf("阈值边界必须放行: dup=%v err=%v", dup, err)
	}

	// 超限 1 秒：拒绝 + rest_prompted 置位 + 零事件写入.
	at := subBase.Add(1801 * time.Second)
	_, _, err := submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-bbb", Response: answer("B"),
		ScoringTrace: map[string]any{"scorer_id": "exact_match"},
		At:           at,
	})
	var rre *RestRequiredError
	if !errors.As(err, &rre) || !errors.Is(err, ErrRestRequired) {
		t.Fatalf("超时提交必须锚定时长保护哨兵: %v", err)
	}
	if rre.ElapsedSec != 1801 || rre.TimeLimitSec != 1800 {
		t.Fatalf("时长保护载体漂移: %+v", rre)
	}
	snap, err := s.SessionSnapshot(subSession)
	if err != nil {
		t.Fatal(err)
	}
	if snap.Status != StatusRestPrompted {
		t.Fatalf("超时后应置 rest_prompted: %q", snap.Status)
	}
	if got := len(s.Events()); got != 1 {
		t.Fatalf("时长保护拒绝零事件写入: %d 条", got)
	}
	if snap.AnsweredCount != 1 {
		t.Fatalf("时长保护拒绝零推进: answered=%d", snap.AnsweredCount)
	}
}

// TestSubmitStateAndPresenceFailClosed 状态与存在性：已完成/已放弃/不存在
// 一律明确拒绝且零写入（重复提交的幂等面不受影响——幂等判定先于状态校验）.
func TestSubmitStateAndPresenceFailClosed(t *testing.T) {
	cases := []struct {
		name    string
		seed    func(t *testing.T, s *MemoryStore)
		wantErr error
	}{
		{"会话不存在", func(t *testing.T, s *MemoryStore) {}, ErrSessionNotFound},
		{"已放弃", func(t *testing.T, s *MemoryStore) {
			seedWithStatus(t, s, StatusAbandoned)
		}, ErrSessionState},
		{"已完成", func(t *testing.T, s *MemoryStore) {
			seedWithStatus(t, s, StatusCompleted)
		}, ErrSessionCompleted},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := newSubmitStore(t)
			tc.seed(t, s)
			_, _, err := submit(t, s, SubmitInput{
				SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"),
				ScoringTrace: map[string]any{"scorer_id": "exact_match"},
			})
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("必须锚定 %v: %v", tc.wantErr, err)
			}
			if got := len(s.Events()); got != 0 {
				t.Fatalf("拒绝路径零写入: %d 条", got)
			}
		})
	}
}

// TestSubmitInputValidation 契约前置校验（共享管线——内存与 PG 同判据）：
// 身份/载荷/轨迹/耗时违例在进入临界区之前拒绝.
func TestSubmitInputValidation(t *testing.T) {
	s := newSubmitStore(t)
	seedDefault(t, s)
	cases := []struct {
		name string
		in   SubmitInput
	}{
		{"session_id 非 UUID", SubmitInput{SessionID: "not-a-uuid", ItemVersionID: "i", Response: answer("B"), ScoringTrace: map[string]any{}}},
		{"题目版本为空", SubmitInput{SessionID: subSession, ItemVersionID: "", Response: answer("B"), ScoringTrace: map[string]any{}}},
		{"载荷缺失", SubmitInput{SessionID: subSession, ItemVersionID: "item-aaa", ScoringTrace: map[string]any{}}},
		{"评分轨迹缺失", SubmitInput{SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B")}},
		{"耗时负数", SubmitInput{SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"), ScoringTrace: map[string]any{}, DurationMs: ptrInt32(-1)}},
		{"载荷不可规范化", SubmitInput{SessionID: subSession, ItemVersionID: "item-aaa", Response: map[string]any{"bad": make(chan int)}, ScoringTrace: map[string]any{}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := submit(t, s, tc.in)
			if !errors.Is(err, ErrInvalidSubmission) {
				t.Fatalf("必须锚定入参哨兵: %v", err)
			}
		})
	}
	if got := len(s.Events()); got != 0 {
		t.Fatalf("前置校验零写入: %d 条", got)
	}
}

// TestSeedSessionValidation 开立面同样 fail-closed（装配错误不给运行期埋雷）.
func TestSeedSessionValidation(t *testing.T) {
	s := newSubmitStore(t)
	cases := []SeedInput{
		{SessionID: "bad", StudentAliasID: subAlias, Scene: "practice", Sequence: []string{"i"}, TimeLimitSec: 1},
		{SessionID: subSession, StudentAliasID: "bad", Scene: "practice", Sequence: []string{"i"}, TimeLimitSec: 1},
		{SessionID: subSession, StudentAliasID: subAlias, Scene: "measurement-offline", Sequence: []string{"i"}, TimeLimitSec: 1},
		// measurement 是 D5 合法场景但无在线会话入口（0011 二值域，与 PG CHECK 同构）.
		{SessionID: subSession, StudentAliasID: subAlias, Scene: "measurement", Sequence: []string{"i"}, TimeLimitSec: 1},
		{SessionID: subSession, StudentAliasID: subAlias, Scene: "practice", Sequence: nil, TimeLimitSec: 1},
		{SessionID: subSession, StudentAliasID: subAlias, Scene: "practice", Sequence: []string{"i"}, TimeLimitSec: 0},
	}
	for i, in := range cases {
		if err := s.SeedSession(in); !errors.Is(err, ErrInvalidSubmission) && !errors.Is(err, ErrLedgerCorrupted) {
			t.Fatalf("seed 用例 %d 必须拒绝: %v", i, err)
		}
	}
}

// TestSubmitRecordsAreDeepCopies 交出副本契约：改写返回的事件记录/快照不得
// 污染内部账（append-only 只读投影 + -race 干净的结构前提）.
func TestSubmitRecordsAreDeepCopies(t *testing.T) {
	s := newSubmitStore(t)
	seedDefault(t, s)
	if _, _, err := submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"),
		DurationMs:   ptrInt32(5),
		ScoringTrace: map[string]any{"scorer_id": "exact_match"},
	}); err != nil {
		t.Fatal(err)
	}
	evs := s.Events()
	evs[0].RawPayload["choice"] = "篡改"
	evs[0].ScoringTrace["scorer_id"] = "篡改"
	if d, err := validators.ContentDigest(s.Events()[0].RawPayload); err != nil || d != mustDigest(t, answer("B")) {
		t.Fatalf("事件账可被外部改写（append-only 契约破坏）: %v %s", err, d)
	}
	snap, _ := s.SessionSnapshot(subSession)
	snap.CurrentIndex = 99
	again, _ := s.SessionSnapshot(subSession)
	if again.CurrentIndex != 1 {
		t.Fatalf("会话快照可被外部改写: %d", again.CurrentIndex)
	}
}

// TestSubmittedEventsProjection 锁事件账投影与 Writer 入参的口径一致性：
// scene 取会话行、推断数组 nil 语义保持、身份三字段齐备（W6 换装 PG 后
// Writer.Record 的输入即由同一 preparedSubmit 装配）.
func TestSubmittedEventsProjection(t *testing.T) {
	s := newSubmitStore(t)
	if err := s.SeedSession(SeedInput{
		SessionID: subSession, StudentAliasID: subAlias, Scene: "diagnosis",
		Sequence: []string{"item-aaa"}, TimeLimitSec: 60, LastResumeAt: subBase,
	}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := submit(t, s, SubmitInput{
		SessionID: subSession, ItemVersionID: "item-aaa", Response: answer("B"),
		ScoringTrace:    map[string]any{"scorer_id": "exact_match"},
		ErrorInferences: []map[string]any{{"error_type_id": "math.sign"}},
	}); err != nil {
		t.Fatal(err)
	}
	ev := s.Events()[0]
	if ev.Scene != events.SceneDiagnosis {
		t.Fatalf("scene 应取会话行 diagnosis: %q", ev.Scene)
	}
	if len(ev.ErrorInferences) != 1 || ev.ErrorInferences[0]["error_type_id"] != "math.sign" {
		t.Fatalf("错误推断投影漂移: %+v", ev.ErrorInferences)
	}
	if !events.ValidScene(ev.Scene) {
		t.Fatalf("落账 scene 必须在 D5 值域内: %q", ev.Scene)
	}
}

// TestPGSubmitRequiresTransaction PG 实现的 D11 fail-closed 字面语义：无显式
// 事务执行面的提交一律 ErrNoTransaction（真库并发语义——advisory lock 串行化、
// 23505 兜底翻译的运行时行为——留给 CI 的 migrate-go-check/集成测试，此处不
// 宣称覆盖）.
func TestPGSubmitRequiresTransaction(t *testing.T) {
	pg := NewPGStore()
	_, _, err := pg.SubmitAnswer(context.Background(), nil, SubmitInput{
		SessionID:     subSession,
		ItemVersionID: "item-aaa",
		Response:      answer("B"),
		ScoringTrace:  map[string]any{"scorer_id": "exact_match"},
	})
	if !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("无事务面的提交必须锚定 ErrNoTransaction: %v", err)
	}
}

// TestMapUniqueViolation 锁 23505 翻译纪律：幂等登记账唯一主键拒绝映射为
// ErrSubmissionConflict，且原始驱动错误留在 wrap 链可回溯（吞掉 SQLSTATE
// 证据是反模式）；非唯一冲突不得误报.
func TestMapUniqueViolation(t *testing.T) {
	pgErr := &pgconn.PgError{Code: sqlStateUniqueViolation, Message: "duplicate key"}
	wrapped := mapUniqueViolation(fmt.Errorf("session/pg register submission: %w", pgErr))
	if !errors.Is(wrapped, ErrSubmissionConflict) {
		t.Fatalf("23505 应映射为 ErrSubmissionConflict: %v", wrapped)
	}
	var recovered *pgconn.PgError
	if !errors.As(wrapped, &recovered) || recovered.Code != sqlStateUniqueViolation {
		t.Fatalf("原始 23505 证据链断裂: %v", wrapped)
	}
	other := mapUniqueViolation(&pgconn.PgError{Code: "42P01", Message: "no such table"})
	if errors.Is(other, ErrSubmissionConflict) {
		t.Fatal("非唯一冲突不得误报为幂等冲突")
	}
}

// TestBothImplementationsSatisfyContract 用接口双重锚定保证两实现的调用形态
// 一致（内存与 PG 骨架都实现 SubmissionStore，W6 接线时可无缝换装）.
func TestBothImplementationsSatisfyContract(t *testing.T) {
	var _ SubmissionStore = (*MemoryStore)(nil)
	var _ SubmissionStore = (*PGStore)(nil)
}

// ── 测试小工具 ────────────────────────────────────────────────────────────

func ptrInt32(n int32) *int32 { return &n }

// seedWithStatus 以指定初始状态开立默认会话（状态拒绝路径专用）.
func seedWithStatus(t *testing.T, s *MemoryStore, status string) {
	t.Helper()
	if err := s.SeedSession(SeedInput{
		SessionID:      subSession,
		StudentAliasID: subAlias,
		Scene:          "practice",
		Sequence:       []string{"item-aaa"},
		TimeLimitSec:   1800,
		Status:         status,
		LastResumeAt:   subBase,
	}); err != nil {
		t.Fatal(err)
	}
}

// mustDigest 计算测试期望摘要（复用被测口径函数——判据单一来源）.
func mustDigest(t *testing.T, v any) string {
	t.Helper()
	d, err := validators.ContentDigest(v)
	if err != nil {
		t.Fatalf("摘要计算: %v", err)
	}
	return d
}
