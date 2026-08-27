package compliance

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/jackc/pgx/v5/pgconn"
)

// 测试以内存实现承载 T-W5-011 的全部可本地验证语义（go test -race 下运行）：
// - 并发写入串行化：版本连续无重复、终态链顶唯一且确定（验收 #2）；
// - 撤回语义：append-only 新版本行、永远取最新版本判定、前置失败零副作用（验收 #3）；
// - 前置校验与留痕「谁」维度；账本只读投影不被外部改写。
// 全部断言不依赖 sleep 制造顺序（验收 #5）；PG 实现的运行时行为不在此宣称覆盖
// （无 Docker/PG），仅测错误分类等纯函数面。

var testStudent = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

func mustGrant(t *testing.T, s ConsentStore, in GrantInput) *ConsentEvent {
	t.Helper()
	ev, err := s.RecordGrant(context.Background(), nil, in)
	if err != nil {
		t.Fatalf("RecordGrant(%s@%s) 意外失败: %v", in.StudentAliasID, in.Purpose, err)
	}
	return ev
}

func mustRevoke(t *testing.T, s ConsentStore, in RevokeInput) *ConsentEvent {
	t.Helper()
	ev, err := s.Revoke(context.Background(), nil, in)
	if err != nil {
		t.Fatalf("Revoke(%s@%s) 意外失败: %v", in.StudentAliasID, in.Purpose, err)
	}
	return ev
}

func TestInvalidInputRejected(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.AddDate(1, 0, 0)

	cases := []struct {
		name string
		in   GrantInput
		want error
	}{
		{"空 purpose", GrantInput{StudentAliasID: testStudent, Purpose: "", ValidUntil: until}, ErrInvalidScope},
		{"非 UUID 学生", GrantInput{StudentAliasID: "student-x", Purpose: "practice", ValidUntil: until}, ErrInvalidStudentAlias},
		{"Extra 抢占 purpose 键", GrantInput{StudentAliasID: testStudent, Purpose: "practice", ValidUntil: until,
			Extra: map[string]any{"purpose": "diagnosis"}}, ErrInvalidScope},
		{"Extra 不可序列化", GrantInput{StudentAliasID: testStudent, Purpose: "practice", ValidUntil: until,
			Extra: map[string]any{"subject": func() {}}}, ErrInvalidScope},
		{"valid_until 缺省", GrantInput{StudentAliasID: testStudent, Purpose: "practice"}, ErrInvalidWindow},
		{"valid_until 早于 valid_from", GrantInput{StudentAliasID: testStudent, Purpose: "practice",
			ValidFrom: base, ValidUntil: base.Add(-time.Second)}, ErrInvalidWindow},
		{"valid_until 等于 valid_from", GrantInput{StudentAliasID: testStudent, Purpose: "practice",
			ValidFrom: base, ValidUntil: base}, ErrInvalidWindow},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := s.RecordGrant(ctx, nil, tc.in); !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
			// 非法输入不得改动任何状态：账与授权态均保持空（零副作用）.
			hist, err := s.History(ctx, nil, testStudent, "practice")
			if err != nil {
				t.Fatal(err)
			}
			if len(hist) != 0 {
				t.Fatalf("非法输入入账了 %d 条事件", len(hist))
			}
			st, err := s.CheckConsent(ctx, nil, testStudent, "practice", &base)
			if err != nil {
				t.Fatal(err)
			}
			if st.State != StateMissing || st.IsValid {
				t.Fatalf("非法输入改变了授权状态: %+v", st)
			}
		})
	}

	// 撤回入参同面校验.
	if _, err := s.Revoke(ctx, nil, RevokeInput{StudentAliasID: "bad-alias", Purpose: "practice"}); !errors.Is(err, ErrInvalidStudentAlias) {
		t.Fatalf("撤回侧学生校验缺失: %v", err)
	}
	if _, err := s.Revoke(ctx, nil, RevokeInput{StudentAliasID: testStudent, Purpose: ""}); !errors.Is(err, ErrInvalidScope) {
		t.Fatalf("撤回侧 scope 校验缺失: %v", err)
	}
}

