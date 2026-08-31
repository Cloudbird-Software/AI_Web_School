package subjectmath

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Instance 是生成产物：item_version 契约（specs/contracts/db/item-model.md
// §2.2）四个语义字段的 map 形态 + 谱系。字段只增不改——下游门与写入服务
// 消费的就是这个形态，不让生成器私造第二套结构。
//
// content 结构（对齐 tests/golden 已发布快照形态，块+槽位语义 AST）：
//
//	blocks       题干/选项渲染块 [{kind,text,template,rendered}]
//	answer       机器可读答案位 {letter?,value,blank_id?,unit?}
//	explanation  解析文本（含关键数字，供 validator 独立复核）
type Instance struct {
	TemplateID        string           `json:"template_id"`
	TemplateVersionID string           `json:"template_version_id"` // sha256:<hex>（母题 spec 内容寻址）
	Locale            string           `json:"locale"`
	Objective         map[string]any   `json:"objective"`       // 契约 §5.1 形态
	InteractionRef    map[string]any   `json:"interaction_ref"` // {interaction_id, interaction_params}（D4）
	Content           map[string]any   `json:"content"`         // 题面语义 AST
	ScoringRef        map[string]any   `json:"scoring_ref"`     // {scorer_id, scorer_params}（D4）
	ErrorBindings     []map[string]any `json:"error_bindings"`  // 选项/空位 → 错误类型（R-Q-06）
	Lineage           map[string]any   `json:"lineage"`         // 契约 §5.2 形态（seed 由 batch 层注入）
}

// objective 按 §5.1 组装（kp 均取自 content/seeds/math_kp_3-4.yaml 已登记节点，
// graph_release 与种子图谱版本一致）。
func objective(kpCode, cognitiveLevel, gradeband string) map[string]any {
	return map[string]any{
		"kp_set": []any{
			map[string]any{"dimension": "kp", "code": kpCode},
		},
		"kp_set_mode":     "single",
		"cognitive_level": cognitiveLevel,
		"gradeband":       gradeband,
		"graph_release":   "2026.1",
	}
}

// lineage 按 §5.2 组装（A 级：template_version_id 与 params 必填；
// ai_ledger_refs 空 = 数学轮确定性路径，未经任何 LLM 调用）。
// signed_by/signed_at 为占位：签名属校验门签发动作，此处保持零值确定。
func lineage(tplVersionID string, normalized map[string]any) map[string]any {
	return map[string]any{
		"tier":                "A",
		"pipeline":            map[string]any{"id": "subjectmath-mathgen", "version": "1.0.0"},
		"template_version_id": tplVersionID,
		"params":              map[string]any{"normalized": normalized},
		"corpus_refs":         []any{},
		"ai_ledger_refs":      []any{},
		"signed_by":           "mathgen-batch(门签发前占位)",
		"signed_at":           "0001-01-01T00:00:00Z",
	}
}

// canonicalView 整实例的 map 规范化视图：回放对比/测试对「整实例逐字节一致」
// 断言用。注意 H-W6-1 结构互异判定只看 ContentDigest（content 域），本视图
// 不参与唯一率口径。
func (in *Instance) canonicalView() map[string]any {
	return map[string]any{
		"template_id":         in.TemplateID,
		"template_version_id": in.TemplateVersionID,
		"locale":              in.Locale,
		"objective":           in.Objective,
		"interaction_ref":     in.InteractionRef,
		"content":             in.Content,
		"scoring_ref":         in.ScoringRef,
		"error_bindings":      anySliceOf(in.ErrorBindings),
		"lineage":             in.Lineage,
	}
}

// anySliceOf 把 []map[string]any 提升为 []any（canonical 只认 []any）。
func anySliceOf(ms []map[string]any) []any {
	out := make([]any, len(ms))
	for i, m := range ms {
		out[i] = m
	}
	return out
}

// textBlock 构造题干/选项渲染块（kind=text，template+rendered 双栏，
// 与 tests/golden/items/math/*.yaml 的 expected_content_snapshot 同构）。
func textBlock(template, rendered string) map[string]any {
	return map[string]any{"kind": "text", "template": template, "rendered": rendered}
}

// optionBlocks 把洗牌后的选项序列拼成 A/B/C/D 渲染块。
func optionBlocks(opts []namedOption) []any {
	blocks := make([]any, 0, len(opts))
	for i, o := range opts {
		let := string(rune('A' + i))
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+o.label))
	}
	return blocks
}

// namedOption 是带错误类型出处的选项：干扰项即错误映射（R-Q-06），
// 装配 error_bindings 时按出处回填，避免“事后猜归属”。
type namedOption struct {
	label     string // 选项呈现文本（如 "168"、"3/7"、"一样大" 不适用——干扰项均为数值）
	errorType string // 对应错误类型 id；正解项为空串
}

