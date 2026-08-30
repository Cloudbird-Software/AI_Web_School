// 弱项报告与复习到期两条学生只读端点的接线（GO-RW-005/006 服务化收口，
// 审计 #155 最后两处 501）。响应形状对齐冻结契约 openapi-v1.1：
// WeaknessReport / ReviewQueueEntryPydantic（api 层 DTO 显式钉键面）。
//
// 取证分层：api 只做协议面（认证盾 → 归属断言 → 参数解析 → 查询 → DTO
// 直出/脱敏错误映射），DB 取证在 core/report / core/review 的查询服务，
// 装配经消费侧最小接口注入（ContentQueries 同一纪律）；未注入保持 501
// 占位（装配语义，认证盾照挂）。
package api

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/report"
	"github.com/Cloudbird-Software/AI_Web_School/core/review"
)

// DefaultMinEvidence / DefaultDueLimit 是契约缺省（openapi-v1.1 参数表）.
const (
	DefaultMinEvidence = 3
	DefaultDueLimit    = 20
	MaxDueLimit        = 100
)

// ReportQueries 是 api 对弱项报告只读面的最小消费接口.
type ReportQueries interface {
	InferenceEvents(ctx context.Context, studentAliasID string, scene string) ([]report.InferenceEventView, error)
	Recommended(ctx context.Context, errorTypeID string, exclude []string, limit int) ([]string, error)
}

// DueQueries 是 api 对复习到期只读面的最小消费接口.
type DueQueries interface {
	DueEntries(ctx context.Context, studentAliasID string, now time.Time, limit int) ([]review.DueEntryProjection, error)
}

// LearnerReads 聚合两条学生只读面的注入接缝（nil 字段 = 对应端点 501 占位）.
type LearnerReads struct {
	Reports ReportQueries
	Review  DueQueries
}

// weaknessReportDTO 是契约 WeaknessReport 的 api 层键面（required 五键 +
// 可空 scene：null=未过滤跨场景汇总，D5 口径如实回显）.
type weaknessReportDTO struct {
	StudentAliasID string            `json:"student_alias_id"`
	Scene          *string           `json:"scene"`
	MinEvidence    int               `json:"min_evidence"`
	GeneratedAt    string            `json:"generated_at"`
	Items          []weaknessItemDTO `json:"items"`
}

type weaknessItemDTO struct {
	ErrorTypeID               string   `json:"error_type_id"`
	Status                    string   `json:"status"`
	EvidenceCount             int      `json:"evidence_count"`
	Confidence                float64  `json:"confidence"`
	RecommendedItemVersionIDs []string `json:"recommended_item_version_ids,omitempty"`
}

// reviewEntryDTO 是契约 ReviewQueueEntryPydantic 的 api 层键面.
type reviewEntryDTO struct {
	EntryID           string  `json:"entry_id"`
	StudentAliasID    string  `json:"student_alias_id"`
	ItemVersionID     string  `json:"item_version_id"`
	PolicyID          string  `json:"policy_id"`
	PolicyVersion     string  `json:"policy_version"`
	Stage             int     `json:"stage"`
	Status            string  `json:"status"`
	SourceErrorTypeID *string `json:"source_error_type_id"`
	EnqueuedAt        string  `json:"enqueued_at"`
	DueAt             string  `json:"due_at"`
}

