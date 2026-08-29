// seed_loader.go 承载知识图谱种子数据加载（Python 冻结基准
// src/core/knowledge/seed_loader.py 的 Go 移植；T-W2-014）。
//
// 从 content/seeds/*.yaml 加载知识图谱种子数据（kp_node + kp_edge +
// relation_type），按 (pack_id, dimension, code) 查重节点、按
// (src_node_id, dst_node_id, rel_type) 查重边、按 rel_type 查重关系类型——
// 重复导入 = skip，不抛错。
//
// 幂等约定（验收 #2）：同一文件多次 Load 不产生重复行，统计信息反映
// {added, skipped} 的实际操作计数。
//
// 为什么 YAML 用 code 而非 node_id 引用边：种子文件人类可读、可跨环境迁移；
// node_id 由加载器在第一次插入时生成（Python ULID / Go crypto/rand 十六进制，
// 唯一性语义同构），后续重复导入按 code 查重即可——node_id 跨环境不同但不
// 影响幂等性。
package knowledge

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"gopkg.in/yaml.v3"
)

// DefaultSeedPath 默认种子路径（Python DEFAULT_SEED_PATH 的仓库相对形；
// 调用方按运行目录解析为绝对路径）.
const DefaultSeedPath = "content" + string(filepath.Separator) + "seeds" + string(filepath.Separator) + "math_kp_3-4.yaml"

// RelationTypeSeed relation_type 行的种子定义（extra=forbid：未知字段拒绝）.
type RelationTypeSeed struct {
	RelType     string
	Directed    bool // 缺省 true（Python default）
	Transitive  bool // 缺省 false
	Acyclic     bool // 缺省 true
	Symmetric   bool // 缺省 false
	Description *string
}

// KpNodeSeed kp_node 行的种子定义（dimension 固定为 'kp'，由加载器填入）.
type KpNodeSeed struct {
	Code      string
	Title     string
	StdAnchor *string
	Gradeband *string // 缺省 "M"
}

// KpEdgeSeed kp_edge 行的种子定义（src/dst 用 code 引用，加载器解析为 node_id）.
type KpEdgeSeed struct {
	Src        string
	Dst        string
	RelType    string
	Attrs      map[string]any
	Provenance map[string]any
}

// SeedFile 种子 YAML 文件根模型（extra=forbid）.
type SeedFile struct {
	Version        string
	PackID         string
	GraphReleaseID string
	RelationTypes  []RelationTypeSeed
	Nodes          []KpNodeSeed
	Edges          []KpEdgeSeed
}

// ── YAML 原始形状（指针字段承载「缺省→默认值」链路；KnownFields(true)
//   兑现 pydantic extra="forbid"）────────────────────────────────────────

type rawRelationTypeSeed struct {
	RelType     *string `yaml:"rel_type"`
	Directed    *bool   `yaml:"directed"`
	Transitive  *bool   `yaml:"transitive"`
	Acyclic     *bool   `yaml:"acyclic"`
	Symmetric   *bool   `yaml:"symmetric"`
	Description *string `yaml:"description"`
}

type rawKpNodeSeed struct {
	Code      *string `yaml:"code"`
	Title     *string `yaml:"title"`
	StdAnchor *string `yaml:"std_anchor"`
	Gradeband *string `yaml:"gradeband"`
}

type rawKpEdgeSeed struct {
	Src        *string        `yaml:"src"`
	Dst        *string        `yaml:"dst"`
	RelType    *string        `yaml:"rel_type"`
	Attrs      map[string]any `yaml:"attrs"`
	Provenance map[string]any `yaml:"provenance"`
}

type rawSeedFile struct {
	Version        *string               `yaml:"version"`
	PackID         *string               `yaml:"pack_id"`
	GraphReleaseID *string               `yaml:"graph_release_id"`
	RelationTypes  []rawRelationTypeSeed `yaml:"relation_types"`
	Nodes          []rawKpNodeSeed       `yaml:"nodes"`
	Edges          []rawKpEdgeSeed       `yaml:"edges"`
}

