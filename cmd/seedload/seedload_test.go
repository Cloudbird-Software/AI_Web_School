// seedload_test.go cmd/seedload 的本地可验证语义（无 PG 的面）：
// 种子清单收集（叠加/去重/排序/缺失失败）、dry-run 装饰器（抑制写入 +
// 账实交叉校验）、DSN fail-closed、汇总输出（幂等重跑 = skip 统计的证据面
// 经 MemoryGraph 全链实证：首装 added、重装全 skip、底层零重复行）。
package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/knowledge"
)

// ── 种子清单收集 ────────────────────────────────────────────────────────

func TestCollectSeedsDirSortedAndDedup(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"b.yaml", "a.yaml", "c.txt", "note.md"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o644); err != nil {
			t.Fatalf("写 %s: %v", name, err)
		}
	}
	sub := filepath.Join(dir, "sub")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	got, err := collectSeeds(nil, dir)
	if err != nil {
		t.Fatalf("collectSeeds: %v", err)
	}
	want := []string{filepath.Join(dir, "a.yaml"), filepath.Join(dir, "b.yaml")}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("目录扫描应只收 *.yaml 且按名排序: %v", got)
	}
}

func TestCollectSeedsFilePlusDirDedup(t *testing.T) {
	dir := t.TempDir()
	a := filepath.Join(dir, "a.yaml")
	if err := os.WriteFile(a, []byte("x"), 0o644); err != nil {
		t.Fatalf("写 a.yaml: %v", err)
	}
	b := filepath.Join(dir, "b.yaml")
	if err := os.WriteFile(b, []byte("x"), 0o644); err != nil {
		t.Fatalf("写 b.yaml: %v", err)
	}

	got, err := collectSeeds(multiFlag{a, b}, dir)
	if err != nil {
		t.Fatalf("collectSeeds: %v", err)
	}
	// -file 在前且与目录扫描去重：a/b 各出现一次，共 2 条
	if len(got) != 2 || got[0] != filepath.Clean(a) || got[1] != filepath.Clean(b) {
		t.Fatalf("叠加去重结果不符: %v", got)
	}
}

func TestCollectSeedsMissingDirWithFilesTolerated(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "only.yaml")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatalf("写 only.yaml: %v", err)
	}
	got, err := collectSeeds(multiFlag{f}, filepath.Join(dir, "nope"))
	if err != nil {
		t.Fatalf("给了 -file 时缺失目录应容忍: %v", err)
	}
	if len(got) != 1 || got[0] != filepath.Clean(f) {
		t.Fatalf("应只含显式文件: %v", got)
	}
}

func TestCollectSeedsEmptyFails(t *testing.T) {
	if _, err := collectSeeds(nil, filepath.Join(t.TempDir(), "nope")); err == nil {
		t.Fatal("无文件可装应报错（禁止静默空跑）")
	}
}

func TestCollectSeedsMissingFileFails(t *testing.T) {
	if _, err := collectSeeds(multiFlag{filepath.Join(t.TempDir(), "nope.yaml")}, ""); err == nil {
		t.Fatal("-file 不可达应报错")
	}
}

// ── dry-run 装饰器：抑制写入 + 记账自证 ────────────────────────────────

func TestDryRunSinkSuppressesWritesButCounts(t *testing.T) {
	g := knowledge.NewMemoryGraph()
	dr := newDryRunSink(g)

	stats, err := knowledge.Load(testSeedPath(t), dr, "kp")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if stats.NodesAdded != 2 || stats.EdgesAdded != 1 || stats.RelationTypesAdded != 1 {
		t.Fatalf("added 统计不符: %+v", stats)
	}
	// 演练不动账：底层图零行
	if g.NodeCount() != 0 || g.EdgeCount() != 0 {
		t.Fatalf("dry-run 不得写库：nodes=%d edges=%d", g.NodeCount(), g.EdgeCount())
	}
}

func TestDryRunSinkVerifyAndReset(t *testing.T) {
	dr := newDryRunSink(knowledge.NewMemoryGraph())
	if _, err := knowledge.Load(testSeedPath(t), dr, "kp"); err != nil {
		t.Fatalf("Load: %v", err)
	}
	stats := &knowledge.SeedLoadStats{RelationTypesAdded: 1, NodesAdded: 2, EdgesAdded: 1}
	if err := dr.verifyAndReset(stats); err != nil {
		t.Fatalf("账实应一致: %v", err)
	}
	if dr.relTypes != 0 || dr.nodes != 0 || dr.edges != 0 {
		t.Fatalf("verify 后应清零: %+v", dr)
	}
	// 篡改统计 → 必须报不一致（漏记写入即 fail loud）
	if _, err := knowledge.Load(testSeedPath(t), dr, "kp"); err != nil {
		t.Fatalf("Load#2: %v", err)
	}
	if err := dr.verifyAndReset(&knowledge.SeedLoadStats{RelationTypesAdded: 1, NodesAdded: 9, EdgesAdded: 1}); err == nil {
		t.Fatal("统计与抑制写入不一致必须报错")
	}
}

