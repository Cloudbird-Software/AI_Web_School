package scoring

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// 评分执行核心域（T-W5-016：评分链路可回放）。
//
// 铁律（D4）：评分器只能从注册表取——Runner 不认任何裸评分器实现，未注册的
// id 在这里结构不可达。每次评分装配一条可回放 scoring_trace（D6/D10）：十年后
// 必须能回答「当时是哪个评分器哪个版本、哪个模型哪版提示词、什么输入、给的分
// 依据是什么、跑了多久」。trace 以 map 形态交付，落账走 core/events.Writer
// 的 response_event.scoring_trace 列（append-only，D1）；契约 §3 明示该对象
// 可扩展只增不改，故 D10 回放字段全部为加性 JSON 键，零 DDL.

// 哨兵错误：调用方按 errors.Is 分支处理（错误文本不含作答与参数原文，X3）.
var (
	// ErrInvalidInput 表示评分入参违反注册表契约声明（缺必备键/形态不符/
	// 不可序列化）——禁止静默转换是验收 #2 的字面语义：缺字段与类型不符
	// 必须在出分前明确失败，而不是被鸭子转换吞成错判.
	ErrInvalidInput = errors.New("scoring: 评分入参违反注册表契约")

	// ErrScorerNotFound 表示请求的评分器未注册（含学科包评分器未装配）。
	// 评分器只能来自注册表（D4），未注册 id 不可达、不可临时 substitute.
	ErrScorerNotFound = errors.New("scoring: 评分器未注册")
)

// RunInput 是一次评分请求（Python 冻结实现 run_scorer 的
// (item_version.scoring_ref, response, params) 三态收敛为显式结构；
// 作答原文与评分参数随 item_version 版本化，版本身份由调用方在
// response_event.item_version_id 列承载，不重复入 trace）.
type RunInput struct {
	// ScorerID 注册表内的评分器 id（scorer.yaml 注册键，D4）.
	ScorerID string
	// Answer 作答原文（registry.Scorer.Score 的作答面；原文只落
	// response_event.raw_payload，trace 只留摘要——职责分离）.
	Answer string
	// Params 评分参数（答案程序/量规/关键点表，scorer_params，随
	// item_version 版本化）；必备键集由条目 InputSchema 声明.
	Params map[string]any
}

// Run 是一次评分执行的产出（落账前返回给调用方）.
type Run struct {
	// ScorerID/ScorerVersion 本次出分的评分器身份（与 Trace 内同值，链式引用）.
	ScorerID      string
	ScorerVersion string
	// Result 判定结果（已过 registry.ValidateResult 契约校验）.
	Result registry.ScoreResult
	// Trace 可回放 scoring_trace（契约 §3 扩展面 + D10 回放字段）；值即
	// response_event.scoring_trace 列的落账原文，调用方不得再改写.
	Trace map[string]any
	// DurationMS 评分执行耗时（毫秒；与 response_event.duration_ms 的作答
	// 耗时是两个维度——本值度量评分链路自身，健康度监控用）.
	DurationMS float64
}

// Runner 是评分执行服务：注册表取评分器 → 入参契约校验 → 执行 → 结果契约
// 校验 → 装配可回放 trace。无状态、可并发；时钟可注入（确定性测试）.
type Runner struct {
	table *registry.ScorerTable
	now   func() time.Time
}

// NewRunner 构造评分执行器；注册表缺失直接报错——无注册表的评分执行器就是
// 违宪产物（D4：评分器只能来自注册表），从构造期堵死.
func NewRunner(table *registry.ScorerTable) (*Runner, error) {
	if table == nil {
		return nil, fmt.Errorf("%w: 评分器注册表未注入（D4）", ErrInvalidInput)
	}
	return &Runner{table: table, now: time.Now}, nil
}

// SetClock 是测试注入点（生产留零值）：固定时钟让 duration_ms 确定，
// 同输入同版本的两次评分 trace 可逐字节比对（可回放断言的前提）.
func (r *Runner) SetClock(now func() time.Time) { r.now = now }

