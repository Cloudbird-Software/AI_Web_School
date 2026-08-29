// health.go 承载题目健康度模型 + 生命周期状态机规则面（T-W4-004；Python
// 冻结实现 src/core/data/health.py 的 Go 重锚定）。
//
// 架构 v2 §4.7「飞轮闭环」：题目健康度评估（正确率异常/区分度低/干扰项无人
// 选/耗时异常）→ 生命周期 ACTIVE→WATCH→QUARANTINED→RETIRED（签发制，
// 退役不删除）。健康度评估是只读分析；状态变更是 append-only INSERT（D1
// 物理强制）——INSERT 面（transition_lifecycle 的 DB 写入）本波留白，规则
// 面由 ValidateTransition 纯函数承担。
//
// 取数 SQL 三段（事件 / item_param 实测区分度 / item_version 选项结构）是
// IO 面，本波留白——EvaluateHealth 消费注入的事件视图与选项结构。
//
// 宪法 A5/X6：本包是核心域数据子模块，禁止 import 任何学科包/学段包。
package datastat

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

// 健康度评估常量（对应冻结实现 HEALTH_MIN_SAMPLE 与异常阈值，架构 §4.7
// 默认档，可在调用方覆盖后重算）.
const (
	// HealthMinSample 最小样本门槛（n < 此值不判定异常，记 insufficient_sample）.
	HealthMinSample = 30
	// CorrectRateTooHigh 正确率 > 0.95 → 题太易（无区分度）.
	CorrectRateTooHigh = 0.95
	// CorrectRateTooLow 正确率 < 0.05 → 题太难.
	CorrectRateTooLow = 0.05
	// LowDiscrimination 区分度 < 0.2 → 区分度低.
	LowDiscrimination = 0.2
	// TimeTooFastMs 中位耗时 < 2s → 猜题/秒杀.
	TimeTooFastMs = 2000.0
	// TimeTooSlowMs 中位耗时 > 30s → 困惑/卡题.
	TimeTooSlowMs = 30000.0
	// AnomalyPenalty 每个异常的扣分（health_score = 1.0 - sum(penalties)，下限 0.0）.
	AnomalyPenalty = 0.2
)

// 生命周期四态（对应冻结实现 ItemLifecycleState，值与 PG 枚举
// item_lifecycle_state_enum 一致）.
const (
	LifecycleActive      = "ACTIVE"
	LifecycleWatch       = "WATCH"
	LifecycleQuarantined = "QUARANTINED"
	LifecycleRetired     = "RETIRED"
)

// lifecycleStates 合法状态集合（对应冻结实现 LIFECYCLE_STATES）.
var lifecycleStates = []string{LifecycleActive, LifecycleWatch, LifecycleQuarantined, LifecycleRetired}

// ActivePoolStates 活跃池状态集合（排除 QUARANTINED 与 RETIRED；对应冻结
// 实现 ACTIVE_POOL_STATES）.
var ActivePoolStates = []string{LifecycleActive, LifecycleWatch}

// terminalStates 终态集合（无任何回边；对应冻结实现 TERMINAL_STATES）.
var terminalStates = map[string]bool{LifecycleRetired: true}

// gateCertRequiredStates 需要门证书的目标状态（对应冻结实现
// GATE_CERT_REQUIRED_STATES）.
var gateCertRequiredStates = map[string]bool{LifecycleQuarantined: true, LifecycleRetired: true}

// allowedTransitions 状态机转换规则：from → 允许的 to 集合（对应冻结实现
// _ALLOWED_TRANSITIONS；RETIRED 为终态，无任何回边）.
var allowedTransitions = map[string]map[string]bool{
	LifecycleActive:      {LifecycleWatch: true, LifecycleRetired: true},
	LifecycleWatch:       {LifecycleActive: true, LifecycleQuarantined: true, LifecycleRetired: true},
	LifecycleQuarantined: {LifecycleWatch: true, LifecycleRetired: true},
	LifecycleRetired:     {},
}