// reportsWeakness 生成 GET /reports/weakness/{student_alias_id} 的 handler：
// 归属断言（D9）→ 参数解析（scene 三值域/min_evidence≥1，非法 422）→
// 聚合取证 → 契约 JSON 直出。推荐小卷取数失败不静默：fail-closed 500.
func reportsWeakness(reads LearnerReads) http.HandlerFunc {
	const aliasVar = "student_alias_id"
	return func(w http.ResponseWriter, r *http.Request) {
		p, ok := middleware.FromContext(r.Context())
		if !ok {
			log.Printf("auth denied class=%q reason=principal-missing route=%s", middleware.ErrorClassUnauthorized, r.Pattern) // route=服务端常量，禁落 r.URL.Path（go/log-injection）
			writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
			return
		}
		alias := r.PathValue(aliasVar)
		if err := auth.AssertOwnsAlias(p, alias); err != nil {
			middleware.WriteAuthErrorResponse(w, err)
			return
		}
		scene := r.URL.Query().Get("scene")
		if !report.ValidReportScene(scene) {
			writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
			return
		}
		minEvidence := DefaultMinEvidence
		if v := r.URL.Query().Get("min_evidence"); v != "" {
			n, err := strconv.Atoi(v)
			if err != nil || n < 1 {
				writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
				return
			}
			minEvidence = n
		}
		events, err := reads.Reports.InferenceEvents(r.Context(), alias, scene)
		if err != nil {
			log.Printf("report query failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		evidences, err := report.AggregateInferences(events)
		if err != nil {
			log.Printf("report aggregate failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		rep := report.BuildWeaknessReport(alias, scene, minEvidence, time.Now().UTC(), evidences)
		dto := weaknessReportDTO{
			StudentAliasID: rep.StudentAliasID,
			MinEvidence:    rep.MinEvidence,
			GeneratedAt:    rep.GeneratedAt.Format(time.RFC3339),
			Items:          make([]weaknessItemDTO, 0, len(rep.Items)),
		}
		if scene != "" {
			s := scene
			dto.Scene = &s
		}
		for _, it := range rep.Items {
			item := weaknessItemDTO{
				ErrorTypeID:   it.ErrorTypeID,
				Status:        it.Status,
				EvidenceCount: it.EvidenceCount,
				Confidence:    it.Confidence,
			}
			if it.Status == report.StatusConcluded {
				ids, err := reads.Reports.Recommended(r.Context(), it.ErrorTypeID, evidences[it.ErrorTypeID].ContributingItemVersionIDs(), 5)
				if err != nil {
					log.Printf("report recommend failure route=%s error_class=%s", r.Pattern, errClass(err))
					writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
					return
				}
				item.RecommendedItemVersionIDs = ids
			}
			dto.Items = append(dto.Items, item)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if encErr := json.NewEncoder(w).Encode(dto); encErr != nil {
			log.Printf("report encode failure error_class=%T", encErr)
		}
	}
}

// reviewDue 生成 GET /review/due/{student_alias_id} 的 handler：归属断言
// （D9）→ 参数解析（now RFC3339 缺省当前 UTC / limit 1..100 缺省 20，非法
// 422）→ 到期队列直出（空为 []，序列化恒为数组）.
func reviewDue(reads LearnerReads) http.HandlerFunc {
	const aliasVar = "student_alias_id"
	return func(w http.ResponseWriter, r *http.Request) {
		p, ok := middleware.FromContext(r.Context())
		if !ok {
			log.Printf("auth denied class=%q reason=principal-missing route=%s", middleware.ErrorClassUnauthorized, r.Pattern) // route=服务端常量，禁落 r.URL.Path（go/log-injection）
			writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
			return
		}
		alias := r.PathValue(aliasVar)
		if err := auth.AssertOwnsAlias(p, alias); err != nil {
			middleware.WriteAuthErrorResponse(w, err)
			return
		}
		now := time.Now().UTC()
		if v := r.URL.Query().Get("now"); v != "" {
			t, err := time.Parse(time.RFC3339, v)
			if err != nil {
				writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
				return
			}
			now = t.UTC()
		}
		limit := DefaultDueLimit
		if v := r.URL.Query().Get("limit"); v != "" {
			n, err := strconv.Atoi(v)
			if err != nil || n < 1 || n > MaxDueLimit {
				writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
				return
			}
			limit = n
		}
		rows, err := reads.Review.DueEntries(r.Context(), alias, now, limit)
		if err != nil {
			log.Printf("review due failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		dto := make([]reviewEntryDTO, 0, len(rows))
		for _, e := range rows {
			dto = append(dto, reviewEntryDTO{
				EntryID:           e.EntryID,
				StudentAliasID:    e.StudentAliasID,
				ItemVersionID:     e.ItemVersionID,
				PolicyID:          e.PolicyID,
				PolicyVersion:     e.PolicyVersion,
				Stage:             e.Stage,
				Status:            e.Status,
				SourceErrorTypeID: e.SourceErrorTypeID,
				EnqueuedAt:        e.EnqueuedAt.Format(time.RFC3339),
				DueAt:             e.DueAt.Format(time.RFC3339),
			})
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if encErr := json.NewEncoder(w).Encode(dto); encErr != nil {
			log.Printf("review due encode failure error_class=%T", encErr)
		}
	}
}
