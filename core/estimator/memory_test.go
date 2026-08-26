package estimator

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

// 测试以内存实现承载 T-W5-019 的全部可本地验证语义：
// - 并发切换串行化、终态恰好一条活跃指针（验收 #2，go test -race 下运行）；
// - 幂等重放无副作用；
// - 切换留痕 who/when/from/to 完整且只增不改（验收 #3）；
// - 时间回溯取「当时」活跃版本（D6）。
// PG 实现的运行时行为不在此宣称覆盖（无 Docker/PG），仅测错误分类的纯函数面。

// mustSet 是测试便捷封装：失败即 Fatal，避免每个用例堆错误分支.
func mustSet(t *testing.T, s ActivePointerStore, in SetInput) (*EstimatorRun, bool) {
	t.Helper()
	run, switched, err := s.SetActive(context.Background(), nil, in)
	if err != nil {
		t.Fatalf("SetActive(%s@%s) 意外失败: %v", in.PurposeScope, in.ModelVersion, err)
	}
	return run, switched
}

func TestSetInvalidInputRejected(t *testing.T) {
	s := NewMemoryStore()
	cases := []struct {
		name string
		in   SetInput
		want error
	}{
		{"越域场景", SetInput{PurposeScope: "all", ModelVersion: "ctt-v1"}, ErrInvalidScope},
		{"空场景", SetInput{PurposeScope: "", ModelVersion: "ctt-v1"}, ErrInvalidScope},
		{"空版本", SetInput{PurposeScope: ScopePractice, ModelVersion: ""}, ErrEmptyModelVersion},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := s.SetActive(context.Background(), nil, tc.in)
			if !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
			// 非法输入不得改动任何状态：账与活跃指针均保持空.
			trail, _ := s.SwitchTrail(context.Background(), nil, ScopePractice)
			if len(trail) != 0 {
				t.Fatalf("非法输入入账了 %d 条留痕", len(trail))
			}
			got, _ := s.GetActive(context.Background(), nil, ScopePractice, nil)
			if got != nil {
				t.Fatalf("非法输入产生了活跃指针 %+v", got)
			}
		})
	}
}

func TestFirstRegistrationAndSwitchChain(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)

	run1, switched := mustSet(t, s, SetInput{
		PurposeScope: ScopePractice, ModelVersion: "ctt-v1",
		CodeDigest: "digest-a", InputSnapshotID: "snap-1", GraphReleaseID: "rel-1",
		ActivatedBy: "ops-alice", ActivatedAt: base,
	})
	if !switched {
		t.Fatal("首次登记应记为一次真实切换")
	}
	trail, err := s.SwitchTrail(ctx, nil, ScopePractice)
	if err != nil {
		t.Fatal(err)
	}
	if len(trail) != 1 || trail[0].Who != "ops-alice" || trail[0].From != "" ||
		trail[0].To != "ctt-v1" || !trail[0].At.Equal(base) {
		t.Fatalf("首次登记留痕不符: %+v", trail)
	}

	mustSet(t, s, SetInput{
		PurposeScope: ScopePractice, ModelVersion: "rasch-v1",
		CodeDigest: "digest-b", InputSnapshotID: "snap-2", GraphReleaseID: "rel-1",
		ActivatedBy: "ops-bob", ActivatedAt: base.Add(time.Hour),
	})
	trail, _ = s.SwitchTrail(ctx, nil, ScopePractice)
	if len(trail) != 2 || trail[1].From != "ctt-v1" || trail[1].To != "rasch-v1" ||
		trail[1].Who != "ops-bob" || !trail[1].At.Equal(base.Add(time.Hour)) {
		t.Fatalf("第二次切换留痕应为 ctt-v1→rasch-v1: %+v", trail)
	}

	cur, err := s.GetActive(ctx, nil, ScopePractice, nil)
	if err != nil {
		t.Fatal(err)
	}
	if cur.ModelVersion != "rasch-v1" || cur.RetiredAt != nil {
		t.Fatalf("当前活跃应为 rasch-v1 且未退役: %+v", cur)
	}
	if cur.CodeDigest != "digest-b" || cur.ActivatedBy != "ops-bob" || cur.RunID == run1.RunID {
		t.Fatalf("实证链字段错乱: %+v", cur)
	}

	// D6 时间回溯：base+30min 时正活跃的是 ctt-v1（ Rasch 尚未登记）.
	asOf := base.Add(30 * time.Minute)
	historic, err := s.GetActive(ctx, nil, ScopePractice, &asOf)
	if err != nil {
		t.Fatal(err)
	}
	if historic == nil || historic.ModelVersion != "ctt-v1" || historic.RetiredAt == nil {
		t.Fatalf("回溯版本错: %+v", historic)
	}
}

