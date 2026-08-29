// closure_test.go 知识域 Go 移植的验收测试（闭包计算 + 种子装载）。
//
// 测试策略：纯函数内核 + MemoryGraph 查询面，无 DB。每算法至少 1 正例
// 1 负例；闭包行（depth/path_count）与冻结实现递归 CTE 语义逐条对齐——
// 先修链展开、多路径计数、非传递直连、环路防护、时间窗过滤、幂等；
// 种子装载以冻结实现 parse_seed_file 对 content/seeds/math_kp_3-4.yaml 的
// 采样（nodes=80 / edges=76 / relation_types=3）为地面真值。
package knowledge

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// ────────────────────────────────────────────────────────────────────
// 构造辅助
// ────────────────────────────────────────────────────────────────────

func relTransitive(rel string) RelationType {
	return RelationType{RelType: rel, Directed: true, Transitive: true, Acyclic: true, Symmetric: false}
}

func relSymmetric(rel string) RelationType {
	return RelationType{RelType: rel, Directed: false, Transitive: false, Acyclic: false, Symmetric: true}
}

func edge(src, dst, rel string) KpEdge {
	return KpEdge{SrcNodeID: src, DstNodeID: dst, RelType: rel}
}

func timeP(t time.Time) *time.Time { return &t }

// closureKeyOf 闭包行检索键 "src→dst@depth".
func closureKeyOf(r ClosureRow) string {
	return fmt.Sprintf("%s→%s@%d", r.SrcNodeID, r.DstNodeID, r.Depth)
}

func rowBy(t *testing.T, rows []ClosureRow, key string) ClosureRow {
	t.Helper()
	for _, r := range rows {
		if closureKeyOf(r) == key {
			return r
		}
	}
	t.Fatalf("闭包缺少 %s：实际 %v", key, rows)
	return ClosureRow{}
}

