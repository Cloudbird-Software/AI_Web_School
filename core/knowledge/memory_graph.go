// memory_graph.go 承载知识域查询面端口的内存实现（PyR 纯逻辑层配套；PG 实现
// 属装配层）。语义对齐冻结实现的表约束：
//   - kp_node：uq_kp_node_pack_dim_code（pack+dimension+code 唯一）；
//   - kp_edge：uq_kp_edge_src_dst_rel（src+dst+rel_type 唯一）；
//   - relation_type：rel_type 主键唯一；
//   - graph_release：release_id 主键唯一；
//   - kp_closure：uq_kpc_release_src_dst_rel_depth 唯一 + ck（depth≥1、
//     path_count≥1、src<>dst），ReplaceClosure 先删后插幂等。
package knowledge

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sort"
	"sync"
)

// KpNode 知识点节点（kp_node 表投影）.
type KpNode struct {
	NodeID    string
	PackID    string
	Dimension string
	Code      string
	Title     string
	StdAnchor *string
	Gradeband *string
	Status    string
}

// MemoryGraph 是知识图谱五表（node/edge/relation_type/graph_release/closure）
// 的内存实现：互斥锁保护，唯一约束在写入时强制（迁移 0007/0014 的 23505 同义）。
type MemoryGraph struct {
	mu          sync.RWMutex
	nodes       []KpNode
	edges       []KpEdge
	relTypes    []RelationType
	releases    []GraphRelease
	closureRows map[string][]ClosureRow // graph_release_id → rows
}

// NewMemoryGraph 构造空图.
func NewMemoryGraph() *MemoryGraph {
	return &MemoryGraph{closureRows: map[string][]ClosureRow{}}
}

// ── 写入面（种子装载用）───────────────────────────────────────────────

// AddRelationType 插入关系类型；重复 = 唯一约束冲突错误（rel_type 主键）.
func (g *MemoryGraph) AddRelationType(rt RelationType) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, existing := range g.relTypes {
		if existing.RelType == rt.RelType {
			return fmt.Errorf("knowledge: relation_type %q 重复（主键冲突）", rt.RelType)
		}
	}
	g.relTypes = append(g.relTypes, rt)
	return nil
}

// AddNode 插入节点（查重键 pack+dimension+code）；重复 = 唯一约束冲突错误
// （uq_kp_node_pack_dim_code）.
func (g *MemoryGraph) AddNode(n KpNode) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, existing := range g.nodes {
		if existing.PackID == n.PackID && existing.Dimension == n.Dimension && existing.Code == n.Code {
			return fmt.Errorf("knowledge: kp_node %q 重复（uq_kp_node_pack_dim_code 冲突）", n.Code)
		}
	}
	g.nodes = append(g.nodes, n)
	return nil
}

// AddEdge 插入边（查重键 src+dst+rel_type）；重复 = 唯一约束冲突错误
// （uq_kp_edge_src_dst_rel）.
func (g *MemoryGraph) AddEdge(e KpEdge) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, existing := range g.edges {
		if existing.SrcNodeID == e.SrcNodeID && existing.DstNodeID == e.DstNodeID && existing.RelType == e.RelType {
			return fmt.Errorf("knowledge: kp_edge %s→%s(%s) 重复（uq_kp_edge_src_dst_rel 冲突）", e.SrcNodeID, e.DstNodeID, e.RelType)
		}
	}
	g.edges = append(g.edges, e)
	return nil
}

// AddRelease 插入图谱版本；重复返回错误（release_id 主键）.
func (g *MemoryGraph) AddRelease(r GraphRelease) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, existing := range g.releases {
		if existing.ReleaseID == r.ReleaseID {
			return fmt.Errorf("knowledge: graph_release %q 重复（release_id 主键冲突）", r.ReleaseID)
		}
	}
	g.releases = append(g.releases, r)
	return nil
}

// ── 读取面（GraphView）────────────────────────────────────────────────

// Release 取图谱版本；不存在返回 nil（调用方按「不存在」语义落错误）.
func (g *MemoryGraph) Release(releaseID string) (*GraphRelease, error) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	for i := range g.releases {
		if g.releases[i].ReleaseID == releaseID {
			r := g.releases[i]
			return &r, nil
		}
	}
	return nil, nil
}

// RelationTypes 返回全部关系类型（rel_type 排序，确定性）.
func (g *MemoryGraph) RelationTypes() ([]RelationType, error) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	out := append([]RelationType(nil), g.relTypes...)
	sort.Slice(out, func(i, j int) bool { return out[i].RelType < out[j].RelType })
	return out, nil
}