func TestIdempotentReplayHasNoSideEffect(t *testing.T) {
	s := NewMemoryStore()
	ctx := context.Background()
	first, switched1 := mustSet(t, s, SetInput{PurposeScope: ScopeDiagnosis, ModelVersion: "ctt-v1"})
	replay, switched2 := mustSet(t, s, SetInput{PurposeScope: ScopeDiagnosis, ModelVersion: "ctt-v1"})
	if !switched1 || switched2 {
		t.Fatalf("幂等重放应 switched=false: first=%v replay=%v", switched1, switched2)
	}
	if replay.RunID != first.RunID {
		t.Fatalf("幂等命中应返回现有指针而非新行: %q vs %q", first.RunID, replay.RunID)
	}
	trail, _ := s.SwitchTrail(ctx, nil, ScopeDiagnosis)
	if len(trail) != 1 {
		t.Fatalf("幂等重放不得追加留痕账: %d 条", len(trail))
	}
}

func TestTrailIsReadOnlyProjection(t *testing.T) {
	s := NewMemoryStore()
	mustSet(t, s, SetInput{PurposeScope: ScopeMeasurement, ModelVersion: "elo-v1"})
	ctx := context.Background()
	trail, _ := s.SwitchTrail(ctx, nil, ScopeMeasurement)
	trail[0].To = "篡改"
	again, _ := s.SwitchTrail(ctx, nil, ScopeMeasurement)
	if again[0].To != "elo-v1" {
		t.Fatal("留痕账被外部改写——append-only 契约破坏")
	}
}

