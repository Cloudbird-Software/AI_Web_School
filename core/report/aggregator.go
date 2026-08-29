// aggregator.go 承载弱项报告聚合核（W3 S5；Python 冻结实现
// src/core/report/aggregator.py 的 Go 重锚定）——纯函数，无 IO。
//
// 贝叶斯累积语义见包文档（doc.go）。聚合键是 error_type_id；场景过滤由
// 调用方在取数层完成（D5），本层不感知场景。
package report

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
)

// MinEvidenceDefault 证据阈值默认值：3 条独立证据以下输出「证据不足」.
const MinEvidenceDefault = 3

// ErrInvalidInference 是错误推断证据非法的哨兵错误（细分原因见 wrap 文本）.
var ErrInvalidInference = errors.New("report: error_inferences 证据非法")

// InferenceEventView 是作答事件的报告视图（response_event 的最小投影，
// 对应冻结实现 InferenceEventView dataclass）。
type InferenceEventView struct {
	ItemVersionID   string
	ErrorInferences []map[string]any
}

// ErrorEvidence 是单错误类型的累积证据（Beta 后验 + 计数 + 来源题集合），
// 对应冻结实现 ErrorEvidence dataclass。
type ErrorEvidence struct {
	ErrorTypeID   string
	EvidenceCount int
	// Alpha / Beta 为 Beta 后验参数；先验 α0 = β0 = 1。
	Alpha float64
	Beta  float64
	// contributing 是来源题集合（去重；与 evidence_count 不同——同一题的
	// 多条推断各计一条证据，但来源题只记一次）。
	contributing map[string]struct{}
}

// Posterior 后验均值 = 归因置信度（§4.5 报告置信度即后验）.
func (e *ErrorEvidence) Posterior() float64 {
	return e.Alpha / (e.Alpha + e.Beta)
}

// Add 累积一条证据（置信度加权）。confidence 越界 [0,1] 返回错误
// （与冻结实现 ValueError 同判据）.
func (e *ErrorEvidence) Add(confidence float64, itemVersionID string) error {
	if !(confidence >= 0 && confidence <= 1) {
		return fmt.Errorf("%w: error_inferences[].confidence 越界 [0,1]: %v", ErrInvalidInference, confidence)
	}
	e.EvidenceCount++
	e.Alpha += confidence
	e.Beta += 1.0 - confidence
	if e.contributing == nil {
		e.contributing = map[string]struct{}{}
	}
	e.contributing[itemVersionID] = struct{}{}
	return nil
}

// ContributingItemVersionIDs 返回来源题集合（升序，确定性）——
// 针对性练习推荐剔除该集合（原题重练测的是记忆不是理解）.
func (e *ErrorEvidence) ContributingItemVersionIDs() []string {
	ids := make([]string, 0, len(e.contributing))
	for id := range e.contributing {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// AggregateInferences 按 error_type_id 归并全部事件的错误推断（纯函数）。
//
// events: 报告视图事件流（场景过滤由调用方在取数层完成——D5 分场景
// 取数在 SQL WHERE 定型，不在聚合层混合后再拆）。
//
// 返回 {error_type_id: ErrorEvidence}；只含有过错误推断的类型。
// 脏数据判据（与冻结实现一致）：error_type_id 缺失/非字符串/空串的推断
// 跳过而非炸报告（契约 §4 required=error_type_id）；confidence 缺省按 0.0
// 计（冻结实现 .get("confidence", 0.0)），显式 null、非数值、越界 [0,1]
// 返回错误（冻结实现 float()/Add 的 fail-fast 面）。
func AggregateInferences(events []InferenceEventView) (map[string]*ErrorEvidence, error) {
	evidences := map[string]*ErrorEvidence{}
	for _, event := range events {
		for _, inference := range event.ErrorInferences {
			errorTypeID, _ := inference["error_type_id"].(string)
			if errorTypeID == "" {
				// 契约 §4 required=error_type_id；脏数据跳过而非炸报告
				continue
			}
			confidence, err := inferenceConfidence(inference)
			if err != nil {
				return nil, err
			}
			ev, ok := evidences[errorTypeID]
			if !ok {
				ev = &ErrorEvidence{ErrorTypeID: errorTypeID, Alpha: 1.0, Beta: 1.0}
				evidences[errorTypeID] = ev
			}
			if err := ev.Add(confidence, event.ItemVersionID); err != nil {
				return nil, err
			}
		}
	}
	return evidences, nil
}

// inferenceConfidence 取推断置信度：缺省 0.0；接受 int 族/整值或小数
// float64/json.Number/数字字符串（与冻结实现 float() 的收敛面一致）；
// 显式 null / 非数值 / 布尔（float(True)==1.0 的 Python 真值陷阱）拒绝.
func inferenceConfidence(inference map[string]any) (float64, error) {
	v, present := inference["confidence"]
	if !present {
		return 0.0, nil
	}
	if v == nil {
		return 0, fmt.Errorf("%w: confidence 显式 null（冻结实现 float(None) 同样失败）", ErrInvalidInference)
	}
	switch x := v.(type) {
	case float64:
		return x, nil
	case float32:
		return float64(x), nil
	case int:
		return float64(x), nil
	case int32:
		return float64(x), nil
	case int64:
		return float64(x), nil
	case json.Number:
		f, err := x.Float64()
		if err != nil {
			return 0, fmt.Errorf("%w: confidence 必须是数值，得到 %s", ErrInvalidInference, x.String())
		}
		return f, nil
	case string:
		f, err := json.Number(x).Float64()
		if err != nil {
			return 0, fmt.Errorf("%w: confidence 必须是数值，得到 %q", ErrInvalidInference, x)
		}
		return f, nil
	default:
		return 0, fmt.Errorf("%w: confidence 必须是数值，得到 %T", ErrInvalidInference, v)
	}
}

// roundTo4 与冻结实现 round(posterior, 4) 对齐的 4 位小数舍入。
// 显式偏离（边界情形）：Python round 是银行家舍入（half-even），Go 用
// half-away-from-zero——仅当后验恰落在 0.00005 的二进制表示精确半点上时
// 结果可能差 1 ulp；连续后验落在该半点的概率可忽略，展示语义不变.
func roundTo4(x float64) float64 {
	return math.Round(x*1e4) / 1e4
}