// seedPrerequisiteChain 种 A→B→C 先修链 + release（对齐冻结测试 _seed_prerequisite_chain）.
func seedPrerequisiteChain(t *testing.T) (*MemoryGraph, string) {
	t.Helper()
	g := NewMemoryGraph()
	if err := g.AddRelationType(relTransitive("prerequisite")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	if err := g.AddEdge(edge("A", "B", "prerequisite")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddEdge(edge("B", "C", "prerequisite")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddRelease(GraphRelease{ReleaseID: "2026.1.test", Status: "active"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	return g, "2026.1.test"
}

// ────────────────────────────────────────────────────────────────────
// 一、闭包计算（closure.py）
// ────────────────────────────────────────────────────────────────────

func TestComputeClosurePrerequisiteChain(t *testing.T) {
	g, grid := seedPrerequisiteChain(t)
	stats, err := ComputeClosure(grid, g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	if stats.GraphReleaseID != grid {
		t.Fatalf("release 留档错: %+v", stats)
	}
	if stats.ClosureRows < 3 {
		t.Fatalf("应有 ≥3 条闭包（A→B,B→C,A→C），实际 %d", stats.ClosureRows)
	}
	if !reflect.DeepEqual(stats.TransitiveRelTypes, []string{"prerequisite"}) ||
		len(stats.NonTransitiveRelTypes) != 0 {
		t.Fatalf("transitive 分类错: %+v", stats)
	}
	rows := g.ClosureOf(grid)
	// 验收 #4 场景 1：A→C depth=2 path_count=1（先修链传递展开）
	ac := rowBy(t, rows, "A→C@2")
	if ac.Depth != 2 || ac.PathCount != 1 {
		t.Fatalf("A→C 应 depth=2 path_count=1: %+v", ac)
	}
	ab := rowBy(t, rows, "A→B@1")
	if ab.Depth != 1 {
		t.Fatalf("直接边 depth=1: %+v", ab)
	}
	// 多路径计数：菱形 A→B→D、A→C→D ⇒ A→D depth=2 path_count=2
	g2 := NewMemoryGraph()
	if err := g2.AddRelationType(relTransitive("prerequisite")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	for _, e := range []KpEdge{edge("A", "B", "prerequisite"), edge("A", "C", "prerequisite"), edge("B", "D", "prerequisite"), edge("C", "D", "prerequisite")} {
		if err := g2.AddEdge(e); err != nil {
			t.Fatalf("AddEdge: %v", err)
		}
	}
	if err := g2.AddRelease(GraphRelease{ReleaseID: "diamond"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	if _, err := ComputeClosure("diamond", g2, g2, MaxClosureDepth); err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	ad := rowBy(t, g2.ClosureOf("diamond"), "A→D@2")
	if ad.PathCount != 2 {
		t.Fatalf("菱形 A→D path_count 应为 2: %+v", ad)
	}
}

func TestComputeClosureConfusableNonTransitive(t *testing.T) {
	g := NewMemoryGraph()
	if err := g.AddRelationType(relSymmetric("confusable")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	if err := g.AddEdge(edge("A", "B", "confusable")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	} // symmetric 语义由应用层解释，DB 只存一条
	if err := g.AddRelease(GraphRelease{ReleaseID: "2026.1.confusable", Status: "active"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	stats, err := ComputeClosure("2026.1.confusable", g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	// 验收 #4 场景 2：非传递仅 depth=1 直接边，无 A→C/A→A 展开
	if stats.ClosureRows != 1 {
		t.Fatalf("confusable 非传递应仅 1 条直接边，实际 %d", stats.ClosureRows)
	}
	if !reflect.DeepEqual(stats.NonTransitiveRelTypes, []string{"confusable"}) {
		t.Fatalf("non_transitive 分类错: %+v", stats)
	}
	for _, r := range g.ClosureOf("2026.1.confusable") {
		if r.SrcNodeID == r.DstNodeID {
			t.Fatalf("闭包不应有自环: %+v", r)
		}
		if r.Depth != 1 {
			t.Fatalf("非传递不应展开多跳: %+v", r)
		}
	}
}

func TestComputeClosureCyclesAreGuarded(t *testing.T) {
	// acyclic 被违反（A→B→A）时 visited 防护终止递归，仅产出直接边
	g := NewMemoryGraph()
	if err := g.AddRelationType(relTransitive("prerequisite")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	if err := g.AddEdge(edge("A", "B", "prerequisite")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddEdge(edge("B", "A", "prerequisite")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddRelease(GraphRelease{ReleaseID: "cycle"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	stats, err := ComputeClosure("cycle", g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("环路防护应终止而非报错: %v", err)
	}
	if stats.ClosureRows != 2 {
		t.Fatalf("环图应仅 2 条直接边，实际 %d: %+v", stats.ClosureRows, g.ClosureOf("cycle"))
	}
}

func TestComputeClosureMaxDepthCapsExpansion(t *testing.T) {
	g := NewMemoryGraph()
	if err := g.AddRelationType(relTransitive("prerequisite")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	for _, e := range []KpEdge{edge("A", "B", "prerequisite"), edge("B", "C", "prerequisite"), edge("C", "D", "prerequisite")} {
		if err := g.AddEdge(e); err != nil {
			t.Fatalf("AddEdge: %v", err)
		}
	}
	if err := g.AddRelease(GraphRelease{ReleaseID: "chain4"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	// max_depth=2：展开到 depth=2（A→C），A→D（depth=3）不产出
	if _, err := ComputeClosure("chain4", g, g, 2); err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	rows := g.ClosureOf("chain4")
	rowBy(t, rows, "A→C@2")
	for _, r := range rows {
		if r.Depth > 2 {
			t.Fatalf("深度上限失效: %+v", r)
		}
	}
}

func TestComputeClosureAsOfTimeWindowFiltering(t *testing.T) {
	// 版本切换（验收 #4 场景 3）：gr1 实时快照含全链；gr2 冻结在过去，
	// B→C 在未来生效 → gr2 视角只有 A→B。
	past := time.Now().Add(-10 * 24 * time.Hour)
	future := time.Now().Add(10 * 24 * time.Hour)
	g := NewMemoryGraph()
	if err := g.AddRelationType(relTransitive("prerequisite")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	bc := edge("B", "C", "prerequisite")
	bc.ValidFrom = timeP(future)
	if err := g.AddEdge(edge("A", "B", "prerequisite")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddEdge(bc); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g.AddRelease(GraphRelease{ReleaseID: "gr1", Status: "active"}); err != nil { // 无 valid_from = 实时快照
		t.Fatalf("AddRelease: %v", err)
	}
	if err := g.AddRelease(GraphRelease{ReleaseID: "gr2", Status: "frozen", ValidFrom: timeP(past)}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	r1, err := ComputeClosure("gr1", g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure gr1: %v", err)
	}
	if r1.ClosureRows != 3 {
		t.Fatalf("gr1 实时快照应 3 条（含 A→C）: %+v", r1)
	}
	rowBy(t, g.ClosureOf("gr1"), "A→C@2")
	r2, err := ComputeClosure("gr2", g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure gr2: %v", err)
	}
	if r2.ClosureRows != 1 || r2.AsOf == nil || !r2.AsOf.Equal(past) {
		t.Fatalf("gr2 冻结快照应仅 A→B 且 as_of=valid_from: %+v", r2)
	}
	// 版本切换互不影响：gr1 仍有 A→C
	rowBy(t, g.ClosureOf("gr1"), "A→C@2")
	for _, r := range g.ClosureOf("gr2") {
		if closureKeyOf(r) == "A→C@2" {
			t.Fatalf("gr2 不应有 A→C 闭包条目")
		}
	}
}

func TestComputeClosureIdempotent(t *testing.T) {
	g, grid := seedPrerequisiteChain(t)
	r1, err := ComputeClosure(grid, g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	r2, err := ComputeClosure(grid, g, g, MaxClosureDepth)
	if err != nil {
		t.Fatalf("ComputeClosure: %v", err)
	}
	if r1.ClosureRows != r2.ClosureRows || len(g.ClosureOf(grid)) != r1.ClosureRows {
		t.Fatalf("幂等性失败: %+v vs %+v", r1, r2)
	}
}

func TestComputeClosureErrorPaths(t *testing.T) {
	g, grid := seedPrerequisiteChain(t)
	// release 不存在
	if _, err := ComputeClosure("nonexistent-release", g, g, MaxClosureDepth); err == nil || !strings.Contains(err.Error(), "不存在") {
		t.Fatalf("不存在的 release 应报错: %v", err)
	}
	// max_depth < 1
	if _, err := ComputeClosure(grid, g, g, 0); err == nil || !strings.Contains(err.Error(), "max_depth") {
		t.Fatalf("max_depth<1 应报错: %v", err)
	}
	// 自环边（ck_kpc_no_self_loop）
	g2 := NewMemoryGraph()
	if err := g2.AddRelationType(relSymmetric("confusable")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	if err := g2.AddEdge(edge("A", "A", "confusable")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g2.AddRelease(GraphRelease{ReleaseID: "selfloop"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	if _, err := ComputeClosure("selfloop", g2, g2, MaxClosureDepth); err == nil || !strings.Contains(err.Error(), "自环") {
		t.Fatalf("自环应报 ck 错误: %v", err)
	}
	// 并行边（uq_kp_edge_src_dst_rel）
	g3 := NewMemoryGraph()
	if err := g3.AddRelationType(relSymmetric("confusable")); err != nil {
		t.Fatalf("AddRelationType: %v", err)
	}
	if err := g3.AddEdge(edge("A", "B", "confusable")); err != nil {
		t.Fatalf("AddEdge: %v", err)
	}
	if err := g3.AddRelease(GraphRelease{ReleaseID: "parallel"}); err != nil {
		t.Fatalf("AddRelease: %v", err)
	}
	// MemoryGraph 唯一约束兜底：重复边在 AddEdge 即拒绝（uq_kp_edge_src_dst_rel）
	if err := g3.AddEdge(edge("A", "B", "confusable")); err == nil || !strings.Contains(err.Error(), "uq_kp_edge_src_dst_rel") {
		t.Fatalf("重复边应触发唯一约束: %v", err)
	}
	// 非传递并行边守卫（纯函数面）：stub 视图绕过 store 去重注入两条同键边
	if _, err := ComputeClosure("parallel", parallelEdgeView{}, NewMemoryGraph(), MaxClosureDepth); err == nil || !strings.Contains(err.Error(), "uq_kp_edge_src_dst_rel") {
		t.Fatalf("非传递并行边应显式报错: %v", err)
	}
	// kp_closure 唯一约束兜底：手工构造重复闭包行
	if err := NewMemoryGraph().ReplaceClosure("parallel", []ClosureRow{
		{GraphReleaseID: "parallel", SrcNodeID: "A", DstNodeID: "B", RelType: "confusable", Depth: 1, PathCount: 1},
		{GraphReleaseID: "parallel", SrcNodeID: "A", DstNodeID: "B", RelType: "confusable", Depth: 1, PathCount: 1},
	}); err == nil || !strings.Contains(err.Error(), "uq_kpc") {
		t.Fatalf("kp_closure 唯一约束应兜底: %v", err)
	}
}

// ────────────────────────────────────────────────────────────────────
// 二、种子装载（seed_loader.py；地面真值 = 冻结实现对真实种子文件采样）
// ────────────────────────────────────────────────────────────────────

const repoSeedPath = ".." + string(filepath.Separator) + ".." + string(filepath.Separator) + "content" + string(filepath.Separator) + "seeds" + string(filepath.Separator) + "math_kp_3-4.yaml"

func TestLoadRealSeedFileGroundTruth(t *testing.T) {
	if _, err := os.Stat(repoSeedPath); err != nil {
		t.Skipf("种子文件不可达（非仓库布局）: %v", err)
	}
	g := NewMemoryGraph()
	stats, err := Load(repoSeedPath, g, "kp")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	// 地面真值：冻结实现 parse_seed_file 采样（nodes=80 / edges=76 / relation_types=3）
	if stats.NodesAdded != 80 || stats.NodesSkipped != 0 {
		t.Fatalf("节点装载与冻结实现采样不符: %+v", stats)
	}
	if stats.EdgesAdded != 76 || stats.EdgesSkipped != 0 || stats.EdgesMissingNode != 0 {
		t.Fatalf("边装载与冻结实现采样不符: %+v", stats)
	}
	if stats.RelationTypesAdded != 3 || stats.RelationTypesSkipped != 0 {
		t.Fatalf("关系类型装载与冻结实现采样不符: %+v", stats)
	}
	if stats.PackID != "subject-math" || stats.GraphReleaseID != "2026.1.math-3-4" {
		t.Fatalf("pack/release 留档错: %+v", stats)
	}
	if g.NodeCount() != 80 || g.EdgeCount() != 76 {
		t.Fatalf("图面落账数错: nodes=%d edges=%d", g.NodeCount(), g.EdgeCount())
	}
	// 全部节点 dimension=kp（验收 #3）
	for code, id := range g.NodeIDsByCode("subject-math", "kp") {
		if id == "" || code == "" {
			t.Fatalf("node id 映射缺空值")
		}
	}
	if len(g.NodeIDsByCode("subject-math", "kp")) != 80 {
		t.Fatalf("dimension=kp 节点数应 80")
	}

	// 幂等约定（验收 #2）：同一文件重复 Load 全部 skip，不抛错
	stats2, err := Load(repoSeedPath, g, "kp")
	if err != nil {
		t.Fatalf("重复 Load: %v", err)
	}
	if stats2.NodesAdded != 0 || stats2.NodesSkipped != 80 ||
		stats2.EdgesAdded != 0 || stats2.EdgesSkipped != 76 ||
		stats2.RelationTypesAdded != 0 || stats2.RelationTypesSkipped != 3 ||
		stats2.EdgesMissingNode != 0 {
		t.Fatalf("幂等重载统计错: %+v", stats2)
	}
	if g.NodeCount() != 80 || g.EdgeCount() != 76 {
		t.Fatalf("幂等重载后图面不应增长")
	}
}

const inlineSeedYAML = `version: "1.0"
pack_id: subject-x
graph_release_id: "2026.1.x"
relation_types:
  - rel_type: prerequisite
    transitive: true
nodes:
  - {code: x.a, title: A}
  - {code: x.b, title: B}
edges:
  - {src: x.a, dst: x.b, rel_type: prerequisite}
  - {src: x.a, dst: x.ghost, rel_type: prerequisite}
`

func loadInline(t *testing.T, yamlText string) (*MemoryGraph, *SeedLoadStats, error) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "seed.yaml")
	if err := os.WriteFile(path, []byte(yamlText), 0o600); err != nil {
		t.Fatalf("写种子文件: %v", err)
	}
	g := NewMemoryGraph()
	stats, err := Load(path, g, "kp")
	return g, stats, err
}

func TestLoadSeedIdempotencyAndMissingNode(t *testing.T) {
	g, stats, err := loadInline(t, inlineSeedYAML)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	// 缺失节点计账不抛错
	if stats.EdgesMissingNode != 1 || stats.EdgesAdded != 1 {
		t.Fatalf("缺失节点应计账: %+v", stats)
	}
	// 默认值：directed=true / acyclic=true / transitive 显式 true / gradeband=M
	relTypes, err := g.RelationTypes()
	if err != nil || len(relTypes) != 1 {
		t.Fatalf("关系类型装载错: %v", err)
	}
	rt := relTypes[0]
	if !rt.Directed || !rt.Acyclic || !rt.Transitive || rt.Symmetric {
		t.Fatalf("关系类型默认值与冻结实现不符: %+v", rt)
	}
	if g.NodeCount() != 2 {
		t.Fatalf("节点数错: %d", g.NodeCount())
	}
	// node_id 生成（kp_ 前缀 + 唯一后缀）
	ids := g.NodeIDsByCode("subject-x", "kp")
	if !strings.HasPrefix(ids["x.a"], "kp_") || ids["x.a"] == ids["x.b"] {
		t.Fatalf("node_id 生成错: %v", ids)
	}
	// 边按 code 解析为 node_id
	if g.EdgeCount() != 1 {
		t.Fatalf("边数错: %d", g.EdgeCount())
	}
}

func TestLoadSeedSchemaValidation(t *testing.T) {
	// 未知字段拒绝（pydantic extra="forbid"）
	unknownField := inlineSeedYAML + "bogus_key: 1\n"
	if _, _, err := loadInline(t, unknownField); err == nil {
		t.Fatalf("未知字段应被拒绝")
	}
	// 缺必填字段
	missingNodes := "version: \"1.0\"\npack_id: subject-x\ngraph_release_id: \"r\"\n"
	if _, _, err := loadInline(t, missingNodes); err == nil {
		t.Fatalf("缺 nodes 应报错")
	}
	// 缺 src/dst 的边
	badEdge := "version: \"1.0\"\npack_id: p\ngraph_release_id: r\nnodes:\n  - {code: a, title: A}\nedges:\n  - {src: a, rel_type: prerequisite}\n"
	if _, _, err := loadInline(t, badEdge); err == nil {
		t.Fatalf("边缺 dst 应报错")
	}
	// 不存在的文件
	if _, err := Load(filepath.Join(t.TempDir(), "nope.yaml"), NewMemoryGraph(), "kp"); err == nil {
		t.Fatalf("文件不存在应报错")
	}
}

func TestParseSeedFileDefaultsGradebandM(t *testing.T) {
	if _, err := os.Stat(repoSeedPath); err != nil {
		t.Skipf("种子文件不可达: %v", err)
	}
	sf, err := ParseSeedFile(repoSeedPath)
	if err != nil {
		t.Fatalf("ParseSeedFile: %v", err)
	}
	if sf.Version != "1.0" || sf.PackID != "subject-math" || sf.GraphReleaseID != "2026.1.math-3-4" {
		t.Fatalf("种子头字段错: %+v", sf)
	}
	if len(sf.Nodes) != 80 || len(sf.Edges) != 76 || len(sf.RelationTypes) != 3 {
		t.Fatalf("种子规模与冻结实现采样不符: %d/%d/%d", len(sf.Nodes), len(sf.Edges), len(sf.RelationTypes))
	}
	// gradeband 缺省 "M"
	for _, n := range sf.Nodes {
		if n.Gradeband == nil || *n.Gradeband != "M" {
			t.Fatalf("节点 gradeband 缺省应为 M: %+v", n)
		}
		break
	}
}

func TestGraphViewReleaseContract(t *testing.T) {
	g, grid := seedPrerequisiteChain(t)
	rel, err := g.Release(grid)
	if err != nil || rel == nil || rel.ReleaseID != grid || rel.Status != "active" {
		t.Fatalf("Release 读取错: %v %+v", err, rel)
	}
	missing, err := g.Release("nope")
	if err != nil || missing != nil {
		t.Fatalf("不存在的 release 应返回 (nil, nil): %v %v", missing, err)
	}
	// AddRelease 主键重复
	if err := g.AddRelease(GraphRelease{ReleaseID: grid}); err == nil {
		t.Fatalf("release 重复应报错")
	}
	// ComputeClosure 走 release 不存在路径
	if _, err := ComputeClosure("nope", g, g, MaxClosureDepth); err == nil || !errors.Is(err, err) {
		t.Fatalf("release 缺失应报错: %v", err)
	}
}

func TestReplaceClosureCheckConstraints(t *testing.T) {
	g := NewMemoryGraph()
	// depth < 1
	err := g.ReplaceClosure("r", []ClosureRow{{GraphReleaseID: "r", SrcNodeID: "A", DstNodeID: "B", RelType: "prerequisite", Depth: 0, PathCount: 1}})
	if err == nil || !strings.Contains(err.Error(), "ck_kpc_depth_positive") {
		t.Fatalf("depth=0 应触发 ck: %v", err)
	}
	// path_count < 1
	err = g.ReplaceClosure("r", []ClosureRow{{GraphReleaseID: "r", SrcNodeID: "A", DstNodeID: "B", RelType: "prerequisite", Depth: 1, PathCount: 0}})
	if err == nil || !strings.Contains(err.Error(), "ck_kpc_path_count_positive") {
		t.Fatalf("path_count=0 应触发 ck: %v", err)
	}
	// src == dst
	err = g.ReplaceClosure("r", []ClosureRow{{GraphReleaseID: "r", SrcNodeID: "A", DstNodeID: "A", RelType: "prerequisite", Depth: 1, PathCount: 1}})
	if err == nil || !strings.Contains(err.Error(), "ck_kpc_no_self_loop") {
		t.Fatalf("自环应触发 ck: %v", err)
	}
}

// parallelEdgeView 是非传递并行边的最小 GraphView stub（绕过 store 去重，
// 直测 nonTransitiveDirectRows 的数据错误守卫）.
type parallelEdgeView struct{}

func (parallelEdgeView) Release(string) (*GraphRelease, error) {
	return &GraphRelease{ReleaseID: "parallel"}, nil
}

func (parallelEdgeView) RelationTypes() ([]RelationType, error) {
	return []RelationType{relSymmetric("confusable")}, nil
}

func (parallelEdgeView) Edges() ([]KpEdge, error) {
	return []KpEdge{edge("A", "B", "confusable"), edge("A", "B", "confusable")}, nil
}