// TestConcurrentSetDistinctVersionsExactlyOneActive 是验收 #2 的主用例：
// N 个 goroutine 以互不相同版本并发切换同一 scope（不带自定义时钟，含
// now() 竞争路径），在 -race 下必须满足——零异常泄漏；最终恰一条活跃指针；
// 留痕账与真实切换一一对应且 from/to 链条闭合.
func TestConcurrentSetDistinctVersionsExactlyOneActive(t *testing.T) {
	const n = 64
	s := NewMemoryStore()
	ctx := context.Background()

	results := make([]bool, n)
	var eg errgroup.Group
	for i := range n {
		eg.Go(func() error {
			v := fmt.Sprintf("ctt-v1-%02d", i)
			run, switched, err := s.SetActive(ctx, nil, SetInput{
				PurposeScope: ScopePractice,
				ModelVersion: v,
				CodeDigest:   "d",
				ActivatedBy:  fmt.Sprintf("actor-%02d", i),
			})
			if err != nil {
				return fmt.Errorf("并发切换意外失败: %w", err)
			}
			results[i] = switched
			if switched && run.ModelVersion != v {
				t.Errorf("switched=true 却返回他版 %q", run.ModelVersion)
			}
			return nil
		})
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	swCount := 0
	for _, switched := range results {
		if switched {
			swCount++
		}
	}
	trail, _ := s.SwitchTrail(ctx, nil, ScopePractice)
	if swCount != n {
		t.Fatalf("互异版本的每次串行切换都应是真实切换: %d/%d", swCount, n)
	}
	if len(trail) != n {
		t.Fatalf("留痕账应与切换一一对应: %d 条 vs %d 次", len(trail), n)
	}
	// from/to 链闭合：首条 From 为空，其后每条 From 接上一条 To.
	if trail[0].From != "" {
		t.Fatalf("首条留痕不应有前驱: %+v", trail[0])
	}
	for i := 1; i < n; i++ {
		if trail[i].From != trail[i-1].To {
			t.Fatalf("第 %d 条留痕链条断裂: %q 应为 %q", i, trail[i].From, trail[i-1].To)
		}
	}

	cur, err := s.GetActive(ctx, nil, ScopePractice, nil)
	if err != nil {
		t.Fatal(err)
	}
	// 账实一致：留痕账只在持锁临界区内按串行序追加，「最后落账者即最终活跃」，
	// 不取 goroutine 完成序（那是下标序，与串行序无关）.
	if cur == nil || cur.ModelVersion != trail[n-1].To || cur.RetiredAt != nil {
		t.Fatalf("终态活跃指针应为最后落账版本 %q: %+v", trail[n-1].To, cur)
	}
	// 同集合去重校验恰好一条：时间回溯至终点也只该有一行存活.
	end := time.Now().Add(time.Hour)
	liveAtEnd, err := s.GetActive(ctx, nil, ScopePractice, &end)
	if err != nil {
		t.Fatal(err)
	}
	if liveAtEnd == nil || liveAtEnd.ModelVersion != cur.ModelVersion {
		t.Fatalf("终点回溯活跃指针漂移: %+v vs %+v", liveAtEnd, cur)
	}
}

// TestConcurrentSameVersionIdempotentFlood：N 个并发请求全部指向同一版本时，
// 只允许一个真实切换落账，其余一律幂等命中（验收 #2 允许的「幂等成功」分支），
// 终态仍恰一条活跃.
func TestConcurrentSameVersionIdempotentFlood(t *testing.T) {
	const n = 32
	s := NewMemoryStore()
	ctx := context.Background()

	var switchedCount atomic.Int32
	var eg errgroup.Group
	for i := range n {
		eg.Go(func() error {
			_, switched, err := s.SetActive(ctx, nil, SetInput{
				PurposeScope: ScopeMeasurement,
				ModelVersion: "elo-v1",
				ActivatedBy:  fmt.Sprintf("actor-%02d", i),
			})
			if err != nil {
				return err
			}
			if switched {
				switchedCount.Add(1)
			}
			return nil
		})
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}
	if got := switchedCount.Load(); got != 1 {
		t.Fatalf("同版本并发只能有一次真实切换: %d 次", got)
	}
	trail, _ := s.SwitchTrail(ctx, nil, ScopeMeasurement)
	if len(trail) != 1 {
		t.Fatalf("留痕账只应有首次登记一条: %d 条", len(trail))
	}
	cur, _ := s.GetActive(ctx, nil, ScopeMeasurement, nil)
	if cur == nil || cur.ModelVersion != "elo-v1" {
		t.Fatalf("终态活跃指针丢失: %+v", cur)
	}
}

// TestConcurrentMixedScopesIndependentUniqueness：多场景并发各自收敛到
// 「每 scope 恰一条活跃」（D5 分场景隔离不被并发破坏）.
func TestConcurrentMixedScopesIndependentUniqueness(t *testing.T) {
	scopes := []PurposeScope{ScopePractice, ScopeDiagnosis, ScopeMeasurement}
	const perScope = 16
	s := NewMemoryStore()
	ctx := context.Background()

	var switchedTotal atomic.Int32
	var eg errgroup.Group
	for _, sc := range scopes {
		for i := range perScope {
			eg.Go(func() error {
				_, switched, err := s.SetActive(ctx, nil, SetInput{
					PurposeScope: sc,
					ModelVersion: fmt.Sprintf("%s-%02d", sc, i),
				})
				if err != nil {
					return err
				}
				if switched {
					switchedTotal.Add(1)
				}
				return nil
			})
		}
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	sum := 0
	for _, sc := range scopes {
		trail, _ := s.SwitchTrail(ctx, nil, sc)
		sum += len(trail)
		cur, err := s.GetActive(ctx, nil, sc, nil)
		if err != nil {
			t.Fatal(err)
		}
		if cur == nil || cur.RetiredAt != nil {
			t.Fatalf("scope %s 终态应有一条活跃: %+v", sc, cur)
		}
	}
	if int(switchedTotal.Load()) != sum {
		t.Fatalf("真实切换数(%d) 与三场景留痕总数(%d) 不一致", switchedTotal.Load(), sum)
	}
}

// TestReturnedRunIsDeepCopy 锁死「交出副本」契约：这是 -race 干净的结构前提.
func TestReturnedRunIsDeepCopy(t *testing.T) {
	s := NewMemoryStore()
	run, _ := mustSet(t, s, SetInput{PurposeScope: ScopePractice, ModelVersion: "ctt-v1"})
	run.ModelVersion = "已篡改"
	cur, _ := s.GetActive(context.Background(), nil, ScopePractice, nil)
	if cur.ModelVersion != "ctt-v1" {
		t.Fatal("返回的不是深拷贝，外部可经指针污染内部状态")
	}
}

func TestMapUniqueViolation(t *testing.T) {
	pgErr := &pgconn.PgError{Code: sqlStateUniqueViolation, Message: "duplicate key"}
	wrapped := mapUniqueViolation(fmt.Errorf("estimator/pg insert new: %w", pgErr))
	if !errors.Is(wrapped, ErrActiveConflict) {
		t.Fatalf("23505 应映射为 ErrActiveConflict: %v", wrapped)
	}
	// 原始驱动错误必须仍可被 errors.As 取回（吞掉真故障细节是反模式）.
	var recovered *pgconn.PgError
	if !errors.As(wrapped, &recovered) || recovered.Code != sqlStateUniqueViolation {
		t.Fatalf("原始 23505 证据链断裂: %v", wrapped)
	}
	other := mapUniqueViolation(&pgconn.PgError{Code: "42P01", Message: "no such table"})
	if errors.Is(other, ErrActiveConflict) {
		t.Fatal("非唯一冲突不得误报为活跃冲突")
	}
}

// TestBothImplementationsSatisfyContract 用接口双重锚定保证两实现的调用形态一致
// （内存与 PG 骨架都实现 ActivePointerStore，W6 接线时可无缝换装）.
func TestBothImplementationsSatisfyContract(t *testing.T) {
	var _ ActivePointerStore = (*MemoryStore)(nil)
	var _ ActivePointerStore = (*PGStore)(nil)
}