func TestLifecycleAndRevokeSemantics(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.Add(24 * time.Hour)

	// 从未授权 → missing（Version=0）.
	st, err := s.CheckConsent(ctx, nil, testStudent, "practice", &base)
	if err != nil {
		t.Fatal(err)
	}
	if st.State != StateMissing || st.Version != 0 || st.IsValid {
		t.Fatalf("初始态应为 missing: %+v", st)
	}

	g1 := mustGrant(t, s, GrantInput{
		StudentAliasID: testStudent, Purpose: "practice",
		ValidFrom: base, ValidUntil: until, RecordedBy: "parent-alice", At: base,
	})
	if g1.Version != 1 || g1.EventType != EventGrant {
		t.Fatalf("首个 grant 应为版本 1: %+v", g1)
	}
	if g1.Scope["purpose"] != "practice" {
		t.Fatalf("scope 载荷缺 purpose 主键: %+v", g1.Scope)
	}
	if g1.RecordedBy != "parent-alice" {
		t.Fatalf("留痕 who 缺失: %+v", g1)
	}

	r1 := mustRevoke(t, s, RevokeInput{StudentAliasID: testStudent, Purpose: "practice",
		RecordedBy: "parent-bob", At: base.Add(time.Hour)})
	if r1.Version != 2 || r1.EventType != EventRevoke {
		t.Fatalf("撤回应为 append-only 新版本行（v2 revoke）: %+v", r1)
	}
	// CHECK 约束镜像：revoke 行两个时刻列必须 NULL.
	if r1.ValidFrom != nil || r1.ValidUntil != nil {
		t.Fatalf("revoke 行时刻列应为 NULL: %+v", r1)
	}

	st, err = s.CheckConsent(ctx, nil, testStudent, "practice", &base)
	if err != nil {
		t.Fatal(err)
	}
	if st.State != StateRevoked || st.Version != 2 || st.IsValid {
		t.Fatalf("最新版本是 revoke 即整体失效: %+v", st)
	}

	// 无有效授权可再撤回：明确拒绝且零副作用——不烧版本号、不入账.
	before, _ := s.History(ctx, nil, testStudent, "practice")
	if _, err := s.Revoke(ctx, nil, RevokeInput{StudentAliasID: testStudent, Purpose: "practice",
		At: base.Add(2 * time.Hour)}); !errors.Is(err, ErrNoActiveConsent) {
		t.Fatalf("重复撤回应报 ErrNoActiveConsent: %v", err)
	}
	after, _ := s.History(ctx, nil, testStudent, "practice")
	if len(before) != len(after) {
		t.Fatalf("失败的撤回不得产生新版本行: before=%d after=%d", len(before), len(after))
	}

	// 再授权走链上第三个版本：链单调续接，不重排历史.
	g2 := mustGrant(t, s, GrantInput{StudentAliasID: testStudent, Purpose: "practice",
		ValidUntil: until.AddDate(1, 0, 0), RecordedBy: "parent-carol", At: base.Add(3 * time.Hour)})
	if g2.Version != 3 {
		t.Fatalf("再授权应为 v3: %+v", g2)
	}

	// 留痕时间线：who/when/from→to 完整可还原（append-only 只增不改）.
	hist, err := s.History(ctx, nil, testStudent, "practice")
	if err != nil {
		t.Fatal(err)
	}
	wantChain := []struct {
		v    int
		et   EventType
		who  string
		when time.Time
	}{
		{1, EventGrant, "parent-alice", base},
		{2, EventRevoke, "parent-bob", base.Add(time.Hour)},
		{3, EventGrant, "parent-carol", base.Add(3 * time.Hour)},
	}
	for i, w := range wantChain {
		ev := hist[i]
		if ev.Version != w.v || ev.EventType != w.et || ev.RecordedBy != w.who || !ev.CreatedAt.Equal(w.when) {
			t.Fatalf("第 %d 条留痕不符: got %+v want %+v", i, ev, w)
		}
		if ev.ConsentID == "" {
			t.Fatalf("consent_id 未发号: %+v", ev)
		}
	}
	// 三个事件 id 两两互异（发号面唯一性抽查）.
	if hist[0].ConsentID == "" || hist[1].ConsentID == "" ||
		hist[2].ConsentID == hist[0].ConsentID || hist[2].ConsentID == hist[1].ConsentID {
		t.Fatal("事件 id 发生碰撞")
	}
}

