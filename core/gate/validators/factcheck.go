// 语篇事实核查验证器（Go 重锚定 T-W5-021）：判定基于**语篇来源可对账的事实**，
// 修正冻结实现的判定与阻断策略缺陷。
//
// 冻结基线（src/core/gate/validators/passage_fact_check.py）的两处缺陷：
//  1. 判定失实：规则（敏感词/暴力词）全过仍返回 review、置信 0.5——而门编排器
//     对任何 review 都不签证书，正常语篇（如「春天来了，花儿开了。」）永远过不了
//     门，C 线批量入库被结构性阻断。「永远失败的规则比没有规则更危险」：运维会
//     以为系统正常。本实现把判定改为基于可对账事实的一致性核对，干净语篇必须
//     pass（判定表见 Validate 上方，逐条显式列出）。
//  2. 阻断硬编码：`blocking = True` 类属性把阻断性焊死在验证器里。本实现不携带
//     任何阻断属性——该验证器在策略矩阵中的阻断性由链配置决定（冻结契约
//     specs/contracts/gate/policy.default.yaml 的 passage 链；Go 编排器 W6 落地时
//     读链配置），验证器只产出三值 verdict，不替策略做阻断决定。
//
// 不确定不放行（#79 review-not-pass 纪律的延续）：事实登记源未挂接/查询失败/
// 事实集合非法/语义判定面故障，一律 review 且置信 0——绝不伪造 pass。这与冻结
// 实现的「规则覆盖有限→一律 review」方向相反：review 只留给**确有可疑或无法
// 查证**的候选，干净语篇不得被 review 噪声淹没。
//
// 核对面分层（确定性先行，语义面留注入点）：
//   - number/date（一致性面，断言方向）：抽取正文机器断言（阿拉伯数字、ISO/中文
//     式日期），与登记事实集合对账。同类事实已登记而断言无一兼容 → 硬性错误 fail
//     （登记面即该类事实的唯一裁决基准，一旦登记，正文该类断言必须落集合内）；
//     同类事实零登记而正文有断言 → 无引用可对账，可疑 review；登记多于正文不罚
//     （语篇只引用部分来源事实是摘要的合法形态）。
//   - entity（引用面）：登记实体必须在正文出现（引用完整性），缺失 → 可疑 review；
//     正文出现未登记词项不罚（出现≠矛盾，误配零成本）。
//   - semantic（语义面）：机器不可核对的常识/语义事实走 FactJudge 注入面，W6 接
//     BAML harness；本卡只定义接口，不接任何 LLM 实现。Judge 未挂接而有语义事实
//     → review（无法核查，不放行）。
//
// 确定性面已知边界（刻意从简，宁 review 不误 fail）：中文数词（「三十」）、千分位
// （「1,000」）等形态不进抽取，落到无引用/无断言路径由人工或语义面兜底；日期不做
// 历法校验（2 月 30 日等交语义面）。
//
// 判定独立性（A8/X11 的结构前提）：本验证器与内容生成、查重不共享任何**判定**
// 逻辑；复用的唯一包内函数是 ContentDigest（D3 内容寻址唯一口径，只用于定位事实
// 登记键，迁移 0020 passage.content_hash 同源，不参与对账裁决）。抽取/兼容/裁决
// 三段判定面为本卡独立实现；W6 的生成器与 LLM harness 禁止反向复用本文件对账
// 函数「自证」——判定面与生成面分离，测试不得以生成逻辑构造命中。
//
// 宪法 A5/X6：核心域零学科特判，本包不 import 学科/学段包；产物类型过滤由策略
// 链（W6 编排器）决定，验证器不特判 artifact_type 是否为 passage。
package validators

