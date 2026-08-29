// spec_table.go 承载双向细目表约束 schema（Python 冻结基准
// src/core/assembly/spec_table.py 的 Go 移植；架构 v2 §4.4「测量(预留)」行：
// 「双向细目表（内容×认知×题量×难度）= 单元格计数约束」）。
//
// 双向细目表（Two-Way Specification Table）：
//   - 第一维：内容（知识点编码 content_code，可任意层级深度的点分树形 code）
//   - 第二维：认知层级（Bloom 六级，与 Objective.cognitive_level 同口径）
//   - 每个单元格 {target_count, difficulty_min, difficulty_max}：
//     target_count 该单元格目标题数；difficulty_min/max 题目难度区间
//     （p_correct 口径，越大越易；与 item_param.params.difficulty、
//     AssemblyProfile.target_p_correct_range 同口径，避免单位混估）
//
// 为什么 difficulty 用 p_correct 而非「难度系数（越大越难）」：平台既有参数
// 体系一致采用「p_correct = 答对率，越大越易」；双向细目表的难度区间要与
// 候选池 p_correct_prior 直接比较，同单位才能编译为约束。命名沿用任务卡原文，
// 语义 = p_correct 区间。
//
// 校验规则：1) 全部单元格 target_count 之和 > 0；2) 单个单元格
// difficulty_min ≤ difficulty_max；3) 维度编码存在性（ValidateAgainstGraph
// 显式调用，不在构造期强制——构造期不知图谱范围）。
package assembly

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// 认知层级六值（与 Objective.cognitive_level 同口径，避免单位混估）.
var cognitiveLevels = []string{
	"remember", "understand", "apply", "analyze", "evaluate", "create",
}

// IsValidCognitiveLevel 认知层级域判定.
func IsValidCognitiveLevel(level string) bool {
	return containsStr(cognitiveLevels, level)
}

// SpecCell 双向细目表单元格：内容×认知 的目标题量与难度区间（Python SpecCell）.
type SpecCell struct {
	// ContentCode 知识点编码（任意层级点分树形 code，如
	// "math.nal.decimal.compare"）；存在性由 SpecTable.ValidateAgainstGraph
	// 显式校验，构造期不强制.
	ContentCode string
	// CognitiveLevel 认知层级（Bloom 六级，与 Objective.cognitive_level 同集）.
	CognitiveLevel string
	// TargetCount 该单元格目标题数（≥0；0 表示该单元格不要求题量）.
	TargetCount int
	// DifficultyMin/Max 题目难度上下限（p_correct 口径，[0.0,1.0]，越大越易）.
	DifficultyMin float64
	DifficultyMax float64
}

// validate 构造期校验（Python model_validator：认知层级域 + 难度区间 + 计数域）.
func (c *SpecCell) validate() error {
	if len(c.ContentCode) < 1 {
		return fmt.Errorf("assembly: spec cell content_code 不能为空（min_length=1）")
	}
	if !IsValidCognitiveLevel(c.CognitiveLevel) {
		return fmt.Errorf("assembly: cognitive_level %q 越域；合法域 %v", c.CognitiveLevel, cognitiveLevels)
	}
	if c.TargetCount < 0 {
		return fmt.Errorf("assembly: spec cell [%s/%s] target_count = %d 越域（ge=0）", c.ContentCode, c.CognitiveLevel, c.TargetCount)
	}
	if c.DifficultyMin < 0.0 || c.DifficultyMin > 1.0 || c.DifficultyMax < 0.0 || c.DifficultyMax > 1.0 {
		return fmt.Errorf("assembly: spec cell [%s/%s] 难度区间越域（[0.0, 1.0]）", c.ContentCode, c.CognitiveLevel)
	}
	// 单个单元格 difficulty_min ≤ difficulty_max
	if c.DifficultyMin > c.DifficultyMax {
		return fmt.Errorf("assembly: 单元格 [%s/%s] difficulty_min=%v > difficulty_max=%v",
			c.ContentCode, c.CognitiveLevel, c.DifficultyMin, c.DifficultyMax)
	}
	return nil
}

// SpecTable 双向细目表：单元格集合 + 引用元数据（Python SpecTable）。
// 一份 SpecTable = 一次测量卷的内容×认知×题量×难度目标分布。表本身只增不改
// （D1 风格，ORM 层物理强制；本模型只负责 schema）。
type SpecTable struct {
	// SpecTableID 表 id（ULID 或语义 id）.
	SpecTableID string
	// SpecTableVersion 表版本（同 id 改版需递增版本；D1 版本账）.
	SpecTableVersion string
	// Gradeband 学段（L/M/H，与 AssemblyProfile.gradeband 同集）.
	Gradeband string
	// GraphRelease 引用的知识图谱 release id（维度编码存在性校验依据）.
	GraphRelease string
	// Cells 单元格列表；(content_code, cognitive_level) 唯一.
	Cells []SpecCell
}