// transitionAllowed 报告 from → to 是否合法（from 空 = 初始，仅允许 → ACTIVE；
// 对应冻结实现 None 键与 _ALLOWED_TRANSITIONS.get(from, frozenset())）.
func transitionAllowed(from, to string) bool {
	var allowed map[string]bool
	if from == "" {
		allowed = map[string]bool{LifecycleActive: true}
	} else {
		allowed = allowedTransitions[from]
	}
	return allowed[to]
}

// 生命周期校验错误（对应冻结实现 ValueError / LifecycleTransitionError 的
// 三类失败：非法值 / 终态回边 / 非法转换 / 缺门证书）.
var (
	// ErrUnknownLifecycleState 表示 to_state 不在四态值域内.
	ErrUnknownLifecycleState = errors.New("datastat: 非法生命周期状态")
	// ErrTerminalTransition 表示从终态转出（RETIRED 无任何回边）.
	ErrTerminalTransition = errors.New("datastat: 终态禁止任何转换")
	// ErrIllegalTransition 表示 from → to 不在允许集合内.
	ErrIllegalTransition = errors.New("datastat: 非法生命周期转换")
	// ErrGateCertRequired 表示目标状态需要门证书.
	ErrGateCertRequired = errors.New("datastat: 目标状态需门证书（gate_certificate_id 必填）")
)

// knownLifecycleState 报告 s 是否为合法四态值.
func knownLifecycleState(s string) bool {
	for _, v := range lifecycleStates {
		if s == v {
			return true
		}
	}
	return false
}

// allowedTargets 返回 from 的允许目标集合（升序，报错信息用；from 空 = 初始）.
func allowedTargets(from string) []string {
	var allowed map[string]bool
	if from == "" {
		allowed = map[string]bool{LifecycleActive: true}
	} else {
		allowed = allowedTransitions[from]
	}
	targets := make([]string, 0, len(allowed))
	for to := range allowed {
		targets = append(targets, to)
	}
	sort.Strings(targets)
	return targets
}

// ValidateTransition 校验生命周期状态机转换规则（纯函数；对应冻结实现
// transition_lifecycle 的规则面——append-only INSERT 是 IO 面，本波留白）。
//
// 转换规则（验收 §2）：
//   - ACTIVE ↔ WATCH：自动（无需门证书）
//   - WATCH → QUARANTINED：需门证书
//   - 任何 → RETIRED：需门证书
//   - RETIRED 为终态，无回边
//
// fromState 空 = 初始（无既有状态，仅允许 → ACTIVE）；hasGateCert 表示调用方
// 持有门证书.
func ValidateTransition(fromState, toState string, hasGateCert bool) error {
	if !knownLifecycleState(toState) {
		return fmt.Errorf("%w: 非法 to_state=%q；合法值 %v", ErrUnknownLifecycleState, toState, lifecycleStates)
	}
	if !transitionAllowed(fromState, toState) {
		if terminalStates[fromState] {
			return fmt.Errorf("%w: %s 为终态，禁止任何转换（→ %s）", ErrTerminalTransition, fromState, toState)
		}
		return fmt.Errorf("%w: %s → %s；允许目标 %v", ErrIllegalTransition, fromState, toState, allowedTargets(fromState))
	}
	if gateCertRequiredStates[toState] && !hasGateCert {
		return fmt.Errorf("%w: 转入 %s 需门证书（gate_certificate_id 必填）", ErrGateCertRequired, toState)
	}
	return nil
}

// Median 计算中位数（对应冻结实现 health._median → statistics.median：排序后
// 奇数取中位、偶数取中间两数均值；空切片返回 nil）.
func Median(values []float64) *float64 {
	if len(values) == 0 {
		return nil
	}
	s := append([]float64(nil), values...)
	sort.Float64s(s)
	n := len(s)
	if n%2 == 1 {
		v := s[n/2]
		return &v
	}
	v := (s[n/2-1] + s[n/2]) / 2.0
	return &v
}