// Edges 返回全部边（src/dst/rel_type 排序，确定性）.
func (g *MemoryGraph) Edges() ([]KpEdge, error) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	out := append([]KpEdge(nil), g.edges...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].SrcNodeID != out[j].SrcNodeID {
			return out[i].SrcNodeID < out[j].SrcNodeID
		}
		if out[i].DstNodeID != out[j].DstNodeID {
			return out[i].DstNodeID < out[j].DstNodeID
		}
		return out[i].RelType < out[j].RelType
	})
	return out, nil
}

// NodeIDsByCode 本 pack+dimension 下 code → node_id（种子装载查重用）.
func (g *MemoryGraph) NodeIDsByCode(packID, dimension string) map[string]string {
	g.mu.RLock()
	defer g.mu.RUnlock()
	out := map[string]string{}
	for _, n := range g.nodes {
		if n.PackID == packID && n.Dimension == dimension {
			out[n.Code] = n.NodeID
		}
	}
	return out
}

// NodeCount 节点总数（测试断言用）.
func (g *MemoryGraph) NodeCount() int {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return len(g.nodes)
}

// EdgeCount 边总数（测试断言用）.
func (g *MemoryGraph) EdgeCount() int {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return len(g.edges)
}

// ── SeedSink（种子装载写入面的既有集合查询）────────────────────────────

// ExistingRelationTypes 已有关系类型集合（SeedSink）.
func (g *MemoryGraph) ExistingRelationTypes() (map[string]struct{}, error) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	out := map[string]struct{}{}
	for _, rt := range g.relTypes {
		out[rt.RelType] = struct{}{}
	}
	return out, nil
}

// ExistingNodeIDs 本 pack+dimension 下现存 code → node_id（SeedSink）.
func (g *MemoryGraph) ExistingNodeIDs(packID, dimension string) (map[string]string, error) {
	return g.NodeIDsByCode(packID, dimension), nil
}

// ExistingEdges 已有边集合（src,dst,rel_type 三元组；SeedSink）.
func (g *MemoryGraph) ExistingEdges() (map[[3]string]struct{}, error) {
	g.mu.RLock()
	defer g.mu.RUnlock()
	out := map[[3]string]struct{}{}
	for _, e := range g.edges {
		out[[3]string{e.SrcNodeID, e.DstNodeID, e.RelType}] = struct{}{}
	}
	return out, nil
}

// ── ClosureStore ──────────────────────────────────────────────────────

// ReplaceClosure 先删该 release 既有条目再写入（幂等），并强制 kp_closure
// 表约束（uq 唯一 + ck depth/path_count≥1、src<>dst）.
func (g *MemoryGraph) ReplaceClosure(graphReleaseID string, rows []ClosureRow) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	seen := map[closureRowKey]struct{}{}
	for _, r := range rows {
		k := closureRowKey{r.SrcNodeID, r.DstNodeID, r.RelType, r.Depth}
		if _, dup := seen[k]; dup {
			return fmt.Errorf("knowledge: kp_closure 唯一约束冲突（uq_kpc_release_src_dst_rel_depth）：src=%s dst=%s rel=%s depth=%d", r.SrcNodeID, r.DstNodeID, r.RelType, r.Depth)
		}
		seen[k] = struct{}{}
		if r.Depth < 1 {
			return fmt.Errorf("knowledge: kp_closure depth=%d 违反 ck_kpc_depth_positive", r.Depth)
		}
		if r.PathCount < 1 {
			return fmt.Errorf("knowledge: kp_closure path_count=%d 违反 ck_kpc_path_count_positive", r.PathCount)
		}
		if r.SrcNodeID == r.DstNodeID {
			return fmt.Errorf("knowledge: kp_closure src=dst=%s 违反 ck_kpc_no_self_loop", r.SrcNodeID)
		}
	}
	g.closureRows[graphReleaseID] = append([]ClosureRow(nil), rows...)
	return nil
}

// ClosureOf 读某 release 的闭包条目（depth/src/dst/rel 排序；测试断言用）.
func (g *MemoryGraph) ClosureOf(graphReleaseID string) []ClosureRow {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return append([]ClosureRow(nil), g.closureRows[graphReleaseID]...)
}

type closureRowKey struct {
	src, dst, relType string
	depth             int
}

// NewNodeID 生成节点 id（Python "kp_" + str(ulid.new()) 同语义：唯一性即可，
// 形态不参与任何语义判读。零依赖用 crypto/rand 16 字节十六进制）.
func NewNodeID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("knowledge: 熵源不可用无法生成 node_id: %w", err)
	}
	return "kp_" + hex.EncodeToString(b[:]), nil
}