// NewSpecTable 构造并校验细目表（Python model_validator 全量：学段域、
// total_count>0、单元格唯一性、单元格域）.
func NewSpecTable(specTableID, specTableVersion, gradeband, graphRelease string, cells []SpecCell) (*SpecTable, error) {
	st := &SpecTable{
		SpecTableID:      specTableID,
		SpecTableVersion: specTableVersion,
		Gradeband:        gradeband,
		GraphRelease:     graphRelease,
		Cells:            append([]SpecCell(nil), cells...),
	}
	if len(specTableID) < 1 || len(specTableVersion) < 1 || len(graphRelease) < 1 {
		return nil, fmt.Errorf("assembly: spec_table_id/spec_table_version/graph_release 不能为空（min_length=1）")
	}
	if !containsStr([]string{GradebandL, GradebandM, GradebandH}, gradeband) {
		return nil, fmt.Errorf("assembly: gradeband %q 越域；合法域 ['L', 'M', 'H']", gradeband)
	}
	if len(st.Cells) < 1 {
		return nil, fmt.Errorf("assembly: 细目表 cells 不能为空（min_length=1）")
	}
	for i := range st.Cells {
		if err := (&st.Cells[i]).validate(); err != nil {
			return nil, err
		}
	}
	// 全部单元格 target_count 之和 > 0
	if st.TotalCount() <= 0 {
		return nil, fmt.Errorf("assembly: 细目表所有单元格 target_count 之和 = %d，必须 > 0", st.TotalCount())
	}
	// 同一 (content_code, cognitive_level) 唯一：否则单元格语义重叠，组卷时
	// 无法判定题量归属。允许同一 content_code 在不同 cognitive_level 出现。
	type cellKey struct{ content, cognitive string }
	seen := map[cellKey]struct{}{}
	dupes := []string{}
	for _, c := range st.Cells {
		k := cellKey{c.ContentCode, c.CognitiveLevel}
		if _, ok := seen[k]; ok {
			dupes = append(dupes, fmt.Sprintf("%s/%s", c.ContentCode, c.CognitiveLevel))
		}
		seen[k] = struct{}{}
	}
	if len(dupes) > 0 {
		return nil, fmt.Errorf("assembly: 细目表单元格 (content_code, cognitive_level) 重复：%v", dupes)
	}
	return st, nil
}

// TotalCount 所有单元格 target_count 之和（派生量；Python total_count）.
func (st *SpecTable) TotalCount() int {
	total := 0
	for i := range st.Cells {
		total += st.Cells[i].TargetCount
	}
	return total
}

// ValidateAgainstGraph 校验所有 cell.content_code 存在于给定图谱编码集合
// （Python validate_against_graph）。
//
// 为什么不在构造期强制：构造期 SpecTable 不持有图谱快照（图谱是独立版本化
// 资产，graph_release 字段只是引用指针）；调用方在组卷前以 graph_release 对应
// 的 kp_node.code 集合调用本方法做存在性校验。本方法与 ORM 层 FK 不同：图谱
// 节点本身是版本化的（graph_release），跨版本存在性需运行期校验。
//
// 返回不存在的 content_code 列表（全部存在时为空且 error=nil；存在未知编码时
// 返回排序后的未知编码列表并携带 error）。
func (st *SpecTable) ValidateAgainstGraph(validContentCodes []string) ([]string, error) {
	valid := map[string]struct{}{}
	for _, c := range validContentCodes {
		valid[c] = struct{}{}
	}
	unknownSet := map[string]struct{}{}
	for i := range st.Cells {
		if _, ok := valid[st.Cells[i].ContentCode]; !ok {
			unknownSet[st.Cells[i].ContentCode] = struct{}{}
		}
	}
	if len(unknownSet) == 0 {
		return []string{}, nil
	}
	unknown := make([]string, 0, len(unknownSet))
	for c := range unknownSet {
		unknown = append(unknown, c)
	}
	sort.Strings(unknown)
	return unknown, fmt.Errorf(
		"assembly: 细目表引用了图谱 %q 中不存在的 content_code：%v（共 %d 个）；请检查 graph_release 或 cells",
		st.GraphRelease, unknown, len(unknown))
}