// Run 执行一次评分并装配可回放 trace。
//
// 预期失败面：入参契约违例 → ErrInvalidInput；评分器未注册 → ErrScorerNotFound；
// 评分器执行失败/结果违例 → 对应 wrap 链（执行失败的错误文本原样上抛由调用方
// 处置，本层不吞不转译）。失败路径零 trace 产出——残缺评分不落账.
func (r *Runner) Run(ctx context.Context, in RunInput) (*Run, error) {
	start := r.now()

	if in.ScorerID == "" {
		return nil, fmt.Errorf("%w: scorer_id 必填", ErrInvalidInput)
	}
	// 输入摘要先行：含不可 JSON 化值的入参在此显式失败（而非出分后补账失败）.
	digest, err := inputDigest(in)
	if err != nil {
		return nil, fmt.Errorf("%w: 输入摘要计算失败（入参含不可序列化值，禁止静默豁免）: %w", ErrInvalidInput, err)
	}

	scorer, spec, ok := r.table.Get(in.ScorerID)
	if !ok {
		return nil, fmt.Errorf("%w: %q（评分器只能来自注册表，D4）", ErrScorerNotFound, in.ScorerID)
	}
	if err := validateParams(spec, in.Params); err != nil {
		return nil, err
	}

	res, err := scorer.Score(ctx, in.Answer, in.Params)
	if err != nil {
		return nil, fmt.Errorf("scoring: scorer %s 执行失败: %w", in.ScorerID, err)
	}
	if err := registry.ValidateResult(spec, res); err != nil {
		return nil, fmt.Errorf("scoring: scorer %s: %w", in.ScorerID, err)
	}

	durMS := float64(r.now().Sub(start)) / float64(time.Millisecond)
	return &Run{
		ScorerID:      spec.Entry.ID,
		ScorerVersion: spec.Entry.Version,
		Result:        res,
		Trace:         buildTrace(spec, res, digest, durMS),
		DurationMS:    durMS,
	}, nil
}

// validateParams 按条目声明面校验入参结构（验收 #2）：缺必备键/形态不符皆
// 明确报错——「任意作答」类条目跳过键表校验（其契约就是无必备键）.
func validateParams(spec registry.ScorerSpec, params map[string]any) error {
	if spec.AcceptsAnyInput {
		return nil
	}
	for key, kind := range spec.InputSchema {
		v, ok := params[key]
		if !ok {
			return fmt.Errorf("%w: scorer %s 入参缺必备键 %q（禁止静默转换）", ErrInvalidInput, spec.Entry.ID, key)
		}
		if !kindMatch(kind, v) {
			return fmt.Errorf("%w: scorer %s 入参 %q 形态应为 %s", ErrInvalidInput, spec.Entry.ID, key, kind)
		}
	}
	return nil
}

// kindMatch 报告 v 是否符合声明形态（JSON 通道常态形态 + Go 字面量兼容）.
func kindMatch(k registry.ParamKind, v any) bool {
	switch k {
	case registry.KindObject:
		_, ok := v.(map[string]any)
		return ok
	case registry.KindArray:
		_, ok := v.([]any)
		return ok
	case registry.KindString:
		_, ok := v.(string)
		return ok
	case registry.KindNumber:
		switch v.(type) {
		case float64, int, int64: // JSON 解码数字=float64；进程内字面量 int/int64
			return true
		}
		return false
	default:
		return false
	}
}

// buildTrace 装配可回放 scoring_trace（Python 冻结实现 build_scoring_trace
// 语义同构 + D10 回放字段）：
//   - 契约 §5 必备三键：scorer_id / scorer_version / confidence.scoring；
//   - dimension_scores.correct：客观题 0|1 口径（Python 同构；CTT 标定取数位）；
//   - process.correct：复习排程 derive_correctness 的判定依据键（验收 #1，
//     Python T-W4-048 补写语义）；total 为评分器聚合分；
//   - input_digest：输入摘要（sha256 前 16 hex，与 core/ai 台账 PromptHash
//     同宽同构），覆盖 scorer_id+作答+参数——同输入必同摘要的回放定位键；
//   - duration_ms：评分执行耗时；
//   - AI 评分（非确定性）追加 model / model_version / prompt_version 三键
//     （D10：十年后可定位当时是哪个模型哪版提示词给的分）.
func buildTrace(spec registry.ScorerSpec, res registry.ScoreResult, digest string, durMS float64) map[string]any {
	correct := 0.0
	if res.Correct {
		correct = 1.0
	}
	trace := map[string]any{
		"scorer_id":      spec.Entry.ID,
		"scorer_version": spec.Entry.Version,
		"dimension_scores": map[string]any{
			"correct": correct,
			"total":   res.Score,
		},
		"process":      map[string]any{"correct": res.Correct},
		"confidence":   map[string]any{"scoring": res.Confidence},
		"input_digest": digest,
		"duration_ms":  durMS,
	}
	if !spec.Deterministic {
		trace["model"] = res.Model
		trace["model_version"] = res.ModelVersion
		trace["prompt_version"] = spec.PromptVersion
	}
	return trace
}

// inputDigest 计算输入摘要：对 scorer_id+作答+参数的 canonical JSON（encoding/json
// 对 map 键排序输出，确定性）取 sha256 前 16 hex——与 core/ai 台账 PromptHash
// 同宽同构。作答原文不入 trace（在 raw_payload），摘要足以定位与比对重放输入.
func inputDigest(in RunInput) (string, error) {
	blob, err := json.Marshal(map[string]any{
		"scorer_id": in.ScorerID,
		"answer":    in.Answer,
		"params":    in.Params,
	})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(blob)
	return hex.EncodeToString(sum[:8]), nil
}
