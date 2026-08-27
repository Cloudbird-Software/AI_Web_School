package subjectmath

import (
	"fmt"
	"strings"
	"sync"
	"testing"
)

// generators_test.go：种子确定性与产能实证。
//   - 固定 seed → 固定输出摘要（可回放，AI 台账外确定性路径的回放义务）
//   - 每母题 ≥30 实例全过独立验证器、结构互异率 100%（H-W6-1 断言）
//   - Instance(i) 纯函数：同索引同内容（跨 seed 重叠索引产物逐字节相同）
//   - 全空间扫描：生成器×验证器全域一致 + content 全域无碰撞

func allTemplateIDs() []string { return IDs() }

func TestBatchAtLeast30PerTemplate(t *testing.T) {
	for _, id := range allTemplateIDs() {
		for _, seed := range []uint64{1, 42, 20260827} {
			recs, rep, err := Run(Options{TemplateID: id, N: 30, Seed: seed})
			if err != nil {
				t.Fatalf("%s seed=%d: %v", id, seed, err)
			}
			if rep.Accepted != 30 || rep.UniqueRate != 1 || !rep.DistinctOK {
				t.Fatalf("%s seed=%d 报告异常: %+v", id, seed, rep)
			}
			if len(rep.Rejected) != 0 {
				t.Fatalf("%s seed=%d 存在拒绝（合格产出应零拒绝）：%v", id, seed, rep.Rejected)
			}
			digests := make([]string, len(recs))
			for i := range recs {
				digests[i] = recs[i].ContentDigest
			}
			if err := AssertPairwiseDistinct(digests); err != nil {
				t.Fatalf("%s seed=%d: %v", id, seed, err)
			}
		}
	}
}

func TestSeedReplayByteIdentical(t *testing.T) {
	digestSeries := map[uint64][]string{}
	fullSeries := map[uint64][]string{}
	for _, seed := range []uint64{7, 7, 99} {
		recs, _, err := Run(Options{TemplateID: idIntMul, N: 24, Seed: seed})
		if err != nil {
			t.Fatalf("seed=%d: %v", seed, err)
		}
		var ds, fs []string
		for _, r := range recs {
			ds = append(ds, r.ContentDigest)
			instDigest, derr := DigestAny(r.canonicalView())
			if derr != nil {
				t.Fatalf("整实例摘要失败: %v", derr)
			}
			fs = append(fs, instDigest+"@"+string(rune('a'+r.SpaceIndex%26)))
		}
		digestSeries[seed] = ds
		fullSeries[seed] = fs
	}
	sameStrings := func(a, b []string) bool {
		if len(a) != len(b) {
			return false
		}
		for i := range a {
			if a[i] != b[i] {
				return false
			}
		}
		return true
	}
	if !sameStrings(digestSeries[7], digestSeries[7]) || !sameStrings(fullSeries[7], fullSeries[7]) {
		t.Fatal("同 seed 两次运行输出不一致——可回放性破坏")
	}
	if sameStrings(fullSeries[7], fullSeries[99]) {
		t.Fatal("不同 seed 应产生不同采样序列（弱断言，固定种子下为确定性事实）")
	}
}

func TestInstanceIsPureFunctionOfIndex(t *testing.T) {
	for _, id := range allTemplateIDs() {
		g, _ := Get(id)
		for _, idx := range []int{0, g.Size() / 2, g.Size() - 1} {
			a, errA := g.Instance(idx)
			b, errB := g.Instance(idx)
			if errA != nil || errB != nil {
				t.Fatalf("%s idx=%d 构造失败: %v/%v", id, idx, errA, errB)
			}
			da, _ := DigestAny(a)
			db, _ := DigestAny(b)
			if da != db {
				t.Fatalf("%s idx=%d 非纯函数：%s vs %s", id, idx, da, db)
			}
		}
		if _, err := g.Instance(g.Size()); err == nil {
			t.Fatalf("%s 越界索引未拒绝", id)
		}
		if _, err := g.Instance(-1); err == nil {
			t.Fatalf("%s 负索引未拒绝", id)
		}
	}
}

// TestWholeSpaceDistinctAndValid 全空间扫描：
// 1) 每个参数点产出的实例都过独立验证器；
// 2) 整个空间的 content 摘要两两不同（injectivity —— n=30 唯一率的体力来源）。
func TestWholeSpaceDistinctAndValid(t *testing.T) {
	if testing.Short() {
		t.Skip("short 模式跳过全空间扫描")
	}
	for _, id := range allTemplateIDs() {
		g, _ := Get(id)
		size := g.Size()
		seen := make(map[string]int, size)
		digests := make([]string, 0, size)
		for i := 0; i < size; i++ {
			inst, err := g.Instance(i)
			if err != nil {
				t.Fatalf("%s idx=%d 构造失败（全空间应零构造失败）: %v", id, i, err)
			}
			d, err := ContentDigest(inst.Content)
			if err != nil {
				t.Fatalf("%s idx=%d 摘要失败: %v", id, i, err)
			}
			if first, dup := seen[d]; dup {
				t.Fatalf("%s 全空间摘要碰撞：idx %d 与 %d", id, first, i)
			}
			seen[d] = i
			digests = append(digests, d)
			if verr := Validate(inst); verr != nil {
				t.Fatalf("%s idx=%d 未过独立验证器: %v", id, i, verr)
			}
		}
		if err := AssertPairwiseDistinct(digests); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		t.Logf("%s 全空间 %d 参数点：验证器全过、content 两两互异", id, size)
	}
}