// parseSeedYAML 读取并校验种子 YAML（Python parse_seed_file 的解析半边）.
// schema 校验失败（缺字段/类型不符/未知字段）返回错误。
func parseSeedYAML(data []byte) (*SeedFile, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true) // extra="forbid"
	var raw rawSeedFile
	if err := dec.Decode(&raw); err != nil {
		return nil, fmt.Errorf("knowledge: 种子 YAML 校验失败: %w", err)
	}
	if raw.Version == nil || raw.PackID == nil || raw.GraphReleaseID == nil {
		return nil, fmt.Errorf("knowledge: 种子 YAML 缺 version/pack_id/graph_release_id")
	}
	if raw.Nodes == nil {
		return nil, fmt.Errorf("knowledge: 种子 YAML 缺 nodes（必填）")
	}
	sf := &SeedFile{
		Version:        *raw.Version,
		PackID:         *raw.PackID,
		GraphReleaseID: *raw.GraphReleaseID,
		RelationTypes:  []RelationTypeSeed{},
		Nodes:          []KpNodeSeed{},
		Edges:          []KpEdgeSeed{},
	}
	boolOr := func(v *bool, d bool) bool {
		if v != nil {
			return *v
		}
		return d
	}
	for _, rt := range raw.RelationTypes {
		if rt.RelType == nil {
			return nil, fmt.Errorf("knowledge: relation_types 条目缺 rel_type")
		}
		sf.RelationTypes = append(sf.RelationTypes, RelationTypeSeed{
			RelType:     *rt.RelType,
			Directed:    boolOr(rt.Directed, true),
			Transitive:  boolOr(rt.Transitive, false),
			Acyclic:     boolOr(rt.Acyclic, true),
			Symmetric:   boolOr(rt.Symmetric, false),
			Description: rt.Description,
		})
	}
	for _, n := range raw.Nodes {
		if n.Code == nil || n.Title == nil {
			return nil, fmt.Errorf("knowledge: nodes 条目缺 code/title")
		}
		gradeband := "M"
		if n.Gradeband != nil {
			gradeband = *n.Gradeband
		}
		sf.Nodes = append(sf.Nodes, KpNodeSeed{
			Code:      *n.Code,
			Title:     *n.Title,
			StdAnchor: n.StdAnchor,
			Gradeband: &gradeband,
		})
	}
	for _, e := range raw.Edges {
		if e.Src == nil || e.Dst == nil || e.RelType == nil {
			return nil, fmt.Errorf("knowledge: edges 条目缺 src/dst/rel_type")
		}
		attrs := map[string]any{}
		for k, v := range e.Attrs {
			attrs[k] = v
		}
		prov := map[string]any{}
		for k, v := range e.Provenance {
			prov[k] = v
		}
		sf.Edges = append(sf.Edges, KpEdgeSeed{
			Src:        *e.Src,
			Dst:        *e.Dst,
			RelType:    *e.RelType,
			Attrs:      attrs,
			Provenance: prov,
		})
	}
	return sf, nil
}

// ParseSeedFile 读取并校验种子 YAML 文件（Python parse_seed_file）.
func ParseSeedFile(path string) (*SeedFile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("knowledge: 种子文件读取失败: %w", err)
	}
	return parseSeedYAML(data)
}

// SeedSink 是种子装载的写入面端口（relation_type/node/edge 三账的幂等 upsert）.
type SeedSink interface {
	// ExistingRelationTypes 已有关系类型集合.
	ExistingRelationTypes() (map[string]struct{}, error)
	// AddRelationType 插入关系类型.
	AddRelationType(rt RelationType) error
	// ExistingNodeIDs 本 pack+dimension 下现存 code → node_id.
	ExistingNodeIDs(packID, dimension string) (map[string]string, error)
	// AddNode 插入节点（node_id 由调用方生成后传入）.
	AddNode(n KpNode) error
	// ExistingEdges 已有边集合（src,dst,rel_type 三元组）.
	ExistingEdges() (map[[3]string]struct{}, error)
	// AddEdge 插入边.
	AddEdge(e KpEdge) error
}

// SeedLoadStats 幂等导入统计（Python load 返回 dict 的 Go 形）.
type SeedLoadStats struct {
	PackID               string
	GraphReleaseID       string
	RelationTypesAdded   int
	RelationTypesSkipped int
	NodesAdded           int
	NodesSkipped         int
	EdgesAdded           int
	EdgesSkipped         int
	// EdgesMissingNode src 或 dst code 未在文件内定义且库中不存在.
	EdgesMissingNode int
}