func TestExpiredBoundaryIsExclusiveValid(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.Add(time.Hour)

	mustGrant(t, s, GrantInput{StudentAliasID: testStudent, Purpose: "diagnosis",
		ValidUntil: until, RecordedBy: "system", At: base})

	// 边界语义与 Python 冻结实现一致：now < valid_until 才有效，now >= valid_until 即过期.
	cases := []struct {
		name string
		now  time.Time
		want State
	}{
		{"截止前一瞬仍有效", until.Add(-time.Nanosecond), StateGranted},
		{"恰在截止时刻已过期", until, StateExpired},
		{"截止之后已过期", until.Add(time.Minute), StateExpired},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			st, err := s.CheckConsent(ctx, nil, testStudent, "diagnosis", &tc.now)
			if err != nil {
				t.Fatal(err)
			}
			if st.State != tc.want || st.IsValid != (tc.want == StateGranted) {
				t.Fatalf("got %+v want state %s", st, tc.want)
			}
		})
	}

	// 过期授权不可撤回（Python 冻结实现 NoActiveConsentError 同口径）：
	// 撤回判定以 At 为基准时刻，避免真实时钟污染.
	if _, err := s.Revoke(ctx, nil, RevokeInput{StudentAliasID: testStudent, Purpose: "diagnosis",
		At: until.Add(time.Minute)}); !errors.Is(err, ErrNoActiveConsent) {
		t.Fatalf("过期授权撤回应被拒绝: %v", err)
	}
}

// deriveStateFromHistory 是并发测试的账实一致性判据：从全量账逐条重放出期望
// 链顶态，与 CheckConsent 的结果对照——不看 goroutine 完成序，只看账本自身.
func deriveStateFromHistory(t *testing.T, hist []ConsentEvent, at time.Time) (State, int) {
	t.Helper()
	if len(hist) == 0 {
		return StateMissing, 0
	}
	top := hist[len(hist)-1]
	st := stateAt(top.StudentAliasID, top.Scope["purpose"].(string), &top, at)
	return st.State, st.Version
}

