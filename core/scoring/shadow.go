package scoring

// 影子模式（Python 冻结实现 src/core/scoring/shadow_mode.py 的 Go 移植，
// T-W4-020 语义同构）：架构 v2 §4.5「AI 维度量规评分器」上线四步之第二步
// 「影子运行」——对模拟作答跑 ai_rubric 评分，结果写入 shadow_score 账形态
// 的结构，**不触碰** response_event 主 score 字段（验收①：不影响真实分数）。
//
// 账语义（D1/D8）：
//   - shadow_score 是 append-only 独立账：影子评分的作答可能是合成的基准
//     数据集，无 event_id 可绑（不复用 score_run——语义不同，Python 同构）；
//   - 本包不接 DB：持久化经 ShadowStore 接口注入，MemoryShadowStore 是
//     进程内实现；「影子运行不碰真实分数」由结构保证——本文件没有任何
//     response_event/score 账的写路径；
//   - 一致率是内部验证指标，不输出排名（宪法 D8）。
//
// ai 台账对齐：影子运行的 AI 调用与在线评分共用同一 Caller 注入面
// （core/ai Caller；生产由装配层绑定带 D10 台账的执行面），不存在绕过
// 总线的影子直连通道.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
)

// 影子模式常量（Python shadow_mode.py 同值）.
const (
	// DefaultConsistencyTolerance 一致性判定阈值（验收③：5 分制逐维偏差
	// ≤1 分视为一致）.
	DefaultConsistencyTolerance = 1.0

	// DefaultConsistencyRateThreshold 一致率门槛（验收③：整体一致率 ≥70%）.
	DefaultConsistencyRateThreshold = 0.70

	// DefaultDatasetID 默认基准数据集 id.
	DefaultDatasetID = "shadow-baseline-v1"

	// 影子账一致性状态三值（shadow_score.consistency_status 域）.
	StatusPending      = "pending"
	StatusConsistent   = "consistent"
	StatusInconsistent = "inconsistent"
)

// ErrDuplicateShadowID 影子账 append-only 的执行面投影（同 shadow_id 重复
// 写入被拒；重判应生成新 shadow_id——content-addressed，不覆盖历史评分）.
var ErrDuplicateShadowID = errors.New("scoring/shadow: 影子账同 shadow_id 重复写入（append-only）")

// ShadowScoreRecord 是影子评分记录（shadow_score 表一行的账形态结构）.
type ShadowScoreRecord struct {
	// ShadowID 行 id（dataset|case|rubric|作答摘要 的 sha256 派生——确定性，
	// 便于重放）.
	ShadowID    string `json:"shadow_id"`
	DatasetID   string `json:"dataset_id"`
	CaseID      string `json:"case_id"`
	RubricID    string `json:"rubric_id"`
	GradeBand   string `json:"grade_band"`
	WritingType string `json:"writing_type"`
	// ResponseText 被评分作答文本（合成数据/脱敏文本，D7 由注入面保证）.
	ResponseText string `json:"response_text"`
	// ResponseTextDigest 作答文本 sha256（"sha256:" 前缀，dedup/replay 键）.
	ResponseTextDigest string `json:"response_text_digest"`
	// AIScore AI 量规评分结果（ai_score_payload 的结构形态）.
	AIScore *AIRubricScore `json:"ai_score"`
	// HumanScore 人工量规结论（可空；基准场景携带）.
	HumanScore map[string]any `json:"human_score"`
	// ConsistencyStatus pending/consistent/inconsistent.
	ConsistencyStatus string `json:"consistency_status"`
}

// ShadowStore 是影子账的持久化面（append-only；不接 DB——生产装配方以
// shadow_score 表实现，测试用 MemoryShadowStore）.
type ShadowStore interface {
	// Record 追加一条影子评分记录；同 ShadowID 重复写入必须报错
	// （append-only 语义：历史影子评分永不被覆盖）.
	Record(ctx context.Context, rec ShadowScoreRecord) error
}

// MemoryShadowStore 是进程内影子账（读写锁保护，-race 安全）.
type MemoryShadowStore struct {
	mu   sync.RWMutex
	seen map[string]bool
	recs []ShadowScoreRecord
}