// ── run：DSN fail-closed + 幂等重跑汇总 ────────────────────────────────

func TestRunFailClosedWhenDSNMissing(t *testing.T) {
	var out bytes.Buffer
	err := run([]string{"-file", testSeedPath(t)}, func(string) string { return "" }, &out)
	if err == nil || !strings.Contains(err.Error(), "SCHOOL_DATABASE_URL") {
		t.Fatalf("DSN 缺失必须 fail-closed 并点名环境变量: %v", err)
	}
}

// noEnv 恒空环境（fail-closed 用例）.
func noEnv(string) string { return "" }

func TestRunIdempotentRerunSkipStats(t *testing.T) {
	// 以 MemoryGraph 模拟「同一库重跑两遍」：首装 added、重装全 skip，
	// 底层零重复行——幂等约定的 CLI 侧证据（PG 侧行为由库端唯一约束兜底，
	// 见 core/knowledge pg_sink_test 与 0006 DDL）。
	g := knowledge.NewMemoryGraph()
	seed := testSeedPath(t)

	first, err := knowledge.Load(seed, g, "kp")
	if err != nil {
		t.Fatalf("首装: %v", err)
	}
	second, err := knowledge.Load(seed, g, "kp")
	if err != nil {
		t.Fatalf("重装: %v", err)
	}
	if first.NodesAdded != 2 || second.NodesAdded != 0 || second.NodesSkipped != 2 {
		t.Fatalf("节点重装应全 skip: 首=%+v 重=%+v", first, second)
	}
	if first.EdgesAdded != 1 || second.EdgesAdded != 0 || second.EdgesSkipped != 1 {
		t.Fatalf("边重装应全 skip: 首=%+v 重=%+v", first, second)
	}
	if g.NodeCount() != 2 || g.EdgeCount() != 1 {
		t.Fatalf("重装后不得有重复行: nodes=%d edges=%d", g.NodeCount(), g.EdgeCount())
	}

	// 汇总输出面：skip 统计可读（幂等证据落到 stdout 口径）
	var out bytes.Buffer
	tot := &seedTotals{}
	tot.merge(second)
	tot.print(&out, "apply")
	text := out.String()
	if !strings.Contains(text, "skipped=2") || !strings.Contains(text, "added=0") {
		t.Fatalf("汇总应呈现重跑 skip 口径: %s", text)
	}
	if !strings.Contains(text, "missing-node=0") {
		t.Fatalf("汇总应呈现 missing-node 计数: %s", text)
	}
}

func TestSeedTotalsMergeAndDryRunLabel(t *testing.T) {
	tot := &seedTotals{}
	tot.merge(&knowledge.SeedLoadStats{RelationTypesAdded: 1, RelationTypesSkipped: 2, NodesAdded: 3, NodesSkipped: 4, EdgesAdded: 5, EdgesSkipped: 6, EdgesMissingNode: 7})
	tot.merge(&knowledge.SeedLoadStats{NodesAdded: 1})
	if tot.files != 2 || tot.relAdded != 1 || tot.relSkipped != 2 || tot.nodeAdded != 4 || tot.nodeSkip != 4 || tot.edgeAdded != 5 || tot.edgeSkipped != 6 || tot.missing != 7 {
		t.Fatalf("merge 累计不符: %+v", tot)
	}

	var out bytes.Buffer
	tot.print(&out, "dry-run")
	if !strings.Contains(out.String(), "would-add=4") {
		t.Fatalf("dry-run 汇总应用 would-add 标签: %s", out.String())
	}
}

// testSeedPath 落一个最小双节点+单边种子文件（本套件共用）.
func testSeedPath(t *testing.T) string {
	t.Helper()
	const yaml = `version: "1.0"
pack_id: subject-math
graph_release_id: "2026.1.seedload-test"
relation_types:
  - rel_type: prerequisite
    directed: true
    transitive: true
    acyclic: true
    symmetric: false
nodes:
  - {code: math.t.a, title: 甲}
  - {code: math.t.b, title: 乙}
edges:
  - {src: math.t.a, dst: math.t.b, rel_type: prerequisite}
`
	path := filepath.Join(t.TempDir(), "seed.yaml")
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatalf("写种子: %v", err)
	}
	return path
}