// seedLoader 内部并发保护（Load 本身单协程使用；锁面为测试并行化兜底）.
var seedLoaderMu sync.Mutex

// Load 幂等导入知识图谱种子数据（Python load）。
//
// path 为空时使用 DefaultSeedPath；dimension 默认 "kp"（任务卡验收 #3：所有
// 节点 dimension=kp）。YAML 内 src/dst code 未在 nodes 中定义且库中也不存在
// 时计入 EdgesMissingNode（记录但不抛错，让 caller 通过 stats 检测）。
func Load(path string, store SeedSink, dimension string) (*SeedLoadStats, error) {
	seedLoaderMu.Lock()
	defer seedLoaderMu.Unlock()

	if path == "" {
		path = DefaultSeedPath
	}
	seed, err := ParseSeedFile(path)
	if err != nil {
		return nil, err
	}
	stats := &SeedLoadStats{
		PackID:         seed.PackID,
		GraphReleaseID: seed.GraphReleaseID,
	}

	// ── 1. relation_types 幂等 upsert ──
	existingRelTypes, err := store.ExistingRelationTypes()
	if err != nil {
		return nil, err
	}
	for _, rt := range seed.RelationTypes {
		if _, ok := existingRelTypes[rt.RelType]; ok {
			stats.RelationTypesSkipped++
			continue
		}
		if err := store.AddRelationType(RelationType{
			RelType:     rt.RelType,
			Directed:    rt.Directed,
			Transitive:  rt.Transitive,
			Acyclic:     rt.Acyclic,
			Symmetric:   rt.Symmetric,
			Description: rt.Description,
		}); err != nil {
			return nil, err
		}
		existingRelTypes[rt.RelType] = struct{}{}
		stats.RelationTypesAdded++
	}

	// ── 2. nodes 幂等 upsert，构建 code → node_id 映射 ──
	// 查重键：(pack_id, dimension, code) 唯一约束 uq_kp_node_pack_dim_code
	codeToNodeID, err := store.ExistingNodeIDs(seed.PackID, dimension)
	if err != nil {
		return nil, err
	}
	for _, nodeSeed := range seed.Nodes {
		if _, ok := codeToNodeID[nodeSeed.Code]; ok {
			stats.NodesSkipped++
			continue
		}
		nodeID, err := NewNodeID()
		if err != nil {
			return nil, err
		}
		if err := store.AddNode(KpNode{
			NodeID:    nodeID,
			PackID:    seed.PackID,
			Dimension: dimension,
			Code:      nodeSeed.Code,
			Title:     nodeSeed.Title,
			StdAnchor: nodeSeed.StdAnchor,
			Gradeband: nodeSeed.Gradeband,
			Status:    "active", // 种子数据默认 active
		}); err != nil {
			return nil, err
		}
		codeToNodeID[nodeSeed.Code] = nodeID
		stats.NodesAdded++
	}

	// ── 3. edges 幂等 upsert，src/dst 用 code 解析 ──
	// 查重键：(src_node_id, dst_node_id, rel_type) 唯一约束 uq_kp_edge_src_dst_rel。
	// 加载器在文件级别去重——同一文件内若有重复 (src, dst, rel_type)，
	// 第二次出现算 skipped（不抛错，方便种子文件冗余书写）。
	seenEdges, err := store.ExistingEdges()
	if err != nil {
		return nil, err
	}
	for _, edgeSeed := range seed.Edges {
		srcID, srcOK := codeToNodeID[edgeSeed.Src]
		dstID, dstOK := codeToNodeID[edgeSeed.Dst]
		if !srcOK || !dstOK {
			// 文件内引用了未定义的 code——记录但不抛错，让 caller 通过 stats 检测
			stats.EdgesMissingNode++
			continue
		}
		key := [3]string{srcID, dstID, edgeSeed.RelType}
		if _, ok := seenEdges[key]; ok {
			stats.EdgesSkipped++
			continue
		}
		if err := store.AddEdge(KpEdge{
			SrcNodeID:  srcID,
			DstNodeID:  dstID,
			RelType:    edgeSeed.RelType,
			Attrs:      edgeSeed.Attrs,
			Provenance: edgeSeed.Provenance,
		}); err != nil {
			return nil, err
		}
		seenEdges[key] = struct{}{}
		stats.EdgesAdded++
	}

	return stats, nil
}