// NewMemoryShadowStore 构造空影子账.
func NewMemoryShadowStore() *MemoryShadowStore {
	return &MemoryShadowStore{seen: make(map[string]bool)}
}

// Record 实现 ShadowStore：同 ShadowID 拒绝（ErrDuplicateShadowID）.
func (s *MemoryShadowStore) Record(_ context.Context, rec ShadowScoreRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.seen[rec.ShadowID] {
		return fmt.Errorf("%w: %s", ErrDuplicateShadowID, rec.ShadowID)
	}
	s.seen[rec.ShadowID] = true
	s.recs = append(s.recs, rec)
	return nil
}

// Len 返回账内记录数.
func (s *MemoryShadowStore) Len() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.recs)
}

// Snapshot 返回账内全部记录的副本（读面；调用方改写副本不影响账面）.
func (s *MemoryShadowStore) Snapshot() []ShadowScoreRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]ShadowScoreRecord, len(s.recs))
	copy(out, s.recs)
	return out
}

// ShadowRequest 是一次影子评分请求（Python shadow_score 入参收敛为结构）.
type ShadowRequest struct {
	ResponseText string
	// Rubric 量规（scorer.yaml ai_rubric.params_schema.rubric 形态）.
	Rubric map[string]any
	// GradeBand 学段 L/M/H（prompt 上下文，不改量规分值）.
	GradeBand string
	// WritingType 写作类型（composition / picture_writing；缺省 composition）.
	WritingType string
	// CaseID/DatasetID 基准数据集内 id（缺省 ad-hoc）.
	CaseID    string
	DatasetID string
	// HumanScore 人工量规结论（{dimensions:[{id,score,...}]}；nil → pending）.
	HumanScore map[string]any
	// Tolerance 逐维一致性阈值（0 取 DefaultConsistencyTolerance）.
	Tolerance float64
}

// ShadowRunner 是影子评分执行器：AI 评分（经注入的 ai_rubric 执行面）→
// 派生 shadow_id → 一致性判定 → 追加影子账.
type ShadowRunner struct {
	scorer *AIRubricScorer
	store  ShadowStore
}

// NewShadowRunner 构造影子评分执行器；store 可为 nil（纯评分不落账——
// 基准验证脚本的纯函数面，Python shadow_score 不直接写 DB 同构）.
func NewShadowRunner(scorer *AIRubricScorer, store ShadowStore) (*ShadowRunner, error) {
	if scorer == nil {
		return nil, fmt.Errorf("%w: 影子评分器未注入", ErrInvalidInput)
	}
	return &ShadowRunner{scorer: scorer, store: store}, nil
}

// Score 执行一次影子评分：AI 评分（不触碰真实分数）→ 派生 shadow_id →
// 一致性状态 → store 非 nil 时追加影子账（写败即失败，不留「跑了没账」
// 的影子——与 ai 总线 ErrLedgerWrite 同纪律）.
func (r *ShadowRunner) Score(ctx context.Context, req ShadowRequest) (ShadowScoreRecord, error) {
	rec, err := r.evaluate(ctx, req)
	if err != nil {
		return ShadowScoreRecord{}, err
	}
	if r.store != nil {
		if err := r.store.Record(ctx, rec); err != nil {
			return ShadowScoreRecord{}, fmt.Errorf("scoring/shadow: 影子账写入失败: %w", err)
		}
	}
	return rec, nil
}

// evaluate 影子评分纯函数面（不落账）：AI 评分 + 派生 id + 一致性状态.
func (r *ShadowRunner) evaluate(ctx context.Context, req ShadowRequest) (ShadowScoreRecord, error) {
	aiScore, err := r.scorer.ScoreRubric(ctx, req.ResponseText, req.Rubric, DefaultTaskLevel, req.GradeBand)
	if err != nil {
		return ShadowScoreRecord{}, err
	}

	// 派生 id：dataset|case|rubric|作答摘要 的 sha256（确定性，便于重放）.
	respDigest := sha256Text(req.ResponseText)
	rubricID := RubricID(req.Rubric)
	datasetID := orShadowDefault(req.DatasetID, "ad-hoc")
	caseID := orShadowDefault(req.CaseID, "ad-hoc")
	writingType := orShadowDefault(req.WritingType, "composition")
	shadowID := sha256Text(fmt.Sprintf("%s|%s|%s|%s", datasetID, caseID, rubricID, respDigest))

	status := StatusPending
	tolerance := req.Tolerance
	if tolerance == 0 {
		tolerance = DefaultConsistencyTolerance
	}
	if req.HumanScore != nil {
		status = StatusInconsistent
		if ComputeConsistency(aiScore, req.HumanScore, tolerance) {
			status = StatusConsistent
		}
	}

	return ShadowScoreRecord{
		ShadowID:           shadowID,
		DatasetID:          datasetID,
		CaseID:             caseID,
		RubricID:           rubricID,
		GradeBand:          req.GradeBand,
		WritingType:        writingType,
		ResponseText:       req.ResponseText,
		ResponseTextDigest: respDigest,
		AIScore:            aiScore,
		HumanScore:         req.HumanScore,
		ConsistencyStatus:  status,
	}, nil
}