// CellAt 按 (content_code, cognitive_level) 取单元格；不存在返回 nil
// （Python cell_at）.
func (st *SpecTable) CellAt(contentCode, cognitiveLevel string) *SpecCell {
	for i := range st.Cells {
		if st.Cells[i].ContentCode == contentCode && st.Cells[i].CognitiveLevel == cognitiveLevel {
			return &st.Cells[i]
		}
	}
	return nil
}

// dump 转 Python model_dump(mode="json") 同形值树（键名 snake_case 逐字一致）.
func (st *SpecTable) dump() map[string]any {
	cells := make([]any, 0, len(st.Cells))
	for i := range st.Cells {
		c := &st.Cells[i]
		cells = append(cells, map[string]any{
			"content_code":    c.ContentCode,
			"cognitive_level": c.CognitiveLevel,
			"target_count":    c.TargetCount,
			"difficulty_min":  c.DifficultyMin,
			"difficulty_max":  c.DifficultyMax,
		})
	}
	return map[string]any{
		"spec_table_id":      st.SpecTableID,
		"spec_table_version": st.SpecTableVersion,
		"gradeband":          st.Gradeband,
		"graph_release":      st.GraphRelease,
		"cells":              cells,
	}
}

// fromDump 从 Python model_dump 同形值树还原（JSON/YAML 反序列化共用前置）.
func specTableFromDump(obj map[string]any) (*SpecTable, error) {
	id, _ := obj["spec_table_id"].(string)
	version, _ := obj["spec_table_version"].(string)
	gradeband, _ := obj["gradeband"].(string)
	release, _ := obj["graph_release"].(string)
	rawCells, _ := obj["cells"].([]any)
	cells := make([]SpecCell, 0, len(rawCells))
	for _, rc := range rawCells {
		m, ok := rc.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("assembly: spec_table.cells 元素形状非法")
		}
		c := SpecCell{}
		c.ContentCode, _ = m["content_code"].(string)
		c.CognitiveLevel, _ = m["cognitive_level"].(string)
		n, err := asInt(m["target_count"])
		if err != nil {
			return nil, fmt.Errorf("assembly: spec cell target_count 非法: %v", err)
		}
		c.TargetCount = n
		dmin, err := asFloat(m["difficulty_min"])
		if err != nil {
			return nil, fmt.Errorf("assembly: spec cell difficulty_min 非法: %v", err)
		}
		dmax, err := asFloat(m["difficulty_max"])
		if err != nil {
			return nil, fmt.Errorf("assembly: spec cell difficulty_max 非法: %v", err)
		}
		c.DifficultyMin, c.DifficultyMax = dmin, dmax
		cells = append(cells, c)
	}
	return NewSpecTable(id, version, gradeband, release, cells)
}

// ToJSON 序列化为 JSON 字符串（Python to_json：确定性 sort_keys，
// ensure_ascii=False）。规范化与 Python json.dumps 逐字节对齐（canon.go），
// 跨实现可比对/互验。
func (st *SpecTable) ToJSON() string {
	return canonicalJSON(st.dump())
}

// FromJSON 从 JSON 字符串反序列化（与 ToJSON 互逆；Python from_json）.
func FromJSON(data string) (*SpecTable, error) {
	var obj map[string]any
	if err := json.Unmarshal([]byte(data), &obj); err != nil {
		return nil, fmt.Errorf("assembly: spec_table JSON 反序列化失败: %w", err)
	}
	return specTableFromDump(obj)
}

// ToYAML 序列化为 YAML 字符串（Python to_yaml：经 JSON 中转保留类型，
// sort_keys 确定性；Go 侧 yaml.Marshal 对 map[string]any 同样按键排序输出）.
func (st *SpecTable) ToYAML() (string, error) {
	out, err := yaml.Marshal(st.dump())
	if err != nil {
		return "", fmt.Errorf("assembly: spec_table YAML 序列化失败: %w", err)
	}
	return string(out), nil
}

// FromYAML 从 YAML 字符串反序列化（与 ToYAML 互逆；Python from_yaml：
// yaml.safe_load → model_validate）.
func FromYAML(data string) (*SpecTable, error) {
	var obj map[string]any
	dec := yaml.NewDecoder(strings.NewReader(data))
	dec.KnownFields(false) // YAML 别名/锚点展开后键域与 dump 一致；校验由 NewSpecTable 承担
	if err := dec.Decode(&obj); err != nil {
		return nil, fmt.Errorf("assembly: spec_table YAML 反序列化失败: %w", err)
	}
	return specTableFromDump(obj)
}

// ToDict 序列化为 dict（给 ORM 层 JSONB 落库用；与 model_dump(mode='json')
// 一致——Go 形即 dump 值树）.
func (st *SpecTable) ToDict() map[string]any {
	return st.dump()
}