// DetectAnomalies 根据指标判定异常标签（对应冻结实现 health._detect_anomalies；
// 四类异常，标签按判定序输出，确定无歧义）。
//
// 四类异常（架构 §4.7）：
//  1. correct_rate_too_high / correct_rate_too_low：正确率异常（严格大于/小于阈值）
//  2. low_discrimination：区分度 < 0.2（仅 discrimination 可计算时判定）
//  3. no_distractor_selected：某干扰项被选 0 次（单选题；correct 选项不计）
//  4. time_too_fast / time_too_slow：中位耗时过快（<2s）或过慢（>30s）
//
// 样本不足（n < HealthMinSample）不判定——返回空（外层标 insufficient_sample，
// 「样本不足不伪造异常」）。distractorRates 为 nil 时跳过干扰项判定（非单选题）.
func DetectAnomalies(
	sampleSize int,
	correctRate float64,
	discrimination *float64,
	durationMedian *float64,
	distractorRates map[string]float64,
	correctOption string,
) []string {
	anomalies := []string{}
	if sampleSize < HealthMinSample {
		return anomalies // 样本不足不判定（insufficient_sample 在外层标记）
	}

	// 1. 正确率异常
	if correctRate > CorrectRateTooHigh {
		anomalies = append(anomalies, "correct_rate_too_high")
	} else if correctRate < CorrectRateTooLow {
		anomalies = append(anomalies, "correct_rate_too_low")
	}

	// 2. 区分度低（仅 discrimination 可计算时判定）
	if discrimination != nil && *discrimination < LowDiscrimination {
		anomalies = append(anomalies, "low_discrimination")
	}

	// 3. 干扰项无人选（单选题；固定键序遍历保证确定性——冻结实现 set 迭代序
	// 不定，但命中即 break 且标签唯一，结果等价）
	if distractorRates != nil && correctOption != "" {
		options := make([]string, 0, len(distractorRates))
		for option := range distractorRates {
			options = append(options, option)
		}
		sort.Strings(options)
		for _, option := range options {
			if option != correctOption && distractorRates[option] == 0.0 {
				anomalies = append(anomalies, "no_distractor_selected")
				break // 一个干扰项无人选即标记
			}
		}
	}

	// 4. 耗时异常
	if durationMedian != nil {
		if *durationMedian < TimeTooFastMs {
			anomalies = append(anomalies, "time_too_fast")
		} else if *durationMedian > TimeTooSlowMs {
			anomalies = append(anomalies, "time_too_slow")
		}
	}

	return anomalies
}

// HealthScoreOf 由异常数计算健康分：max(0, 1 - AnomalyPenalty·n)（对应冻结
// 实现 evaluate_health 的内联公式，提取为可测纯函数）.
func HealthScoreOf(anomalyCount int) float64 {
	return math.Max(0.0, 1.0-AnomalyPenalty*float64(anomalyCount))
}

// HealthMetrics 是健康度明细指标（对应冻结实现 evaluate_health 的 metrics
// dict 的类型化重锚定；Note 承载无数据报告的说明键）.
type HealthMetrics struct {
	Note             string // 仅无数据报告携带（"no response_event in scope"）
	CorrectRate      float64
	Discrimination   *float64
	DurationMedianMs *float64
	DistractorRates  map[string]float64
	CorrectOption    string
	PurposeScope     string
}

// HealthReport 是单题健康度评估报告（对应冻结实现 health.HealthReport）.
type HealthReport struct {
	// ItemID 被评估的题目身份.
	ItemID string
	// SampleSize 参与评估的作答事件数（n）.
	SampleSize int
	// HealthScore 0.0~1.0（1.0=完全健康，0.0=最差）.
	HealthScore float64
	// Anomalies 异常标签列表（如 [correct_rate_too_high, low_discrimination]）.
	Anomalies []string
	// Metrics 明细指标.
	Metrics HealthMetrics
	// InsufficientSample n < HealthMinSample 时为 true（不判定异常）.
	InsufficientSample bool
}

// HealthEventView 是健康度评估的单条作答事件视图（取数面注入——冻结实现
// _FETCH_EVENTS_SQL 按 scene 过滤 + correct 缺键过滤后的事件行）。
// Correct 为 scoring_trace.dimension_scores.correct；「解析失败按 0.0 计入
// 样本」（冻结实现 float() except 分支）的契约由取数面承担。Selected 为
// raw_payload.selected，空 = 无选项作答.
type HealthEventView struct {
	Correct    float64
	DurationMs *float64
	Selected   string
}

