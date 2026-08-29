// report.go 承载弱项报告类型与等级化输出（W3 S5；Python 冻结实现
// src/core/report/schemas.py + service.py 纯函数面的 Go 重锚定）。
//
// D8 不排名：输出是「结论已定 / 证据不足」两级等级，不是分数与名次；
// items 按 evidence_count 降序、error_type_id 升序排列只为展示确定性，
// 不构成能力排序。confidence 是归因置信度（后验），不是孩子能力的分数，
// 消费方不得据此排名或比较孩子。
package report

import (
	"sort"
	"time"
)

// 弱项条目状态两级（D8：等级非分数；与冻结实现 WeaknessItem.status
// Literal 同域）.
const (
	// StatusConcluded 证据达阈值，可归因（confidence 为贝叶斯后验）
	StatusConcluded = "concluded"
	// StatusInsufficientEvidence 证据不足，不给定论（confidence 仍返回当前
	// 后验供参考，但消费方不得当作结论呈现；§4.7 允许输出证据不足）
	StatusInsufficientEvidence = "insufficient_evidence"
)

// 场景三值域（D5 禁止混估；与冻结实现 WeaknessReport.scene Literal 及
// core/events 的 Scene 同域。report 包不 import events——场景在报告契约里
// 是独立声明的字面量域，冻结实现 schemas.py 同样独立声明）.
const (
	ScenePractice    = "practice"
	SceneDiagnosis   = "diagnosis"
	SceneMeasurement = "measurement"
)

// ValidReportScene 报告 s 是否在 D5 三值域内（空串 = 未过滤，合法）.
func ValidReportScene(s string) bool {
	switch s {
	case "", ScenePractice, SceneDiagnosis, SceneMeasurement:
		return true
	}
	return false
}

// WeaknessItem 是单错误类型的弱项条目（对应冻结实现 WeaknessItem）。
//
// RecommendedItemVersionIDs 是针对性练习 5 题小卷（仅 concluded 时非空——
// 没有定论的推荐是误导）；已剔除产生过该错误证据的题目版本。推荐取数
// （已发布实例池按 error_bindings 查题）是 IO 面，由服务化接线填充；
// 纯函数核只保证排除集语义（见 ErrorEvidence.ContributingItemVersionIDs）。
type WeaknessItem struct {
	ErrorTypeID               string
	Status                    string // StatusConcluded | StatusInsufficientEvidence
	EvidenceCount             int
	Confidence                float64 // 后验均值，4 位小数
	RecommendedItemVersionIDs []string
}

// WeaknessReport 是弱项报告 v1：按错误类型聚合作答事件的归因报告
// （对应冻结实现 WeaknessReport）。
//
// Scene 是本报告的取数口径（空=未过滤，跨场景汇总）；D5 禁止混估——
// 需要分场景口径时调用方必须显式传 Scene，报告如实回显取数口径。
type WeaknessReport struct {
	StudentAliasID string // UUID 文本（假名身份）
	Scene          string // 空 = 未过滤
	MinEvidence    int
	GeneratedAt    time.Time
	Items          []WeaknessItem
}

// BuildWeaknessItems 由聚合证据产出等级化条目（阈值判定 + 确定性排序，
// 对应冻结实现 build_weakness_report 的纯函数面）。
//
// 每个错误类型：证据计数 + 贝叶斯后验置信度（4 位小数）+ 阈值判定；
// 达阈值（concluded）的类型由服务化接线附带针对性练习推荐（剔除来源题）；
// 未达阈值输出「证据不足」，不给定论、不给推荐。
// 排序：evidence_count 降序、error_type_id 升序（展示确定性，非排名）。
func BuildWeaknessItems(evidences map[string]*ErrorEvidence, minEvidence int) []WeaknessItem {
	items := make([]WeaknessItem, 0, len(evidences))
	for errorTypeID, ev := range evidences {
		concluded := ev.EvidenceCount >= minEvidence
		status := StatusInsufficientEvidence
		if concluded {
			status = StatusConcluded
		}
		items = append(items, WeaknessItem{
			ErrorTypeID:   errorTypeID,
			Status:        status,
			EvidenceCount: ev.EvidenceCount,
			Confidence:    roundTo4(ev.Posterior()),
		})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].EvidenceCount != items[j].EvidenceCount {
			return items[i].EvidenceCount > items[j].EvidenceCount
		}
		return items[i].ErrorTypeID < items[j].ErrorTypeID
	})
	return items
}

// BuildWeaknessReport 组装报告头 + 条目（对应冻结实现 build_weakness_report
// 的装配面；studentAliasID 为 UUID 文本，scene 为 D5 三值域或空）。
// 推荐小卷由服务化接线在条目级填充（IO 面，见 WeaknessItem 注释）。
func BuildWeaknessReport(studentAliasID, scene string, minEvidence int, generatedAt time.Time, evidences map[string]*ErrorEvidence) WeaknessReport {
	return WeaknessReport{
		StudentAliasID: studentAliasID,
		Scene:          scene,
		MinEvidence:    minEvidence,
		GeneratedAt:    generatedAt,
		Items:          BuildWeaknessItems(evidences, minEvidence),
	}
}