// TestConcurrentGrantsContiguousVersionsExactlyOneActive 是验收 #2 的主用例：
// N 个 goroutine 对同一授权链并发授予（各带显式 At 以免时钟竞争扰动），
// 在 -race 下必须满足——零异常泄漏；版本连续无重复（唯一索引防线的内存等效
// 不变量）；最终生效记录唯一且确定（链顶即最大版本，恰一行）.
func TestConcurrentGrantsContiguousVersionsExactlyOneActive(t *testing.T) {
	const n = 64
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.AddDate(1, 0, 0)

	var eg errgroup.Group
	for i := range n {
		eg.Go(func() error {
			at := base.Add(time.Duration(i) * time.Millisecond)
			ev, err := s.RecordGrant(ctx, nil, GrantInput{
				StudentAliasID: testStudent, Purpose: "practice",
				ValidUntil: until, RecordedBy: fmt.Sprintf("parent-%02d", i),
				At: at,
			})
			if err != nil {
				return fmt.Errorf("并发授权意外失败: %w", err)
			}
			if ev.EventType != EventGrant || ev.Scope["purpose"] != "practice" {
				return fmt.Errorf("回执内容错乱: %+v", ev)
			}
			return nil
		})
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	hist, err := s.History(ctx, nil, testStudent, "practice")
	if err != nil {
		t.Fatal(err)
	}
	if len(hist) != n {
		t.Fatalf("账上行数应等于并发写入数: %d vs %d", len(hist), n)
	}
	seen := make(map[int]bool, n)
	for i, ev := range hist {
		if seen[ev.Version] {
			t.Fatalf("版本号重复: %d（行 %d）", ev.Version, i)
		}
		seen[ev.Version] = true
		if ev.Version != i+1 {
			t.Fatalf("版本号不连续: 行 %d 版本 %d", i, ev.Version)
		}
		if ev.RecordedBy == "" {
			t.Fatalf("留痕 who 缺失: %+v", ev)
		}
	}

	// 判定时刻取窗口内一瞬（晚于一切 vfrom、早于公共 valid_until），
	// 账实必须一致且终态唯一确定为链顶 vN.
	mid := until.Add(-time.Hour)
	got, err := s.CheckConsent(ctx, nil, testStudent, "practice", &mid)
	if err != nil {
		t.Fatal(err)
	}
	wantState, wantVersion := deriveStateFromHistory(t, hist, mid)
	if got.State != wantState || got.State != StateGranted {
		t.Fatalf("账实不一致或终态非 granted: got %+v want (%s,%d)", got, wantState, wantVersion)
	}
	if got.Version != wantVersion || wantVersion != n {
		t.Fatalf("最终生效版本应唯一确定为链顶 v%d: %+v", n, got)
	}
}

// TestConcurrentGrantRevokeFloodStaysCoherent：grant/revoke 任意交织的洪峰下，
// 每次调用要么完整落账要么以 ErrNoActiveConsent 明确失败；账本永远满足
// 「版本连续无重」且 CheckConsent 与账本重放严格自洽——不存在半写状态可被观察.
func TestConcurrentGrantRevokeFloodStaysCoherent(t *testing.T) {
	const writers = 48
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.AddDate(2, 0, 0)
	// 判定时刻取活窗内一瞬：撤回的有效性真的取决于与授权的交织顺序，
	// 洪峰因此同时覆盖「接受」与「闭门羹」两条路径.
	decide := until.Add(-time.Hour)

	var okCount atomic.Int32
	var conflictCount atomic.Int32
	var eg errgroup.Group
	for i := range writers {
		i := i
		switch i % 2 {
		case 0:
			eg.Go(func() error {
				_, err := s.RecordGrant(ctx, nil, GrantInput{
					StudentAliasID: testStudent, Purpose: "measurement",
					ValidUntil: until, RecordedBy: fmt.Sprintf("writer-%02d", i), At: base,
				})
				switch {
				case err == nil:
					okCount.Add(1)
				case errors.Is(err, ErrNoActiveConsent):
					conflictCount.Add(1) // grant 不可能命中此分类；进入即计错
					return fmt.Errorf("grant 绝不应报无有效授权: %w", err)
				default:
					return err
				}
				return nil
			})
		default:
			eg.Go(func() error {
				_, err := s.Revoke(ctx, nil, RevokeInput{
					StudentAliasID: testStudent, Purpose: "measurement",
					RecordedBy: fmt.Sprintf("writer-%02d", i), At: decide,
				})
				switch {
				case err == nil:
					okCount.Add(1)
				case errors.Is(err, ErrNoActiveConsent):
					conflictCount.Add(1) // 合法分支：撤回撞上此刻本就无有效授权
				default:
					return err
				}
				return nil
			})
		}
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	hist, err := s.History(ctx, nil, testStudent, "measurement")
	if err != nil {
		t.Fatal(err)
	}
	totalOK := int(okCount.Load())
	if len(hist) != totalOK {
		t.Fatalf("落账行数(%d) 应等于成功调用数(%d)", len(hist), totalOK)
	}
	for i, ev := range hist {
		if ev.Version != i+1 {
			t.Fatalf("版本断裂: 行 %d 版本 %d", i, ev.Version)
		}
		if ev.RecordedBy == "" {
			t.Fatalf("留痕 who 缺失: %+v", ev)
		}
	}
	// 决定论终态：任意交织后账实必须自洽（不许出现双活跃或半写模糊态）.
	wantState, wantVersion := deriveStateFromHistory(t, hist, decide)
	got, err := s.CheckConsent(ctx, nil, testStudent, "measurement", &decide)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != wantState || got.Version != wantVersion {
		t.Fatalf("账实撕裂: got %+v want (%s,%d)", got, wantState, wantVersion)
	}
	if conflictCount.Load() > int32(writers/2) {
		// 至多一半写者（全部 revoke 端）可合法吃闭门羹.
		t.Fatalf("冲突计数异常膨胀: %d", conflictCount.Load())
	}
}

// TestConcurrentDistinctChainsIndependentUniqueness：不同学生 / 不同 purpose 的
// 链各自从 v1 独立编号，互不阻塞互不串号（锁与索引都是二级粒度）.
func TestConcurrentDistinctChainsIndependentUniqueness(t *testing.T) {
	purposes := []string{"practice", "diagnosis", "measurement"}
	students := []string{
		testStudent,
		"9c858901-8a57-4791-81fe-4c455b099bc9",
		"16fd2706-8baf-433b-82eb-8c7fada847da",
	}
	const perChain = 12
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	until := base.AddDate(1, 0, 0)

	var eg errgroup.Group
	for _, sid := range students {
		for _, p := range purposes {
			for range perChain {
				eg.Go(func() error {
					_, err := s.RecordGrant(ctx, nil, GrantInput{
						StudentAliasID: sid, Purpose: p,
						ValidUntil: until, RecordedBy: "sys", At: base,
					})
					return err
				})
			}
		}
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}
	for _, sid := range students {
		for _, p := range purposes {
			hist, err := s.History(ctx, nil, sid, p)
			if err != nil {
				t.Fatal(err)
			}
			if len(hist) != perChain || hist[len(hist)-1].Version != perChain {
				t.Fatalf("链 (%s,%s) 账目不齐: %d 行 链顶 v%d", sid, p, len(hist), hist[len(hist)-1].Version)
			}
		}
	}
}

func TestLedgerProjectionIsReadOnly(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)

	ev := mustGrant(t, s, GrantInput{StudentAliasID: testStudent, Purpose: "practice",
		ValidUntil: base.AddDate(1, 0, 0), RecordedBy: "p1", At: base, Extra: map[string]any{"subject": "math"}})

	// 回执面篡改不影响账.
	ev.RecordedBy = "被篡改"
	ev.Scope["purpose"] = "hacked"

	hist, err := s.History(ctx, nil, testStudent, "practice")
	if err != nil {
		t.Fatal(err)
	}
	if hist[0].RecordedBy != "p1" || hist[0].Scope["subject"] != "math" ||
		hist[0].Scope["purpose"] != "practice" {
		t.Fatalf("账本可被外部改写——append-only 契约破坏: %+v", hist[0])
	}

	// 投影面篡改同样不影响后续读取.
	hist[0].Version = 99
	hist[0].Scope["purpose"] = "again"
	again, _ := s.History(ctx, nil, testStudent, "practice")
	if again[0].Version != 1 || again[0].Scope["purpose"] != "practice" {
		t.Fatalf("History 返回共享引用: %+v", again[0])
	}
}

// TestRepeatedGrantsAppendDistinctVersions 锁死幂等口径：与指针切换不同，
// 授权事件的重复提交是新的审计事实——包层不做合并去重，去重责任在上层端点
// 幂等键（doc.go 已述）。两条连续授予必须是两个递增新版本.
func TestRepeatedGrantsAppendDistinctVersions(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)

	first := mustGrant(t, s, GrantInput{StudentAliasID: testStudent, Purpose: "practice",
		ValidUntil: base.Add(30 * time.Minute), At: base})
	second := mustGrant(t, s, GrantInput{StudentAliasID: testStudent, Purpose: "practice",
		ValidUntil: base.Add(2 * time.Hour), At: base.Add(10 * time.Minute)})

	if first.Version != 1 || second.Version != 2 || first.ConsentID == second.ConsentID {
		t.Fatalf("重复授予应产生独立新版本: v%d/%v 与 v%d/%v",
			first.Version, first.ConsentID, second.Version, second.ConsentID)
	}
	// 双窗重叠的瞬间（两个版本都「没过期」）：生效的是链顶 v2 而非任一旧版——
	// 「永远取最新版本」的判定口径.
	overlap := base.Add(15 * time.Minute)
	st, err := s.CheckConsent(ctx, nil, testStudent, "practice", &overlap)
	if err != nil {
		t.Fatal(err)
	}
	if !st.IsValid || st.Version != 2 {
		t.Fatalf("重叠窗口内应取链顶 v2 且有效: %+v", st)
	}
	// 第一版窗口已尽、第二版仍活的瞬间：同样由 v2 承载有效性.
	later := base.Add(45 * time.Minute)
	st, err = s.CheckConsent(ctx, nil, testStudent, "practice", &later)
	if err != nil {
		t.Fatal(err)
	}
	if !st.IsValid || st.Version != 2 {
		t.Fatalf("第一版过期后应由链顶 v2 继续生效: %+v", st)
	}
}