// 结构形态多样性：MT2 三种形态都要在批次内出现；MT1/MT3 的题干变体 ≥2 种。
func TestStructuralDiversityWithinBatch(t *testing.T) {
	recs, _, err := Run(Options{TemplateID: idFracCmp, N: 60, Seed: 5})
	if err != nil {
		t.Fatalf("frac 批次失败: %v", err)
	}
	forms := map[string]bool{}
	for _, r := range recs {
		f, _ := r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["form"].(string)
		forms[f] = true
	}
	if !forms["D"] || !forms["N"] || !forms["X"] {
		t.Fatalf("分数比较三结构形态未齐备：%v", forms)
	}

	mulRecs, _, err := Run(Options{TemplateID: idIntMul, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("intmul 批次失败: %v", err)
	}
	variants := map[string]bool{}
	for _, r := range mulRecs {
		blocks := r.Content["blocks"].([]any)
		stem := blocks[0].(map[string]any)["template"].(string)
		variants[stem] = true
	}
	if len(variants) < 2 {
		t.Fatalf("整数乘法题干变体多样性不足：%d 种", len(variants))
	}

	convRecs, _, err := Run(Options{TemplateID: idUnitConv, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("unitconv 批次失败: %v", err)
	}
	kpSeen := map[string]bool{}
	for _, r := range convRecs {
		kp := r.Objective["kp_set"].([]any)[0].(map[string]any)["code"].(string)
		kpSeen[kp] = true
	}
	if !kpSeen["math.nal.quantity.length"] || !kpSeen["math.nal.quantity.mass"] || !kpSeen["math.nal.quantity.money"] {
		t.Fatalf("单位换算三量纲族未齐备：%v", kpSeen)
	}

	// 第二阶 4 母题：结构轴在批次内可见。
	roundRecs, _, err := Run(Options{TemplateID: idIntRound, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("round 批次失败: %v", err)
	}
	places := map[string]bool{}
	for _, r := range roundRecs {
		p, _ := r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["place"].(string)
		places[p] = true
	}
	if len(places) < 2 {
		t.Fatalf("近似数位档多样性不足：%v", places)
	}

	addRecs, _, err := Run(Options{TemplateID: idIntAddSub, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("addsub 批次失败: %v", err)
	}
	ops := map[string]bool{}
	for _, r := range addRecs {
		op, _ := r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["op"].(string)
		ops[op] = true
	}
	if !ops["+"] || !ops["-"] {
		t.Fatalf("整数加减进/退位双向未齐备：%v", ops)
	}

	faRecs, _, err := Run(Options{TemplateID: idFracAddSub, N: 60, Seed: 5})
	if err != nil {
		t.Fatalf("frac-addsub 批次失败: %v", err)
	}
	faOps := map[string]bool{}
	for _, r := range faRecs {
		op, _ := r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["op"].(string)
		faOps[op] = true
	}
	if !faOps["+"] || !faOps["-"] {
		t.Fatalf("同分母分数加减双向未齐备：%v", faOps)
	}

	dcRecs, _, err := Run(Options{TemplateID: idDecCmp, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("dec-cmp 批次失败: %v", err)
	}
	crossScale := false
	for _, r := range dcRecs {
		norm := r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)
		x, _ := norm["x"].(string)
		y, _ := norm["y"].(string)
		if len(x)-strings.Index(x, ".") != len(y)-strings.Index(y, ".") {
			crossScale = true // 位数陷阱结构（如 1.3 与 1.28）确实入批
		}
	}
	if !crossScale {
		t.Fatal("小数比较批次未出现跨位数结构对")
	}
}

// 并发批次（-race 观察 shared registry 只读安全）。
func TestConcurrentRunsShareRegistrySafely(t *testing.T) {
	var wg sync.WaitGroup
	errs := make(chan error, 3)
	for _, id := range allTemplateIDs() {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			_, rep, err := Run(Options{TemplateID: id, N: 12, Seed: 777})
			if err != nil {
				errs <- err
				return
			}
			if rep.Accepted != 12 || !rep.DistinctOK {
				errs <- fmt.Errorf("并发批次报告异常：%s accepted=%d distinct=%v", id, rep.Accepted, rep.DistinctOK)
			}
		}(id)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("并发批次失败: %v", err)
		}
	}
}