// ComputeConsistency 计算 AI 评分与人工结论是否逐维一致（验收③）：AI 维度
// 与人工维度按顺序对齐（数据集内人工维度顺序与量规一致），所有维度
// |ai - human| ≤ tolerance → 一致；人工结论缺维度 → 不一致.
func ComputeConsistency(aiScore *AIRubricScore, humanScore map[string]any, tolerance float64) bool {
	humanDims, _ := humanScore["dimensions"].([]any)
	for i, aiDim := range aiScore.Dimensions {
		if i >= len(humanDims) {
			return false
		}
		hd, ok := humanDims[i].(map[string]any)
		if !ok {
			return false
		}
		hScore, ok := paramFloat(hd["score"])
		if !ok {
			hScore = 0
		}
		delta := aiDim.Score - hScore
		if delta < 0 {
			delta = -delta
		}
		if delta > tolerance {
			return false
		}
	}
	return true
}

// DimensionComparison 是单维度对比结果.
type DimensionComparison struct {
	DimensionID string  `json:"dimension_id"`
	AIScore     float64 `json:"ai_score"`
	HumanScore  float64 `json:"human_score"`
	Delta       float64 `json:"delta"`
	Consistent  bool    `json:"consistent"`
}

// ConsistencyResult 是单 case 一致性结果（含逐维对比，用于基准报告）.
type ConsistencyResult struct {
	CaseID     string                `json:"case_id"`
	Consistent bool                  `json:"consistent"`
	MaxDelta   float64               `json:"max_delta"`
	Dimensions []DimensionComparison `json:"dimensions"`
}

// ConsistencyDetail 计算一致性详情（逐维对比；人工结论缺维度按 0 分对比并
// 计不一致——诊断口径，Python _compute_consistency_detail 同构）.
func ConsistencyDetail(aiScore *AIRubricScore, humanScore map[string]any, caseID string, tolerance float64) ConsistencyResult {
	humanDims, _ := humanScore["dimensions"].([]any)
	comparisons := make([]DimensionComparison, 0, len(aiScore.Dimensions))
	maxDelta := 0.0
	for i, aiDim := range aiScore.Dimensions {
		dimID := fmt.Sprintf("dim%d", i)
		hScore := 0.0
		if i < len(humanDims) {
			if hd, ok := humanDims[i].(map[string]any); ok {
				if id, ok := hd["id"].(string); ok && id != "" {
					dimID = id
				}
				if f, ok := paramFloat(hd["score"]); ok {
					hScore = f
				}
			}
		}
		delta := aiDim.Score - hScore
		if delta < 0 {
			delta = -delta
		}
		if delta > maxDelta {
			maxDelta = delta
		}
		comparisons = append(comparisons, DimensionComparison{
			DimensionID: dimID,
			AIScore:     aiDim.Score,
			HumanScore:  hScore,
			Delta:       delta,
			Consistent:  delta <= tolerance,
		})
	}
	consistent := false
	if len(comparisons) > 0 {
		consistent = true
		for _, c := range comparisons {
			if !c.Consistent {
				consistent = false
				break
			}
		}
	}
	return ConsistencyResult{
		CaseID:     caseID,
		Consistent: consistent,
		MaxDelta:   maxDelta,
		Dimensions: comparisons,
	}
}