// fmtInt 整数统一十进制无符号/带负号直排——禁止千分位等展示层花样进内容。
func fmtInt(v int64) string { return strconv.FormatInt(v, 10) }

// decString 把 mantissa / 10^scale 规范化为最短十进制串：
// 无尾随零、整数不带小数点、纯小数补前导 "0."（如 (15,1)="1.5"、(5,1)="0.5"）。
// 全程整数运算禁 float（契约 §3 D2：定点/分数运算，浮点漂移禁止进内容寻址链）。
func decString(mantissa int64, scale int) string {
	if scale <= 0 {
		return fmtInt(mantissa * pow10(-scale))
	}
	s := fmtInt(absI64(mantissa))
	var intPart, fracPart string
	if len(s) > scale {
		intPart, fracPart = s[:len(s)-scale], s[len(s)-scale:]
	} else {
		intPart = "0"
		fracPart = strings.Repeat("0", scale-len(s)) + s
	}
	fracPart = strings.TrimRight(fracPart, "0")
	if fracPart == "" {
		if mantissa < 0 {
			return "-" + intPart
		}
		return intPart
	}
	out := intPart + "." + fracPart
	if mantissa < 0 {
		return "-" + out
	}
	return out
}

// parseDecString 是 validators 的独立解析入口（与 decString 互为逆运算但
// 分文件维护）：只接受 [digits][.digits] 无符号形态；返回 (mantissa, scale)。
func parseDecString(s string) (int64, int, error) {
	intPart, fracPart, _ := strings.Cut(s, ".")
	if intPart == "" && fracPart == "" {
		return 0, 0, fmt.Errorf("空数值串 %q", s)
	}
	if !allDigits(intPart) || !allDigits(fracPart) {
		return 0, 0, fmt.Errorf("非规范化数值串 %q（禁符号/空白/多小数点）", s)
	}
	m, err := strconv.ParseInt(intPart+fracPart, 10, 64)
	if err != nil {
		return 0, 0, fmt.Errorf("数值超出 int64：%q", s)
	}
	if s != "" && s[0] == '0' && len(intPart) > 1 {
		return 0, 0, fmt.Errorf("前导零非规范：%q", s)
	}
	return m, len(fracPart), nil
}

// mustTemplateVersionID 计算母题版本号：sha256(canonical(spec))（契约 §2.3：
// template_version_id = 内容寻址）。仅在包 init 处理静态字面量时调用——
// canonical 对本包构造的字面量不可能失败，失败即程序员错误，panic 即停。
func mustTemplateVersionID(spec map[string]any) string {
	id, err := DigestAny(map[string]any{"dsl_version": "1", "spec": spec})
	if err != nil {
		panic("subjectmath: 母题版本号计算失败（静态字面量不应失败）: " + err.Error())
	}
	return id
}

// TemplateVersionID 计算母题版本号（sha256(canonical({dsl_version:"1", spec}))，
// 契约 §2.3 内容寻址）——语英轮生成器共用同一公式同一口径（同轴管线，
// issue #34 §二；ingest 摘要对表 ② 的判定对象）。导出面供生成器 CLI 在
// 实例补全 template_version_id 时使用，绝不另造第二套公式.
func TemplateVersionID(spec map[string]any) (string, error) {
	return DigestAny(map[string]any{"dsl_version": "1", "spec": spec})
}

// digestHex 供 registry.Entry 摘要展示外的通用小工具。
func digestHex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// deepCopy 经 JSON Number 往返复制实例（mutation 测试的载体：验证器负例
// 需要在真实实例上做单点变异）。json.Number 保住整数精度不落 float64。
func deepCopy(in *Instance) (*Instance, error) {
	buf, err := json.Marshal(in)
	if err != nil {
		return nil, fmt.Errorf("deepCopy 序列化失败: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(buf))
	dec.UseNumber()
	out := new(Instance)
	if err := dec.Decode(out); err != nil {
		return nil, fmt.Errorf("deepCopy 反序列化失败: %w", err)
	}
	return out, nil
}

// ── 小工具：stdlib 缺口的最小自实现（零依赖红线） ──

func absI64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}

func pow10(n int) int64 {
	p := int64(1)
	for i := 0; i < n; i++ {
		p *= 10
	}
	return p
}

func gcdI64(a, b int64) int64 {
	a, b = absI64(a), absI64(b)
	for b != 0 {
		a, b = b, a%b
	}
	return a
}

func allDigits(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// strings_trimLeadingZeros 规范化整数十进制串的前导零（"007"→"7"，"000"→"0"）。
func strings_trimLeadingZeros(s string) string {
	t := strings.TrimLeft(s, "0")
	if t == "" {
		return "0"
	}
	return t
}

// sortedKeys 返回 map 键的升序副本（canonical 序列化用，杜绝 map 遍历序）。
func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