import (
	"context"
	"fmt"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// FactKind 是事实引用的种类（判定面按种类分派核对方式）。
type FactKind string

const (
	// FactKindNumber 数值事实：正文数值断言与之精确对账（值相等即兼容）。
	FactKindNumber FactKind = "number"
	// FactKindDate 日期事实：正文日期断言与之对账（年/年月/年月日粒度可粗于登记值）。
	FactKindDate FactKind = "date"
	// FactKindEntity 实体引用：登记实体必须在正文出现（引用完整性面）。
	FactKindEntity FactKind = "entity"
	// FactKindSemantic 语义事实：机器不可核对，须 FactJudge 判定（W6 接 LLM）。
	FactKindSemantic FactKind = "semantic"
)

// FactClaim 是语篇来源侧登记的结构化事实引用（可对账事实）。
// Value 口径：number=十进制字面量（"3"/"3.5"）；date="1949-10-01"/"1949年10月"等
// （对账前按与正文相同的规范化器归一）；entity/semantic=原文字面。
type FactClaim struct {
	Kind  FactKind
	Value string
}

// FactSource 是已登记事实引用集合的只读读取面。
//
// key 是被检语篇的内容寻址摘要（ContentDigest 口径，迁移 0020 passage.content_hash
// 同源）——事实引用由生成管线在对账前按该键登记。W5-R 提供进程内实现
// MemoryFactSource；W6 由 DB 适配提供生产实现，禁止回退到主键列查询
// （T-W5-020 同一纪律）。
type FactSource interface {
	// ClaimsFor 返回 key 下登记的事实引用集合；无登记返回空切片而非错误
	// （「无引用可对账」是判定输入，不是基础设施故障）。
	ClaimsFor(ctx context.Context, key string) ([]FactClaim, error)
}

// FactSourceFunc 是 FactSource 的函数适配器（便于注入替身）。
type FactSourceFunc func(ctx context.Context, key string) ([]FactClaim, error)

// ClaimsFor 实现 FactSource。
func (f FactSourceFunc) ClaimsFor(ctx context.Context, key string) ([]FactClaim, error) {
	return f(ctx, key)
}

// FactJudge 是语义类事实的判定面（LLM 判定注入点）。W6 由 BAML harness 提供
// 实现；本卡不注册、不调用任何 LLM。注入的判定面与验证器同守三值纪律：
// 不确定时必须返回 VerdictReview 而非猜测 pass。
type FactJudge interface {
	// JudgeSemantic 判定正文与语义类事实引用的一致性，返回三值判定与置信
	// ∈[0,1]。只收到 Kind==FactKindSemantic 的引用子集。
	JudgeSemantic(ctx context.Context, body string, claims []FactClaim) (Verdict, float64, error)
}

// FactJudgeFunc 是 FactJudge 的函数适配器（测试替身/W6 装配前的占位注入）。
type FactJudgeFunc func(ctx context.Context, body string, claims []FactClaim) (Verdict, float64, error)

// JudgeSemantic 实现 FactJudge。
func (f FactJudgeFunc) JudgeSemantic(ctx context.Context, body string, claims []FactClaim) (Verdict, float64, error) {
	return f(ctx, body, claims)
}

// FactCheckValidatorID / FactCheckValidatorVersion 是语篇事实核查验证器的注册
// 身份。id 沿用冻结策略矩阵中的 passage_fact_check（W6 策略链按此 id 挂接）；
// 版本相对冻结 1.0.0+passage 升位 minor——判定语义修正（干净语篇 review→pass）
// 是行为变更，必须可从版本号分辨。
const (
	FactCheckValidatorID      = "passage_fact_check"
	FactCheckValidatorVersion = "1.1.0+fact-reconcile"
)

// FactCheckValidator 是语篇事实核查验证器：对正文机器断言与登记事实引用做
// 确定性对账，语义面留 Judge 注入点。
//
// 判定表（验收 #1：显式列出；按序求值，先命中先定谳）：
//  1. artifact_type 为空                         → fail   （无法定位语篇登记面，fail-closed X12）
//  2. 内容根非结构化 / 空容器                     → fail   （无真实语篇内容，fail-closed）
//  3. 正文 body 缺失或空白                        → fail   （无正文可核查，fail-closed）
//  4. 内容摘要不可计算                            → fail   （内容寻址失败，fail-closed）
//  5. 事实登记源未挂接 / 查询失败                  → review 置信 0（无法查证，不伪造 pass）
//  6. 事实引用集合含非法条目                       → review 置信 0（登记面可疑，不放行）
//  7. 正文数字/日期断言与已登记同类事实无一兼容     → fail   （硬性错误：与可对账事实冲突）
//  8. Judge 判 fail                               → fail   （判定面确认事实错误）
//  9. Judge 判 review / Judge 故障或置信越界        → review（判定面要求复核/不可信）
//
// 10. 正文断言同类事实零登记（无引用可对账）        → review（可疑：不可对账断言）
// 11. 登记实体未在正文出现（引用不完整）           → review（可疑）
// 12. 存在语义事实且 Judge 未挂接                  → review（无法核查，不放行）
// 13. 全部可核对断言与登记事实一致且无可疑         → pass   （干净语篇放行——修正冻结「规则全过仍 review」）
type FactCheckValidator struct {
	facts FactSource
	judge FactJudge
}

// NewFactCheckValidator 构造语篇事实核查验证器。src 允许为 nil（此时一律 review
// 置信 0，W6 装配 DB 适配后传入），但不允许在未装配时宣称已核查。judge 允许为
// nil（语义事实落 review；W6 接 BAML harness 后注入）。
func NewFactCheckValidator(src FactSource, judge FactJudge) *FactCheckValidator {
	return &FactCheckValidator{facts: src, judge: judge}
}

// Entry 满足注册表条目形态（registry.Entry，条目只增不改，注册冲突即失败）。
func (v *FactCheckValidator) Entry() registry.Entry {
	return registry.Entry{ID: FactCheckValidatorID, Version: FactCheckValidatorVersion}
}

// Validate 执行事实核查判定。任何路径只产出一个 Result，Evidence 为本次新建，
// 实现无共享可变状态，并发安全由 -race 套件承载。
func (v *FactCheckValidator) Validate(ctx context.Context, c Candidate) Result {
	r := Result{
		Validator:  FactCheckValidatorID,
		Version:    FactCheckValidatorVersion,
		Confidence: 1.0,
		Evidence:   make(map[string]any),
	}

	// 判定表 1：artifact_type 为空 fail-closed。
	if strings.TrimSpace(c.ArtifactType) == "" {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "artifact_type 为空，无法定位语篇事实登记面（fail-closed）"
		return r
	}
	r.Evidence["artifact_type"] = c.ArtifactType

	// 判定表 2：内容根必须结构化且非空。
	structured, empty := structuredRoot(c.Content)
	if !structured {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = fmt.Sprintf("内容根非结构化（%T）：仅接受 map/slice（fail-closed）", c.Content)
		return r
	}
	if empty {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "内容为空容器，无语篇内容可核查（fail-closed）"
		return r
	}

	// 判定表 3：正文必须存在且非空白。
	body, ok := passageBody(c.Content)
	if !ok {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "语篇正文 body 缺失或为空白，无事实可核查（fail-closed）"
		return r
	}

	// 判定表 4：内容摘要（事实登记键，D3 唯一口径）。
	key, err := ContentDigest(c.Content)
	if err != nil {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "内容摘要计算失败，无法定位事实登记键（fail-closed）"
		r.Evidence["digest_error"] = err.Error()
		return r
	}
	r.Digest = key
	r.Evidence["fact_key"] = key

	// 判定表 5：登记源未挂接 / 查询失败 → review 置信 0（review-not-pass）。
	if v.facts == nil {
		r.Verdict = VerdictReview
		r.Confidence = 0
		r.Evidence["reason"] = "未挂接事实登记源，无法对账（不放行）"
		return r
	}
	claims, err := v.facts.ClaimsFor(ctx, key)
	if err != nil {
		r.Verdict = VerdictReview
		r.Confidence = 0
		r.Evidence["reason"] = "事实登记源查询失败，无法对账（人工复核）"
		r.Evidence["source_error"] = err.Error()
		return r
	}

	// 判定表 6：事实集合非法 → 登记面可疑，review 置信 0。
	if bad := validateClaims(claims); bad != "" {
		r.Verdict = VerdictReview
		r.Confidence = 0
		r.Evidence["reason"] = "事实引用集合含非法条目，登记面可疑（不放行）"
		r.Evidence["claim_error"] = bad
		return r
	}
	r.Evidence["claims_total"] = len(claims)

	// 确定性核对面：抽取 + 对账。
	assertions := extractAssertions(body)
	r.Evidence["assertions_total"] = len(assertions)

	numClaims, dateClaims, entityClaims, semClaims := splitClaims(claims)
	contradictions, unreconciled := reconcile(assertions, numClaims, dateClaims)
	missingEntities := missingEntityRefs(body, entityClaims)

	// 判定表 7：硬性矛盾直接 fail（廉价先行：已可定谳，不再烧语义判定面）。
	if len(contradictions) > 0 {
		r.Verdict = VerdictFail
		r.Evidence["reason"] = "正文断言与登记事实矛盾（数字/日期核对面）"
		r.Evidence["contradictions"] = contradictions
		return r
	}

	// 语义判定面（仅当存在语义事实；判定表 8/9）。
	judgeConf := 1.0
	judgeReview := false
	if len(semClaims) > 0 && v.judge != nil {
		verdict, conf, err := v.judge.JudgeSemantic(ctx, body, semClaims)
		if err != nil {
			r.Verdict = VerdictReview
			r.Confidence = 0
			r.Evidence["reason"] = "语义判定面故障，无法核查语义事实（人工复核）"
			r.Evidence["judge_error"] = err.Error()
			return r
		}
		if conf < 0 || conf > 1 {
			// 置信越界 = 判定面输出不可信，按故障同级处置，不猜测。
			r.Verdict = VerdictReview
			r.Confidence = 0
			r.Evidence["reason"] = "语义判定面置信越界，判定结果不可信（不放行）"
			return r
		}
		switch verdict {
		case VerdictFail:
			r.Verdict = VerdictFail
			r.Confidence = conf
			r.Evidence["reason"] = "语义判定面确认事实错误"
			return r
		case VerdictReview:
			judgeReview = true
			judgeConf = conf
		case VerdictPass:
			judgeConf = conf
		default:
			// 判定面返回未知 verdict：与故障同级 fail-closed，不猜测。
			r.Verdict = VerdictReview
			r.Confidence = 0
			r.Evidence["reason"] = "语义判定面返回未知 verdict（不放行）"
			return r
		}
	}

	// 判定表 10/11/12：可疑与无法核查 → review（确定性发现本身置信 1）。
	if len(unreconciled) > 0 || len(missingEntities) > 0 || (len(semClaims) > 0 && v.judge == nil) || judgeReview {
		r.Verdict = VerdictReview
		r.Confidence = judgeReviewConfidence(judgeReview, judgeConf)
		switch {
		case len(unreconciled) > 0:
			r.Evidence["reason"] = "正文存在无可对账断言（该类事实零登记），转人工复核"
			r.Evidence["unreconciled"] = unreconciled
		case len(missingEntities) > 0:
			r.Evidence["reason"] = "登记实体引用未在正文出现，转人工复核"
			r.Evidence["missing_entities"] = missingEntities
		case len(semClaims) > 0 && v.judge == nil:
			r.Evidence["reason"] = "存在语义类事实但未挂接判定面，无法核查（不放行）"
			r.Evidence["semantic_unjudged"] = len(semClaims)
		default:
			r.Evidence["reason"] = "语义判定面要求人工复核"
		}
		return r
	}

	// 判定表 13：干净语篇放行。
	r.Verdict = VerdictPass
	r.Confidence = judgeConf
	r.Evidence["checked_facts"] = true
	r.Evidence["reason"] = "正文可核对断言全部与登记事实一致"
	return r
}

// passageBody 从结构化内容中取语篇正文（冻结 payload["body"] 与迁移 0020
// passage.body 同字段口径）；缺失/非字符串/空白即不可核查。
func passageBody(content any) (string, bool) {
	m, ok := content.(map[string]any)
	if !ok {
		return "", false
	}
	b, ok := m["body"].(string)
	if !ok || strings.TrimSpace(b) == "" {
		return "", false
	}
	return b, true
}

// validateClaims 报告事实集合中第一个非法条目的描述（空串 = 合法）。
// 非法登记意味着对账基准本身不可信——按 review 置信 0 处置而非跳过坏条目
// （跳过 = 用残缺基准裁决，X12 不允许）。
func validateClaims(claims []FactClaim) string {
	for i, cl := range claims {
		if strings.TrimSpace(cl.Value) == "" {
			return fmt.Sprintf("claims[%d]（%s）Value 为空", i, cl.Kind)
		}
		switch cl.Kind {
		case FactKindNumber:
			if _, ok := new(big.Rat).SetString(cl.Value); !ok {
				return fmt.Sprintf("claims[%d] Value %q 不是十进制数值", i, cl.Value)
			}
		case FactKindDate:
			if _, ok := normalizeClaimDate(cl.Value); !ok {
				return fmt.Sprintf("claims[%d] Value %q 不是可归一的日期", i, cl.Value)
			}
		case FactKindEntity, FactKindSemantic:
			// 原文字面，非空即可。
		default:
			return fmt.Sprintf("claims[%d] 未知 Kind %q", i, cl.Kind)
		}
	}
	return ""
}

// splitClaims 按种类分桶（返回切片均为登记序副本，调用方可安全持有）。
func splitClaims(claims []FactClaim) (nums, dates, entities, semantics []FactClaim) {
	for _, cl := range claims {
		switch cl.Kind {
		case FactKindNumber:
			nums = append(nums, cl)
		case FactKindDate:
			dates = append(dates, cl)
		case FactKindEntity:
			entities = append(entities, cl)
		case FactKindSemantic:
			semantics = append(semantics, cl)
		}
	}
	return nums, dates, entities, semantics
}

// finding 是一条对账发现（落 evidence 的自描述结构）。
type finding struct {
	kind  FactKind
	value string
	raw   string
}

func (f finding) evidence() map[string]any {
	return map[string]any{"kind": string(f.kind), "value": f.value, "raw": f.raw}
}

// reconcile 对正文机器断言逐条对账：
//   - 同类事实已登记且断言与其中任何一条兼容 → 一致；
//   - 同类事实已登记但断言与全部条目不兼容 → 硬性矛盾（fail 依据）；
//   - 同类事实零登记 → 无引用可对账（可疑 review 依据）。
func reconcile(assertions []assertion, nums, dates []FactClaim) (contradictions, unreconciled []map[string]any) {
	numVals := make([]*big.Rat, 0, len(nums))
	for _, cl := range nums {
		v, _ := new(big.Rat).SetString(cl.Value)
		numVals = append(numVals, v)
	}
	dateVals := make([]string, 0, len(dates))
	for _, cl := range dates {
		v, _ := normalizeClaimDate(cl.Value)
		dateVals = append(dateVals, v)
	}

	for _, a := range assertions {
		switch a.kind {
		case FactKindNumber:
			v, _ := new(big.Rat).SetString(a.value)
			hit := false
			for _, cv := range numVals {
				if v != nil && cv != nil && v.Cmp(cv) == 0 {
					hit = true
					break
				}
			}
			if len(numVals) == 0 {
				unreconciled = append(unreconciled, finding{a.kind, a.value, a.raw}.evidence())
			} else if !hit {
				contradictions = append(contradictions, finding{a.kind, a.value, a.raw}.evidence())
			}
		case FactKindDate:
			hit := false
			for _, cv := range dateVals {
				if dateCompatible(a.value, cv) {
					hit = true
					break
				}
			}
			if len(dateVals) == 0 {
				unreconciled = append(unreconciled, finding{a.kind, a.value, a.raw}.evidence())
			} else if !hit {
				contradictions = append(contradictions, finding{a.kind, a.value, a.raw}.evidence())
			}
		}
	}
	return contradictions, unreconciled
}

// missingEntityRefs 报告已登记但未在正文出现的实体引用（引用完整性面）。
func missingEntityRefs(body string, entities []FactClaim) []string {
	var missing []string
	for _, cl := range entities {
		if !strings.Contains(body, cl.Value) {
			missing = append(missing, cl.Value)
		}
	}
	return missing
}

// judgeReviewConfidence：确定性可疑发现置信 1；判定面要求复核时采信其置信
// （调用前已校验 conf ∈ [0,1]）。
func judgeReviewConfidence(judgeReviewed bool, judgeConf float64) float64 {
	if judgeReviewed {
		return judgeConf
	}
	return 1.0
}

// ────────────────────────────────────────────────────────────────────
// 正文断言抽取（确定性；只认阿拉伯数字与 ISO/中文式日期，边界见包头注释）
// ────────────────────────────────────────────────────────────────────

type assertion struct {
	kind  FactKind // FactKindNumber | FactKindDate
	value string   // 规范化值（数值十进制字面量；日期 YYYY-MM-DD[/MM/DD 粒度]）
	raw   string   // 原文片段（证据用）
}

type span struct{ s, e int }

var (
	dateFullRe = regexp.MustCompile(`([0-9]{4})[-/年]([0-9]{1,2})[-/月]([0-9]{1,2})[日号]?`)
	dateYMRe   = regexp.MustCompile(`([0-9]{4})[-/年]([0-9]{1,2})月?`)
	dateYRe    = regexp.MustCompile(`([0-9]{4})年`)
	numberRe   = regexp.MustCompile(`[0-9]+(?:\.[0-9]+)?`)
)

// extractAssertions 按日期（全→年月→年）优先抽取，命中跨度不再进数值抽取；
// 历法非法的疑似日期不占跨度，交由数值面接住（宁可可疑 review，不静默吞掉）。
func extractAssertions(body string) []assertion {
	var spans []span
	var out []assertion

	addDate := func(re *regexp.Regexp, kind FactKind, build func(m []string) (string, bool)) {
		for _, loc := range re.FindAllStringSubmatchIndex(body, -1) {
			if overlaps(spans, loc[0], loc[1]) {
				continue
			}
			m := make([]string, 0, 4)
			for g := 1; g*2 < len(loc); g++ {
				m = append(m, body[loc[g*2]:loc[g*2+1]])
			}
			val, ok := build(m)
			if !ok {
				continue
			}
			spans = append(spans, span{loc[0], loc[1]})
			out = append(out, assertion{kind: kind, value: val, raw: body[loc[0]:loc[1]]})
		}
	}
	addDate(dateFullRe, FactKindDate, func(m []string) (string, bool) { return padYMD(m[0], m[1], m[2]) })
	addDate(dateYMRe, FactKindDate, func(m []string) (string, bool) { return padYMD(m[0], m[1], "") })
	addDate(dateYRe, FactKindDate, func(m []string) (string, bool) { return padYMD(m[0], "", "") })

	for _, loc := range numberRe.FindAllStringIndex(body, -1) {
		if overlaps(spans, loc[0], loc[1]) {
			continue
		}
		raw := body[loc[0]:loc[1]]
		out = append(out, assertion{kind: FactKindNumber, value: raw, raw: raw})
	}
	return out
}

func overlaps(spans []span, s, e int) bool {
	for _, sp := range spans {
		if s < sp.e && sp.s < e {
			return true
		}
	}
	return false
}

// padYMD 校验并归一日期为 YYYY-MM-DD / YYYY-MM / YYYY（month∈[1,12]，day∈[1,31]；
// 不做历法深校验，边界见包头注释）。
func padYMD(y, m, d string) (string, bool) {
	if len(y) != 4 {
		return "", false
	}
	if _, err := strconv.Atoi(y); err != nil {
		return "", false
	}
	out := y
	if m != "" {
		mi, err := strconv.Atoi(m)
		if err != nil || mi < 1 || mi > 12 {
			return "", false
		}
		out += "-" + fmt.Sprintf("%02d", mi)
	}
	if d != "" {
		di, err := strconv.Atoi(d)
		if err != nil || di < 1 || di > 31 {
			return "", false
		}
		out += "-" + fmt.Sprintf("%02d", di)
	}
	return out, true
}

// dateCompatible 报告断言粒度与登记粒度是否相容：相等，或一方为另一方的前缀
// 粒度（正文「1949年」与登记 1949-10-01 相容；反之正文细于登记亦相容）。
func dateCompatible(asserted, claimed string) bool {
	if asserted == claimed {
		return true
	}
	return strings.HasPrefix(claimed, asserted+"-") || strings.HasPrefix(asserted, claimed+"-")
}

// normalizeClaimDate 把登记日期值按与正文相同的规范化器归一（整值匹配）。
func normalizeClaimDate(v string) (string, bool) {
	v = strings.TrimSpace(v)
	if val, ok := padYMD(v, "", ""); ok {
		return val, true
	}
	if m := matchFull(dateFullRe, v); m != nil {
		return padYMD(m[1], m[2], m[3])
	}
	if m := matchFull(dateYMRe, v); m != nil {
		return padYMD(m[1], m[2], "")
	}
	if m := matchFull(dateYRe, v); m != nil {
		return padYMD(m[1], "", "")
	}
	return "", false
}

func matchFull(re *regexp.Regexp, s string) []string {
	m := re.FindStringSubmatch(s)
	if m == nil || m[0] != s {
		return nil
	}
	return m
}

// ────────────────────────────────────────────────────────────────────
// 进程内事实登记源（测试与非 PG 场景；W6 由 DB 适配替换）
// ────────────────────────────────────────────────────────────────────

// MemoryFactSource 是 FactSource 的进程内实现：按内容摘要键分桶，读写用
// RWMutex 保护（-race 并发套件覆盖）。Register 不做合法性校验——非法条目
// 照实进入判定面（验证器按判定表 6 处置），登记面不吞错。
type MemoryFactSource struct {
	mu      sync.RWMutex
	buckets map[string][]FactClaim
}

// NewMemoryFactSource 构造空的进程内事实登记源。
func NewMemoryFactSource() *MemoryFactSource {
	return &MemoryFactSource{buckets: make(map[string][]FactClaim)}
}

// Register 登记一组事实引用（对应事实入库事务后的可见性；可多次追加）。
func (m *MemoryFactSource) Register(key string, claims ...FactClaim) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.buckets[key] = append(m.buckets[key], claims...)
}

// ClaimsFor 返回 key 下登记的事实引用副本（防御性拷贝，并发安全）。
func (m *MemoryFactSource) ClaimsFor(_ context.Context, key string) ([]FactClaim, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	src := m.buckets[key]
	if len(src) == 0 {
		return []FactClaim{}, nil
	}
	out := make([]FactClaim, len(src))
	copy(out, src)
	return out, nil
}

// Len 返回某键下已登记事实数（测试与可观测用）。
func (m *MemoryFactSource) Len(key string) int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.buckets[key])
}