// BenchmarkReport 是基准验证报告（验收③：整体一致率 ≥70%）.
type BenchmarkReport struct {
	DatasetID       string              `json:"dataset_id"`
	TotalCases      int                 `json:"total_cases"`
	ConsistentCases int                 `json:"consistent_cases"`
	ConsistencyRate float64             `json:"consistency_rate"`
	Passed          bool                `json:"passed"`
	Threshold       float64             `json:"threshold"`
	PerCase         []ConsistencyResult `json:"per_case"`
}

// BenchmarkOptions 是基准验证的判定参数（零值取默认阈值）.
type BenchmarkOptions struct {
	// Tolerance 逐维一致性阈值（0 → DefaultConsistencyTolerance）.
	Tolerance float64
	// ConsistencyRateThreshold 一致率门槛（0 → DefaultConsistencyRateThreshold）.
	ConsistencyRateThreshold float64
}

// Benchmark 对基准数据集跑影子评分 + 一致率计算（验收③；纯报告不落影子账
// ——Python benchmark_against_dataset 调 shadow_score 纯函数同构）。
// 数据集形态：{dataset_id, rubric, cases:[{case_id, grade_band, writing_type,
// response_text, human_score}]}；rubric/cases 缺失即 ErrInvalidInput.
func (r *ShadowRunner) Benchmark(ctx context.Context, dataset map[string]any, opts BenchmarkOptions) (BenchmarkReport, error) {
	rubric, ok := dataset["rubric"].(map[string]any)
	if !ok {
		return BenchmarkReport{}, fmt.Errorf("%w: 基准数据集缺 rubric 或非 object", ErrInvalidInput)
	}
	cases, ok := dataset["cases"].([]any)
	if !ok || len(cases) == 0 {
		return BenchmarkReport{}, fmt.Errorf("%w: 基准数据集缺 cases 或为空", ErrInvalidInput)
	}

	tolerance := opts.Tolerance
	if tolerance == 0 {
		tolerance = DefaultConsistencyTolerance
	}
	threshold := opts.ConsistencyRateThreshold
	if threshold == 0 {
		threshold = DefaultConsistencyRateThreshold
	}
	datasetID := DefaultDatasetID
	if id, ok := dataset["dataset_id"].(string); ok && id != "" {
		datasetID = id
	}

	report := BenchmarkReport{
		DatasetID: datasetID,
		Threshold: threshold,
		PerCase:   make([]ConsistencyResult, 0, len(cases)),
	}
	for _, raw := range cases {
		cs, ok := raw.(map[string]any)
		if !ok {
			return BenchmarkReport{}, fmt.Errorf("%w: 基准数据集 case 非 object", ErrInvalidInput)
		}
		caseID := "unknown"
		if id, ok := cs["case_id"].(string); ok && id != "" {
			caseID = id
		}
		gradeBand, _ := cs["grade_band"].(string)
		writingType, _ := cs["writing_type"].(string)
		responseText, _ := cs["response_text"].(string)
		humanScore, _ := cs["human_score"].(map[string]any)
		if humanScore == nil {
			humanScore = map[string]any{}
		}

		rec, err := r.evaluate(ctx, ShadowRequest{
			ResponseText: responseText,
			Rubric:       rubric,
			GradeBand:    gradeBand,
			WritingType:  writingType,
			CaseID:       caseID,
			DatasetID:    datasetID,
			HumanScore:   humanScore,
			Tolerance:    tolerance,
		})
		if err != nil {
			return BenchmarkReport{}, fmt.Errorf("scoring/shadow: case %q 影子评分失败: %w", caseID, err)
		}
		detail := ConsistencyDetail(rec.AIScore, humanScore, caseID, tolerance)
		report.PerCase = append(report.PerCase, detail)
		if detail.Consistent {
			report.ConsistentCases++
		}
	}

	report.TotalCases = len(cases)
	report.ConsistencyRate = float64(report.ConsistentCases) / float64(report.TotalCases)
	report.Passed = report.ConsistencyRate >= threshold
	return report, nil
}

// sha256Text 文本 sha256（"sha256:" 前缀 + hex；response_text_digest 与
// shadow_id 派生共用，Python _sha256_text 同构）.
func sha256Text(s string) string {
	sum := sha256.Sum256([]byte(s))
	return "sha256:" + hex.EncodeToString(sum[:])
}

// orShadowDefault 空串兜底.
func orShadowDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}
