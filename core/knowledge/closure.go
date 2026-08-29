// closure.go 承载知识图谱传递闭包计算（Python 冻结基准
// src/core/knowledge/closure.py 的 Go 移植；T-W2-013）。
//
// 架构 v2 §4.2：闭包预计算后写入 kp_closure 扁平表，热路径查询退化为单表
// 过滤；递归 CTE 仅在闭包计算时使用（管理查询，非热路径）。
//
// 闭包计算规则（任务卡验收 #2）：
//   - 对 transitive 关系类型（如 prerequisite/composes）：递归展开多跳可达；
//   - 对非 transitive 关系类型（如 confusable）：仅 depth=1 直接边；
//   - path_count：同 (src, dst, rel_type, depth) 的不同路径数。
//
// 冻结实现用 PostgreSQL 递归 CTE 枚举所有路径再聚合（千级节点万级边的图在
// DB 内执行优于应用层往返）；Go 移植为纯函数内核（路径枚举 DFS + 计数聚合），
// 语义逐条对齐 CTE：
//   - 基础：直接边（depth=1）；
//   - 递归：在路径末端扩展一跳，深度上限 max_depth（默认 50，超过判定为
//     环路保护——acyclic 约束被违反时的深度爆炸防线）；
//   - 循环检测：CTE 的 visited 数组 → Go 的路径访问集（dst 不得已在路径中）；
//   - 时间窗过滤：as_of 为 nil（release 无 valid_from=实时快照）时不过滤，
//     否则仅保留 valid_from ≤ as_of < valid_to 的边（frozen/historical 快照）。
package knowledge

import (
	"fmt"
	"sort"
	"time"
)

// MaxClosureDepth 闭包深度安全上限——超过此深度判定为环路（违反 acyclic
// 约束；Python _MAX_DEPTH）.
const MaxClosureDepth = 50

// KpEdge 知识点边（kp_edge 表的闭包相关投影；Attrs/Provenance 供种子装载
// 留档）.
type KpEdge struct {
	SrcNodeID  string
	DstNodeID  string
	RelType    string
	ValidFrom  *time.Time
	ValidTo    *time.Time
	Attrs      map[string]any
	Provenance map[string]any
}

// RelationType 关系类型（relation_type 表投影）.
type RelationType struct {
	RelType     string
	Directed    bool
	Transitive  bool
	Acyclic     bool
	Symmetric   bool
	Description *string
}

// GraphRelease 图谱版本（graph_release 表投影；valid_from=NULL 表示实时快照
// （active 状态无时间约束），闭包不做时间过滤）.
type GraphRelease struct {
	ReleaseID    string
	Status       string
	ValidFrom    *time.Time
	ValidTo      *time.Time
	SupersededBy *string
}

// ClosureRow 闭包条目（kp_closure 一行；同 (graph_release_id, src, dst,
// rel_type, depth) 唯一，path_count 承载多路径）.
type ClosureRow struct {
	GraphReleaseID string
	SrcNodeID      string
	DstNodeID      string
	RelType        string
	Depth          int
	PathCount      int
}

// ClosureStats 闭包计算统计（Python compute_closure 返回 dict 的 Go 形）.
type ClosureStats struct {
	GraphReleaseID        string
	AsOf                  *time.Time
	ClosureRows           int
	TransitiveRelTypes    []string
	NonTransitiveRelTypes []string
}

// GraphView 是闭包计算的图快照查询面（对应冻结实现的 graph_release /
// relation_type / kp_edge 三表读取）.
type GraphView interface {
	// Release 取图谱版本；不存在返回错误（Python db.get 返回 None → ValueError）.
	Release(releaseID string) (*GraphRelease, error)
	// RelationTypes 返回全部关系类型及其 transitive 标志.
	RelationTypes() ([]RelationType, error)
	// Edges 返回全部边（含时间窗列）.
	Edges() ([]KpEdge, error)
}

// ClosureStore 是 kp_closure 账面端口（幂等：先删该 release 既有条目再写入，
// Python compute_closure 的 DELETE+INSERT 序）.
type ClosureStore interface {
	// ReplaceClosure 原子替换某 graph_release 的闭包条目；实现方必须强制
	// uq_kpc_release_src_dst_rel_depth / ck_kpc_no_self_loop 等表约束.
	ReplaceClosure(graphReleaseID string, rows []ClosureRow) error
}

