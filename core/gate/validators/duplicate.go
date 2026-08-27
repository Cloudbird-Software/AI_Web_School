// 查重验证器（T-W5-020 核心语义）：对真实内容摘要做唯一性判定。
//
// 判定面是 (artifact_type, content_digest) 的已发布登记视图（DigestSource），
// 首个精确命中即拒——修复冻结实现「计算 _canonical_hash 却查
// item_version_id 主键列」的失实路径。完全相同内容第二次入库必拒；
// 仅参数不同的合法变式内容不同 → 摘要不同 → 不误判。
// 近重复（n-gram shingle Jaccard）仅留接口骨架与阈值常量，实现留 W6。
package validators

import (
	"context"
	"fmt"
	"strings"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// Verdict 是验证判定的三值语义（与 gate_run.verdict 对齐，冻结契约不变）：
// pass 通过；fail 阻断失败；review 人工复核（编排器不因 review 放行）。
type Verdict string

const (
	VerdictPass   Verdict = "pass"
	VerdictFail   Verdict = "fail"
	VerdictReview Verdict = "review"
)

// Candidate 是送入校验门的候选实例查重输入。
type Candidate struct {
	// ArtifactType 产物类型（item/material/corpus/...）；空即 fail-closed。
	ArtifactType string
	// Content 结构化内容：根必须是 map[string]any 或 []any
	// （JSON 解码形态）。nil / 空容器 / 标量根一律 fail-closed。
	Content any
}

// Result 是验证器统一返回契约的最小 Go 面（对应冻结 ValidatorResult 中
// 编排与落库所需字段：verdict/evidence/confidence/validator_id/version；
// cost_ms 由编排器计时，不在本卡范围）。
type Result struct {
	Validator  string         // validator_id（注册表 id）
	Version    string         // validator_version
	Verdict    Verdict        // 判定三值
	Confidence float64        // [0,1]；0 = 无法查证
	Digest     string         // 候选内容的规范化摘要；不可计算时为空
	HitDigest  string         // 命中的已登记摘要（first-hit）；未判重时为空
	Evidence   map[string]any // 自描述证据（落 gate_run.evidence 口径）
}

// DigestSource 是已登记内容摘要集合的只读读取面。
//
// W5-R 提供进程内实现 MemoryDigestSource（测试与非 PG 场景）；
// W6 由 DB 适配实现并接 pgx：按 artifact_type 映射到对应 *_version 表的
// content_digest 列（带索引，迁移 0028 同名语义），禁止回退到主键列查询。
//
// first-hit 即拒：首个命中立即终止判定（对精确哈希而言命中即同内容，
// 无需继续扫描其余登记项）。
type DigestSource interface {
	PublishedHit(ctx context.Context, artifactType, digest string) (bool, error)
}

// DuplicateValidatorID / DuplicateValidatorVersion 是平台查重验证器的注册身份。
const (
	DuplicateValidatorID      = "duplicate"
	DuplicateValidatorVersion = "1.0.0+real-digest"
)

// 近重复（近似相似度）扩展点的 W6 默认参数骨架。本卡 non_goals 明示
// 语义相似度查重不做实现；常量仅锚定未来口径，当前无任何调用点。
const (
	// DefaultShingleK 是字符级 n-gram shingle 宽度的 W6 默认值。
	DefaultShingleK = 5
	// NearDuplicateThreshold 是 Jaccard 相似度 ≥ 该值即视为近重复的 W6 默认阈值。
	NearDuplicateThreshold = 0.90
)

// NearDuplicateChecker 是近似查重的扩展点接口骨架（W6 落地 n-gram/shingle
// Jaccard 实现）。W5-R 平台不提供、也不注册任何实现——在实现落地并通过
// 契约测试前，禁止任何代码宣称近似查重已强制（A8/X11）。
type NearDuplicateChecker interface {
	Entry() registry.Entry
	// NearDuplicate 返回两份规范化文本（CanonicalJSON 输出）的相似度 ∈ [0,1]。
	NearDuplicate(canonicalA, canonicalB string) (float64, error)
}

// DuplicateValidator 是平台通用查重验证器：计算候选内容的规范化摘要，
// 与已登记摘要集合做精确比对，first-hit 即拒。
//
// verdict 规则：
//   - fail：摘要命中已登记集合（重复内容入库，第一铁律面：不进已发布池）。
//   - fail：artifact_type 为空 / 内容为 nil、空容器或非结构化根 /
//     摘要不可计算（fail-closed，X12：宁可拒绝放行）。
//   - review：未挂接 DigestSource 或源查询失败（无法查证时不伪造 pass）。
//   - pass：无命中（含合法参数变式）。
type DuplicateValidator struct {
	source DigestSource
}

// NewDuplicateValidator 构造查重验证器；src 允许为 nil（此时一律 review，
// W6 装配 DB 适配后传入），但不允许在未装配时宣称已查重。
func NewDuplicateValidator(src DigestSource) *DuplicateValidator {
	return &DuplicateValidator{source: src}
}

// Entry 满足注册表条目形态（registry.Entry，与作答交互/评分器同一纪律：
// 条目只增不改，注册冲突即失败，禁止静默覆盖）。
func (d *DuplicateValidator) Entry() registry.Entry {
	return registry.Entry{ID: DuplicateValidatorID, Version: DuplicateValidatorVersion}
}

// Validate 执行查重判定。任何路径只产出一个 Result，Evidence 为本次新建，
// 调用方可安全并发持有（并发安全由 -race 套件承载）。
func (d *DuplicateValidator) Validate(ctx context.Context, c Candidate) Result {
	r := Result{
		Validator:  DuplicateValidatorID,
		Version:    DuplicateValidatorVersion,
		Confidence: 1.0,
		Evidence:   make(map[string]any),
	}

	if strings.TrimSpace(c.ArtifactType) == "" {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "artifact_type 为空，无法定位查重登记面（fail-closed）"
		return r
	}
	r.Evidence["artifact_type"] = c.ArtifactType

	structured, empty := structuredRoot(c.Content)
	if !structured {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = fmt.Sprintf("内容根非结构化（%T）：仅接受 map/slice（fail-closed）", c.Content)
		return r
	}
	if empty {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "内容为空容器，无真实内容可查重（fail-closed）"
		return r
	}

	digest, err := ContentDigest(c.Content)
	if err != nil {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "内容摘要计算失败（fail-closed）"
		r.Evidence["digest_error"] = err.Error()
		return r
	}
	r.Digest = digest
	r.Evidence["canonical_hash"] = digest

	if d.source == nil {
		r.Verdict = VerdictReview
		r.Confidence = 0
		r.Evidence["reason"] = "未挂接摘要登记源，无法查证重复（不放行）"
		return r
	}

	hit, err := d.source.PublishedHit(ctx, c.ArtifactType, digest)
	if err != nil {
		r.Verdict = VerdictReview
		r.Confidence = 0
		r.Evidence["reason"] = "摘要登记源查询失败，无法查证重复（人工复核）"
		r.Evidence["source_error"] = err.Error()
		return r
	}
	if hit {
		// first-hit 即拒：精确哈希命中即同内容，重复题不得进已发布池。
		r.Verdict = VerdictFail
		r.HitDigest = digest
		r.Evidence["hit"] = true
		r.Evidence["reason"] = "发现完全相同内容的已登记记录，判重拒绝"
		return r
	}

	r.Verdict = VerdictPass
	r.Evidence["checked_published"] = true
	return r
}

// structuredRoot 报告 v 是否为结构化根及其是否为空容器。
// 仅 map[string]any / []any 视为结构化（JSON 解码形态）。
func structuredRoot(v any) (structured bool, empty bool) {
	switch x := v.(type) {
	case map[string]any:
		return true, len(x) == 0
	case []any:
		return true, len(x) == 0
	default:
		return false, false
	}
}

// MemoryDigestSource 是 DigestSource 的进程内实现：按 artifact_type 分桶的
// 摘要集合，读写用 RWMutex 保护（-race 并发套件覆盖）。
type MemoryDigestSource struct {
	mu      sync.RWMutex
	buckets map[string]map[string]struct{}
}

// NewMemoryDigestSource 构造空的进程内摘要登记源。
func NewMemoryDigestSource() *MemoryDigestSource {
	return &MemoryDigestSource{buckets: make(map[string]map[string]struct{})}
}

// Publish 登记一条已发布摘要（对应发布事务落库后的可见性）。
func (m *MemoryDigestSource) Publish(artifactType, digest string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	b := m.buckets[artifactType]
	if b == nil {
		b = make(map[string]struct{})
		m.buckets[artifactType] = b
	}
	b[digest] = struct{}{}
}

// PublishedHit 报告 (artifactType, digest) 是否已有登记；first-hit 即拒。
func (m *MemoryDigestSource) PublishedHit(_ context.Context, artifactType, digest string) (bool, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	_, hit := m.buckets[artifactType][digest]
	return hit, nil
}

// Len 返回某产物类型下已登记摘要数（测试与可观测用，参照 registry.Registry.Len）。
func (m *MemoryDigestSource) Len(artifactType string) int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.buckets[artifactType])
}

// DigestSourceFunc 是 DigestSource 的函数适配器（DB 适配未就绪时便于注入替身）。
type DigestSourceFunc func(ctx context.Context, artifactType, digest string) (bool, error)

// PublishedHit 实现 DigestSource。
func (f DigestSourceFunc) PublishedHit(ctx context.Context, artifactType, digest string) (bool, error) {
	return f(ctx, artifactType, digest)
}
