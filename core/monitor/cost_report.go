// Package monitor 是成本仪表盘与健康度聚合（T-W4-041 语义的 Go 移植，
// PyR 波：Python src/core/monitoring/cost_dashboard.py → core/monitor）。
//
// 聚合 core/ai 台账 entries 输出五项指标：总成本、按模型、按任务、按学科
// （dimension_extractor 参数注入，A5 学科中立——本包不 import 学科包）、
// 单题平均成本。与 core/ai/cost.go（单题成本）的关系：本包是跨题汇总。
package monitor

import (
	"fmt"
	"sort"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// CostReport 成本报告（聚合结果容器；字段对齐 Python CostReport 模型）。
type CostReport struct {
	TotalCostCNY   float64            // 全量台账成本合计（人民币元）
	ByModel        map[string]float64 // model -> cost_cny
	ByTask         map[string]float64 // task_name -> cost_cny
	BySubject      map[string]float64 // 维度值 -> cost_cny
	AvgCostPerItem float64            // total / 唯一 artifact_ref 数；无 artifact_ref 时 0（避免除零）
	ItemCount      int                // 唯一 artifact_ref 数
	CallCount      int                // 台账总调用数
}

// DimensionExtractor 把 artifact_ref 映射到维度值（如学科名）；
// nil 时全部归 "unknown"。调用方负责解析题目元数据，本包保持纯聚合。
type DimensionExtractor func(artifactRef string) string

// BuildCostReport 聚合台账 entries（对齐 Python build_cost_report 语义）：
//   - artifact_ref 为空的调用归 "unassigned"（避免 extractor 收到空串报错）
//   - dimension_extractor 为 nil 时所有成本归 "unknown"（开发/测试环境
//     无学科映射时不阻断报告生成——Python 版同款宽容语义）
func BuildCostReport(entries []ai.LedgerEntry, extractor DimensionExtractor) (CostReport, error) {
	if entries == nil {
		return CostReport{}, fmt.Errorf("monitor: entries 为 nil（fail-closed：空聚合须显式传空切片）")
	}
	r := CostReport{
		ByModel:   map[string]float64{},
		ByTask:    map[string]float64{},
		BySubject: map[string]float64{},
	}
	artifactRefs := map[string]bool{}
	for _, e := range entries {
		r.TotalCostCNY += e.CostCNY
		r.CallCount++
		r.ByModel[e.Model] += e.CostCNY
		r.ByTask[e.TaskName] += e.CostCNY

		var dim string
		switch {
		case extractor == nil:
			dim = "unknown"
		case e.ArtifactRef == "":
			dim = "unassigned"
		default:
			dim = extractor(e.ArtifactRef)
		}
		r.BySubject[dim] += e.CostCNY

		if e.ArtifactRef != "" {
			artifactRefs[e.ArtifactRef] = true
		}
	}
	r.ItemCount = len(artifactRefs)
	if r.ItemCount > 0 {
		r.AvgCostPerItem = r.TotalCostCNY / float64(r.ItemCount)
	}
	return r, nil
}

// SortedKeys 返回 map 的排序键（报告输出确定性——同输入同顺序）。
func SortedKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
