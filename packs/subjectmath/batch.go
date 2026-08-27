package subjectmath

import (
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"sort"
	"strings"
)

// batch.go —— 批量管线：采样 → 确定性验证 → 结构互异去重 → 记录。
//
// 采样策略：以 (seed, fnv(template_id)) 派生的 rand/v2 PCG 流对参数空间
// 做一次确定性洗牌，按洗牌序枚举参数点，直到凑满 N 个「过验证器且摘要
// 互异」的实例。拒绝原因全量计数进报告（W6 S2 产能报告口径）。
//
// 结构互异是硬断言：一旦出现 content 摘要碰撞（= 参数空间折叠缺陷），
// 立即整体失败——唯一率 100% 由 AssertPairwiseDistinct 背书，绝不允许
// 静默跳过后宣称通过。

// Options 单模板批量选项。
type Options struct {
	TemplateID string
	N          int    // 目标合格实例数（如 30）
	Seed       uint64 // 批种子；同 seed 同输出可回放
}

// Record 一条合格实例 + 其空间索引与 content 摘要（JSONL 行载体）。
type Record struct {
	*Instance
	SpaceIndex    int    `json:"space_index"`
	ContentDigest string `json:"content_digest"`
}

// Report 单模板批次汇总（stdout 报告与回归断言共用）。
type Report struct {
	TemplateID      string         `json:"template_id"`
	TemplateVersion string         `json:"template_version_id"`
	Seed            uint64         `json:"seed"`
	SpaceSize       int            `json:"space_size"`
	RequestedN      int            `json:"requested_n"`
	Generated       int            `json:"generated"` // 构造成功数
	Accepted        int            `json:"accepted"`  // 过验证器且互异数
	Attempts        int            `json:"attempts"`  // 总尝试次数
	UniqueRate      float64        `json:"unique_rate"`
	DistinctOK      bool           `json:"distinct_ok"`
	Rejected        map[string]int `json:"rejections"` // 拒绝原因 → 计数
	DurationHintMs  int64          `json:"-"`
}

// Run 对单模板执行批量生成。
// 错误语义：模板未知 / N 超空间规模 / 配额未达成 / 摘要碰撞 都返回错误；
// 已累计的 report 一并带回供上层打印部分结果。
func Run(opts Options) ([]Record, *Report, error) {
	g, ok := Get(opts.TemplateID)
	if !ok {
		return nil, nil, fmt.Errorf("未注册母题 %q（可用：%s）", opts.TemplateID, strings.Join(IDs(), ", "))
	}
	return runBatch(g, opts)
}

// runBatch 是 Run 的内核（不查注册表）：测试用它注入合成母题，
// 直接驱动「碰撞必须显式失败」等红线断言，而不污染包级注册表。
func runBatch(g Generator, opts Options) ([]Record, *Report, error) {
	if opts.N <= 0 {
		return nil, nil, fmt.Errorf("N 必须为正整数，得 %d", opts.N)
	}
	size := g.Size()
	if opts.N > size {
		return nil, nil, fmt.Errorf(
			"母题 %s 参数空间(%d)不足 N=%d 个互异实例——结构互异不可达即拒绝",
			opts.TemplateID, size, opts.N)
	}

	rep := &Report{
		TemplateID:      g.Entry().ID,
		TemplateVersion: g.Entry().Version,
		Seed:            opts.Seed,
		SpaceSize:       size,
		RequestedN:      opts.N,
		Rejected:        map[string]int{},
	}

	rng := rand.New(rand.NewPCG(opts.Seed, streamKey(opts.TemplateID)))
	perm := rng.Perm(size)

	records := make([]Record, 0, opts.N)
	seen := make(map[string]int, opts.N) // digest -> 第一次出现的位置
	for _, idx := range perm {
		if len(records) == opts.N {
			break
		}
		rep.Attempts++
		inst, err := g.Instance(idx)
		if err != nil {
			rep.Rejected["construct:"+errShort(err)]++
			continue
		}
		inst.Lineage["seed"] = int64(opts.Seed) // 回放证据注入谱系（契约 §5.2 seed）
		digest, err := ContentDigest(inst.Content)
		if err != nil {
			rep.Rejected["digest:"+errShort(err)]++
			continue
		}
		if first, dup := seen[digest]; dup {
			return records, rep, fmt.Errorf(
				"H-W6-1 结构互异破坏：索引 %d 与 %d 的 content 摘要相同 (%s)——参数空间存在折叠，拒绝整批",
				first, idx, digest)
		}
		if verr := Validate(inst); verr != nil {
			rep.Rejected["validate:"+rejectionLabel(verr)]++
			continue
		}
		seen[digest] = idx
		records = append(records, Record{Instance: inst, SpaceIndex: idx, ContentDigest: digest})
	}

	rep.Generated = rep.Attempts - totalRejections(rep.Rejected)
	rep.Accepted = len(records)
	if len(records) < opts.N {
		return records, rep, fmt.Errorf(
			"配额未达成：母题 %s 需 %d 实例，仅 %d 合格（空间已扫尽）；拒绝分布=%v",
			opts.TemplateID, opts.N, len(records), summarizeRejections(rep.Rejected))
	}
	digests := make([]string, len(records))
	for i := range records {
		digests[i] = records[i].ContentDigest
	}
	if err := AssertPairwiseDistinct(digests); err != nil {
		return records, rep, err
	}
	rep.UniqueRate = float64(len(seen)) / float64(rep.Accepted)
	rep.DistinctOK = true
	return records, rep, nil
}

// streamKey 模板 id 的流键：不同母题即便同种子也走独立随机流。
func streamKey(templateID string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(templateID))
	return h.Sum64()
}

// 四类哨兵 → 报告标签。
func rejectionLabel(err error) string {
	switch {
	case errors.Is(err, ErrShapeInvalid):
		return "shape-invalid"
	case errors.Is(err, ErrAnswerMismatch):
		return "answer-mismatch"
	case errors.Is(err, ErrFormatViolation):
		return "format-violation"
	case errors.Is(err, ErrConsistencyBroken):
		return "consistency-broken"
	default:
		return "other"
	}
}

func errShort(err error) string { return rejectionLabel(err) }

func totalRejections(m map[string]int) int {
	t := 0
	for _, c := range m {
		t += c
	}
	return t
}

func summarizeRejections(m map[string]int) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", k, m[k]))
	}
	return strings.Join(parts, ",")
}
