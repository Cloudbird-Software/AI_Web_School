package subjectmath

import (
	"fmt"
	"sort"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// generator.go —— 母题生成器接口与注册表。
//
// 注册方式参照 packs/packs.go 现有形态：学科包只复用注册表（宪法 D4/铁律 3），
// 这里直接复用 registry.Registry[T] 泛型注册表装生成器条目——不私造第二套
// 登记机制，core 零改动（packs 框架尚无“生成器”类别，本包按最小接口自持
// 一个包级注册表；后续若框架升级为平台级类别，仅需换装载点，接口形态不变）。

// Generator 是数学母题生成器契约：**纯函数式**——实例内容完全由母题版本 +
// 空间索引决定，索引内部的呈现随机性（选项顺序/题干变体）由索引派生的独立
// rand 流决定。这样种子只控制 batch 层“抽哪些索引”，可回放语义无歧义：
//   - 同 seed 同输出（采样序一致）
//   - 同索引同实例（内容寻址级稳定，跨 seed 重叠索引产物逐字节相同）
type Generator interface {
	// Entry 母题版本化条目（一切条目都是版本化资产，§八）。
	Entry() registry.Entry
	// Spec 母题定义的可读子集（item-model.md §2.3 六大块中本管线的落点：
	// objective / slots / variation_axes / presentation / answer_program /
	// distractor_rules 的静态描述）。template_version_id = sha256(canonical(本值))
	// 在构造期算好。
	Spec() map[string]any
	// Size 参数空间规模：可枚举互异参数点总数（结构互异的来源）。
	Size() int
	// Instance 由空间索引构造实例。index ∉ [0,Size) 返回错误。
	Instance(index int) (*Instance, error)
}

// generators 本学科包的生成器注册表（Register 幂等失败即重复 id——禁止静默覆盖）。
var generators = registry.New[Generator]()

// Register 登记生成器；重复模板 id 即错误。
func Register(g Generator) error { return generators.Register(g.Entry().ID, g) }

// Get 按 id 取生成器。
func Get(id string) (Generator, bool) { return generators.Get(id) }

// IDs 返回全部已注册模板 id（升序），供 mathgen -templates all 枚举。
func IDs() []string {
	ids := make([]string, 0, generators.Len())
	for _, g := range builtinGenerators {
		ids = append(ids, g.Entry().ID)
	}
	sort.Strings(ids)
	return ids
}

// tplMeta 三个母题共享的元数据壳：条目 + spec + 预计算版本号。
type tplMeta struct {
	entry         registry.Entry
	spec          map[string]any
	templateVerID string // sha256:<hex>（构造期由 mustTemplateVersionID 算出）
}

func newTplMeta(id, version string, spec map[string]any) tplMeta {
	return tplMeta{
		entry:         registry.Entry{ID: id, Version: version},
		spec:          spec,
		templateVerID: mustTemplateVersionID(spec),
	}
}

func (m tplMeta) Entry() registry.Entry { return m.entry }
func (m tplMeta) Spec() map[string]any  { return m.spec }
func (m tplMeta) versionID() string     { return m.templateVerID }

// checkIndex 统一的空间边界断言（三个实现共用一行防御）。
func (m tplMeta) checkIndex(index, size int) error {
	if index < 0 || index >= size {
		return fmt.Errorf("空间索引越界：index=%d size=%d template=%s", index, size, m.entry.ID)
	}
	return nil
}