// edgeVisible 边的时间窗过滤（CTE 的 valid_from ≤ as_of < valid_to 同义；
// as_of 为 nil 表示实时快照——包含所有边无论其时间窗如何）.
func edgeVisible(e *KpEdge, asOf *time.Time) bool {
	if asOf == nil {
		return true
	}
	if e.ValidFrom != nil && e.ValidFrom.After(*asOf) {
		return false
	}
	if e.ValidTo != nil && !e.ValidTo.After(*asOf) {
		return false
	}
	return true
}

// ComputeClosure 计算并写入 kp_closure（按 graph_release 版本缓存；Python
// compute_closure）。
//
// 幂等：先删除该 graph_release 的既有闭包条目，再重新计算写入。
// maxDepth < 1 或 release 不存在时返回错误。
func ComputeClosure(graphReleaseID string, graph GraphView, store ClosureStore, maxDepth int) (*ClosureStats, error) {
	if maxDepth < 1 {
		return nil, fmt.Errorf("knowledge: max_depth 必须 >= 1，实际 %d", maxDepth)
	}
	// ── 校验 graph_release 存在并取 valid_from 作为 as-of 时间 ──
	release, err := graph.Release(graphReleaseID)
	if err != nil {
		return nil, err
	}
	if release == nil {
		return nil, fmt.Errorf("knowledge: graph_release_id=%q 不存在", graphReleaseID)
	}
	asOf := release.ValidFrom

	// ── 收集所有 relation_type 与其 transitive 标志 ──
	relTypes, err := graph.RelationTypes()
	if err != nil {
		return nil, err
	}
	// 遍历序确定性：按 rel_type 排序（冻结实现为 DB 行序；键序同样可复现）
	ordered := append([]RelationType(nil), relTypes...)
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].RelType < ordered[j].RelType })

	edges, err := graph.Edges()
	if err != nil {
		return nil, err
	}

	transitiveUsed := []string{}
	nonTransitiveUsed := []string{}
	allRows := []ClosureRow{}

	for _, rt := range ordered {
		var rows []ClosureRow
		if rt.Transitive {
			rows, err = transitiveClosureRows(graphReleaseID, rt.RelType, edges, asOf, maxDepth)
			transitiveUsed = append(transitiveUsed, rt.RelType)
		} else {
			rows, err = nonTransitiveDirectRows(graphReleaseID, rt.RelType, edges, asOf)
			nonTransitiveUsed = append(nonTransitiveUsed, rt.RelType)
		}
		if err != nil {
			return nil, err
		}
		allRows = append(allRows, rows...)
	}

	// ── 幂等写入（先删后插由 store 承担）──
	if err := store.ReplaceClosure(graphReleaseID, allRows); err != nil {
		return nil, err
	}

	return &ClosureStats{
		GraphReleaseID:        graphReleaseID,
		AsOf:                  asOf,
		ClosureRows:           len(allRows),
		TransitiveRelTypes:    transitiveUsed,
		NonTransitiveRelTypes: nonTransitiveUsed,
	}, nil
}