// HealthOptions 是健康度评估的注入输入（区分度来自 item_param 实测行、选项
// 结构来自 item_version——均为取数 SQL 职责，本波 IO 面留白）.
type HealthOptions struct {
	// Discrimination 该 item 最新实测区分度（CTT 标定产出；nil = 无实测行/
	// 不可计算——从 item_param 读避免重算，尊重数据飞轮分工）.
	Discrimination *float64
	// CorrectOption 单选题正解（scoring_ref.scorer_params.answer）；空 = 非单选题.
	CorrectOption string
	// DistractorOptions 干扰项 option_value 列表（error_bindings 展开去重前原序）.
	DistractorOptions []string
}

// EvaluateHealth 评估单题健康度（纯聚合面；对应冻结实现 evaluate_health 的
// 指标计算 + 干扰项分析 + 异常判定 + 评分，DB 取数面注入）。
//
// events 为空时返回无数据报告（score=0、insufficient=true、metrics 仅含说明
// ——「item 无任何版本」与「场景内无事件」的区分在取数面，本核只见事件列表）.
func EvaluateHealth(itemID string, purposeScope string, events []HealthEventView, opts HealthOptions) HealthReport {
	sampleSize := len(events)
	if sampleSize == 0 {
		return HealthReport{
			ItemID:             itemID,
			SampleSize:         0,
			HealthScore:        0.0,
			Anomalies:          []string{},
			Metrics:            HealthMetrics{Note: "no response_event in scope"},
			InsufficientSample: true,
		}
	}

	// 计算指标（对应冻结实现第 3 步）
	correctSum := 0.0
	var durations []float64
	var selections []string // raw_payload.selected
	for _, ev := range events {
		correctSum += ev.Correct
		if ev.DurationMs != nil {
			durations = append(durations, *ev.DurationMs)
		}
		if ev.Selected != "" {
			selections = append(selections, ev.Selected)
		}
	}
	correctRate := correctSum / float64(sampleSize)
	durationMedian := Median(durations)

	// 干扰项分析（单选题；对应冻结实现第 5 步）：全选项计数/样本量，含
	// correct 选项自身的选择率（判定时剔除）。键序固定（升序）——冻结实现
	// set 迭代序不定，rates 值域等价。
	var distractorRates map[string]float64
	correctOption := opts.CorrectOption
	hasOptionStructure := correctOption != "" || len(opts.DistractorOptions) > 0
	if hasOptionStructure && len(selections) > 0 {
		counts := make(map[string]int)
		if correctOption != "" {
			counts[correctOption] = 0
		}
		for _, opt := range opts.DistractorOptions {
			if _, ok := counts[opt]; !ok {
				counts[opt] = 0
			}
		}
		if len(counts) > 0 {
			for _, sel := range selections {
				if _, ok := counts[sel]; ok {
					counts[sel]++
				}
			}
			distractorRates = make(map[string]float64, len(counts))
			options := make([]string, 0, len(counts))
			for opt := range counts {
				options = append(options, opt)
			}
			sort.Strings(options)
			for _, opt := range options {
				distractorRates[opt] = float64(counts[opt]) / float64(sampleSize)
			}
		}
	}

	// 判定异常 + 评分（对应冻结实现第 6 步）
	insufficient := sampleSize < HealthMinSample
	anomalies := DetectAnomalies(sampleSize, correctRate, opts.Discrimination,
		durationMedian, distractorRates, correctOption)
	healthScore := HealthScoreOf(len(anomalies))

	return HealthReport{
		ItemID:      itemID,
		SampleSize:  sampleSize,
		HealthScore: healthScore,
		Anomalies:   anomalies,
		Metrics: HealthMetrics{
			CorrectRate:      correctRate,
			Discrimination:   opts.Discrimination,
			DurationMedianMs: durationMedian,
			DistractorRates:  distractorRates,
			CorrectOption:    correctOption,
			PurposeScope:     purposeScope,
		},
		InsufficientSample: insufficient,
	}
}