func TestMapUniqueViolation(t *testing.T) {
	pgErr := &pgconn.PgError{Code: sqlStateUniqueViolation, Message: "duplicate key"}
	wrapped := mapUniqueViolation(fmt.Errorf("compliance/pg insert grant: %w", pgErr))
	if !errors.Is(wrapped, ErrConsentConflict) {
		t.Fatalf("23505 应映射为 ErrConsentConflict: %v", wrapped)
	}
	// 原始驱动错误必须仍可被 errors.As 取回（吞掉真故障细节是反模式）.
	var recovered *pgconn.PgError
	if !errors.As(wrapped, &recovered) || recovered.Code != sqlStateUniqueViolation {
		t.Fatalf("原始 23505 证据链断裂: %v", wrapped)
	}
	other := mapUniqueViolation(&pgconn.PgError{Code: "42P01", Message: "no such table"})
	if errors.Is(other, ErrConsentConflict) {
		t.Fatal("非唯一冲突不得误报为版本冲突")
	}
}

// TestBothImplementationsSatisfyContract 用接口双重锚定保证两实现的调用形态一致
// （内存与 PG 骨架都实现 ConsentStore，W6 接线时可无缝换装）.
func TestBothImplementationsSatisfyContract(t *testing.T) {
	var _ ConsentStore = (*MemoryStore)(nil)
	var _ ConsentStore = (*PGStore)(nil)
}