// transitiveClosureRows 传递闭包：路径枚举 + 聚合（_TRANSITIVE_CLOSURE_SQL
// 的递归 CTE 同义实现）。
//
// CTE 形态：基础 = depth 1 的直接边（visited=[src,dst]）；递归 = 在路径末端
// 扩展一跳（depth+1，dst 不得已在 visited 中，depth < max_depth 才续）；
// 最终按 (src, dst, rel_type, depth) GROUP BY COUNT(*) 为 path_count。
func transitiveClosureRows(graphReleaseID, relType string, edges []KpEdge, asOf *time.Time, maxDepth int) ([]ClosureRow, error) {
	// 邻接表：仅该 rel_type 且时间窗可见的边；目标序排序保证枚举确定性.
	adj := map[string][]string{}
	for i := range edges {
		e := &edges[i]
		if e.RelType != relType || !edgeVisible(e, asOf) {
			continue
		}
		adj[e.SrcNodeID] = append(adj[e.SrcNodeID], e.DstNodeID)
	}
	for src := range adj {
		targets := adj[src]
		sort.Strings(targets)
		adj[src] = targets
	}

	counts := map[closureKey]int{}
	// 以每条可见直接边为路径种子，DFS 枚举全部简单路径.
	var walk func(src, current string, depth int, visited map[string]struct{})
	walk = func(src, current string, depth int, visited map[string]struct{}) {
		counts[closureKey{src, current, depth}]++
		if depth >= maxDepth {
			return // p.depth < :max_depth 才允许继续扩展
		}
		for _, next := range adj[current] {
			if _, inPath := visited[next]; inPath {
				continue // e.dst_node_id <> ALL(p.visited)：循环检测
			}
			visited[next] = struct{}{}
			walk(src, next, depth+1, visited)
			delete(visited, next)
		}
	}
	for src := range adj {
		for _, dst := range adj[src] {
			visited := map[string]struct{}{src: {}, dst: {}}
			walk(src, dst, 1, visited)
		}
	}

	rows := make([]ClosureRow, 0, len(counts))
	for k, n := range counts {
		if k.src == k.dst {
			// ck_kpc_no_self_loop：src<>dst（CTE 对自环边会产出 A→A 行，
			// 冻结实现在 INSERT 时触发 CHECK 失败——这里前置为显式错误）
			return nil, fmt.Errorf("knowledge: 闭包出现自环 src=dst=%q（rel_type=%s；违反 ck_kpc_no_self_loop，请检查图数据 acyclic 约束）", k.src, relType)
		}
		rows = append(rows, ClosureRow{
			GraphReleaseID: graphReleaseID,
			SrcNodeID:      k.src,
			DstNodeID:      k.dst,
			RelType:        relType,
			Depth:          k.depth,
			PathCount:      n,
		})
	}
	sortClosureRows(rows)
	return rows, nil
}

// nonTransitiveDirectRows 非传递关系：仅直接边（depth=1；每条可见边一条，
// path_count=1——_NON_TRANSITIVE_DIRECT_SQL 同义）.
func nonTransitiveDirectRows(graphReleaseID, relType string, edges []KpEdge, asOf *time.Time) ([]ClosureRow, error) {
	type key struct{ src, dst string }
	counts := map[key]int{}
	for i := range edges {
		e := &edges[i]
		if e.RelType != relType || !edgeVisible(e, asOf) {
			continue
		}
		counts[key{e.SrcNodeID, e.DstNodeID}]++
	}
	rows := make([]ClosureRow, 0, len(counts))
	for k, n := range counts {
		if k.src == k.dst {
			return nil, fmt.Errorf("knowledge: 闭包出现自环 src=dst=%q（rel_type=%s；违反 ck_kpc_no_self_loop，请检查图数据 acyclic 约束）", k.src, relType)
		}
		// uq_kpc_release_src_dst_rel_depth：同 (src,dst) 并行边在 kp_edge 的
		// uq_kp_edge_src_dst_rel 下不可能；出现即数据错，显式报错而非静默聚合.
		if n > 1 {
			return nil, fmt.Errorf("knowledge: rel_type=%s 存在并行边 %s→%s（%d 条）；违反 uq_kp_edge_src_dst_rel", relType, k.src, k.dst, n)
		}
		rows = append(rows, ClosureRow{
			GraphReleaseID: graphReleaseID,
			SrcNodeID:      k.src,
			DstNodeID:      k.dst,
			RelType:        relType,
			Depth:          1,
			PathCount:      1,
		})
	}
	sortClosureRows(rows)
	return rows, nil
}

// closureKey 聚合键 (src, dst, depth)（rel_type 在调用方作用域内恒定）.
type closureKey struct {
	src   string
	dst   string
	depth int
}

// sortClosureRows 确定性输出序：(rel_type, src, dst, depth).
func sortClosureRows(rows []ClosureRow) {
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].RelType != rows[j].RelType {
			return rows[i].RelType < rows[j].RelType
		}
		if rows[i].SrcNodeID != rows[j].SrcNodeID {
			return rows[i].SrcNodeID < rows[j].SrcNodeID
		}
		if rows[i].DstNodeID != rows[j].DstNodeID {
			return rows[i].DstNodeID < rows[j].DstNodeID
		}
		return rows[i].Depth < rows[j].Depth
	})
}
