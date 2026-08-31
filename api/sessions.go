package api

// sessions.go 承载 GO-RW-002 的会话端点业务接线：sessions 族路由从「认证
// 通过 → 501 占位」升级为「认证 → 越权判据 → SessionService → 契约 JSON」。
//
// 分层（ADR-0004 §三）：本文件只做协议层——路由参数提取、请求体解码、错误
// → HTTP 映射、响应编码；会话语义（题序不可变、提交幂等、时长保护、家长
// 授权门、归属断言）全部住在 core/session.Service。响应形状逐字段对齐冻结
// 契约 specs/contracts/api/openapi-v1.1.json 的会话端点 schema.
//
// 授权面前置不变（T-W5-006/010 既定序，不得重排）：
//
//	认证（盾）→ 越权判据（POST /sessions 请求体 alias 冒用）→ 家长授权门
//	→ 业务（SessionService）
//
// 服务未装配（svc == nil）的兼容形态保留 501 占位：与 NewRouterWithConsent
// 的 nil 授权账语义同构——「基础设施未接线」显式暴露，绝不伪造业务响应.

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
	"github.com/Cloudbird-Software/AI_Web_School/core/review"
	"github.com/Cloudbird-Software/AI_Web_School/core/session"
)

// errInvalidSubmitBody 是提交请求体缺必填字段的协议级违例（契约 §required：
// item_version_id / response 缺一即拒，不给评分与落账路径晚到机会）.
var errInvalidSubmitBody = errors.New("api: 作答提交请求体缺必填字段（item_version_id / response）")

// zeroTime 是「取服务时钟」的显式零值形参（服务域 resolveAt 归一）.
var zeroTime time.Time

// ErrorClassConflict 是会话域冲突（409）的对外错误类：时长保护触发/序列外
// 作答/会话已结束。契约 v1.1 的 409 响应未声明 content schema；沿用单字段
// 脱敏形态并新增本类（errmap.go 既有常量集不动——全集断言仍成立），对外
// 不区分冲突子型，细分 reason 只进服务端日志.
const ErrorClassConflict = "conflict"

// ResponseScorer 是作答评分的协议层接缝：契约 Feedback 的对错/维度分来自
// 评分轨迹（response_event 契约 §3），评分本身属上游域（core/scoring × 内容
// 账），本层只声明端口、由装配层（cmd/school）注入 concrete 桥。nil = 评分
// 链路未装配 → 提交入口 fail-closed 501，绝不伪造评分落账.
type ResponseScorer interface {
	// ScoreSubmit 对一次作答产出评分轨迹与错误推断（trace 形态 =
	// core/scoring buildTrace 落账原文；错误推断数组可为空）.
	ScoreSubmit(ctx context.Context, itemVersionID string, response map[string]any) (trace map[string]any, inferences []map[string]any, err error)
}

// ReviewSyncer 是复习队列入队写面的协议层接缝（P0-4，2026-08-31）：提交
// 落账成功后由作答链路触发同步（冻结设计「sync_review_queue 由作答链路
// 调用，API 不暴露」——本端口非业务端点，无独立路由）。nil = 同步面未装配
// → 提交照常成功，队列留待手动/后续同步自愈（派生队列可全量重建，不阻塞
// 作答证据入账——北极星：宁可少一个派生视图，不可丢一条作答证据）.
type ReviewSyncer interface {
	// SyncQueue 重放学生作答事件流，幂等同步复习队列；返回在队条目数.
	SyncQueue(ctx context.Context, studentAliasID, policyID, policyVersion string, now time.Time) (int, error)
}

// startSessionRequest 对齐 openapi-v1.1 StartSessionRequest（身份字段语义
// 见 createSession 的越权判据：student_alias_id 不再是授权输入，仅作冒用
// 判据 peek）.
type startSessionRequest struct {
	Gradeband      string   `json:"gradeband"`
	Scene          string   `json:"scene"`
	PaperID        *string  `json:"paper_id"`
	ItemVersionIDs []string `json:"item_version_ids"`
	RetestWrong    bool     `json:"retest_wrong"`
	StudentAliasID string   `json:"student_alias_id"`
}

// startSessionResponse 对齐 openapi-v1.1 StartSessionResponse.
type startSessionResponse struct {
	SessionID    string `json:"session_id"`
	Status       string `json:"status"`
	Scene        string `json:"scene"`
	Gradeband    string `json:"gradeband"`
	Total        int    `json:"total"`
	TimeLimitSec int32  `json:"time_limit_sec"`
}

// sessionStateResponse 对齐 openapi-v1.1 SessionState（时长判定字段由服务域
// 装配：elapsed/remaining 是业务计算，协议层零业务规则）.
type sessionStateResponse struct {
	SessionID        string  `json:"session_id"`
	Status           string  `json:"status"`
	Scene            string  `json:"scene"`
	Gradeband        string  `json:"gradeband"`
	PaperID          *string `json:"paper_id"`
	Total            int     `json:"total"`
	MainAnswered     int     `json:"main_answered"`
	AnsweredCount    int     `json:"answered_count"`
	CorrectCount     int     `json:"correct_count"`
	WrongCount       int     `json:"wrong_count"`
	RetestPending    int     `json:"retest_pending"`
	ElapsedActiveSec int     `json:"elapsed_active_sec"`
	TimeLimitSec     int     `json:"time_limit_sec"`
	RemainingSec     int     `json:"remaining_sec"`
	StartedAt        string  `json:"started_at"`
	CompletedAt      *string `json:"completed_at,omitempty"`
}

// nextItemResponse 对齐 openapi-v1.1 NextItemResponse：done=true 时仅含 done
// （其余字段 omitempty）；A4 追溯锚 placement_token/item_version_id 随题出示.
type nextItemResponse struct {
	Done           bool    `json:"done"`
	PlacementToken *string `json:"placement_token,omitempty"`
	ItemVersionID  string  `json:"item_version_id,omitempty"`
}

// submitResponseRequest 对齐 openapi-v1.1 SubmitResponseRequest.
type submitResponseRequest struct {
	ItemVersionID string         `json:"item_version_id"`
	Response      map[string]any `json:"response"`
	DurationMs    *int32         `json:"duration_ms"`
}

// progressResponse 是 Feedback.progress（additionalProperties 均整数）.
type progressResponse struct {
	Total         int `json:"total"`
	MainAnswered  int `json:"main_answered"`
	AnsweredCount int `json:"answered_count"`
	CorrectCount  int `json:"correct_count"`
}

// feedbackResponse 对齐 openapi-v1.1 Feedback（required 七面齐备；
// error_feedback/explanation 属内容域装配面——会话域不伪造，按契约缺省）.
type feedbackResponse struct {
	EventID         string             `json:"event_id"`
	Correct         bool               `json:"correct"`
	DimensionScores map[string]float64 `json:"dimension_scores"`
	ErrorInferences []map[string]any   `json:"error_inferences"`
	Progress        progressResponse   `json:"progress"`
	SessionStatus   string             `json:"session_status"`
}

// sessionStateJSON 把服务域状态投影映射为契约响应（时刻统一 RFC3339/UTC
// 编码——time.Time 的 JSON 形态，time_format=date-time 同构）.
func sessionStateJSON(res *session.StateResult) sessionStateResponse {
	out := sessionStateResponse{
		SessionID:        res.SessionID,
		Status:           res.Status,
		Scene:            res.Scene,
		Gradeband:        res.Gradeband,
		PaperID:          res.PaperID,
		Total:            res.Total,
		MainAnswered:     res.MainAnswered,
		AnsweredCount:    res.AnsweredCount,
		CorrectCount:     res.CorrectCount,
		WrongCount:       res.WrongCount,
		RetestPending:    res.RetestPending,
		ElapsedActiveSec: res.ElapsedActiveSec,
		TimeLimitSec:     res.TimeLimitSec,
		RemainingSec:     res.RemainingSec,
		StartedAt:        res.StartedAt.UTC().Format(rfc3339UTC),
	}
	if res.CompletedAt != nil {
		t := res.CompletedAt.UTC().Format(rfc3339UTC)
		out.CompletedAt = &t
	}
	return out
}

// rfc3339UTC 是契约 date-time 的出账形态（UTC + 秒精度，跨语言可互验）.
const rfc3339UTC = "2006-01-02T15:04:05Z07:00"

// sessionPrincipal 从已认证上下文取主体（纵深防御：中间件保证存在，缺失
// 即装配破坏，按「身份不可信」fail-closed 401——惯例同 aliasBoundRead）.
func sessionPrincipal(w http.ResponseWriter, r *http.Request) (aliasID string, ok bool) {
	p, ok := middleware.FromContext(r.Context())
	if !ok {
		log.Printf("auth denied class=%q reason=principal-missing route=%s", middleware.ErrorClassUnauthorized, r.Pattern) // route=服务端常量，禁落 r.URL.Path（go/log-injection）
		writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
		return "", false
	}
	return p.AliasID, true
}

// writeSessionError 是会话域错误的统一映射矩阵（与 middleware.MapError 同源
// 兜底：会话哨兵在前，其余原样交给统一矩阵——授权/认证/未知类不分叉）.
// 完整原因只进服务端日志（脱敏纪律），响应体零内部细节.
func writeSessionError(w http.ResponseWriter, err error) {
	var rre *session.RestRequiredError
	var ose *session.OutOfSequenceError
	var reason string
	if err != nil {
		reason = middleware.Mask(err.Error())
	}
	var status int
	var class string
	switch {
	case errors.Is(err, errInvalidSubmitBody):
		status, class = http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest
	case errors.Is(err, session.ErrSessionNotFound):
		status, class = http.StatusNotFound, middleware.ErrorClassNotFound
	case errors.Is(err, session.ErrNotSessionOwner):
		status, class = http.StatusForbidden, middleware.ErrorClassForbidden
	case errors.As(err, &rre), errors.As(err, &ose),
		errors.Is(err, session.ErrSessionCompleted),
		errors.Is(err, session.ErrSessionState),
		errors.Is(err, session.ErrRetestRoundUnavailable):
		status, class = http.StatusConflict, ErrorClassConflict
	case errors.Is(err, session.ErrInvalidSessionStart),
		errors.Is(err, session.ErrInvalidSubmission),
		errors.Is(err, session.ErrInvalidTopicOrder),
		errors.Is(err, session.ErrPaperSequenceUnavailable):
		status, class = http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest
	default:
		// 授权门（403）/认证（401）/基础设施故障（500）统一矩阵兜底，
		// 绝不自造第四种映射（单点扩展纪律）.
		middleware.HandleError(w, err)
		return
	}
	log.Printf("session error status=%d class=%q reason=%s", status, class, reason)
	middleware.WriteError(w, status, class)
}

// writeJSON 编码成功响应（编码失败已无降级通道，记日志留痕——惯例同
// writeErrorClass）.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("session response encode failure class=%T", err)
	}
}

// decodeSessionBody 解码 JSON 请求体（体限已在边界层收口）。空体按零值
// 请求处理（契约 requestBody required 的违例由业务校验显式拒绝——错误
// 归类单一来源在服务域哨兵）.
func decodeSessionBody(r *http.Request, v any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxPlaceholderBodyBytes+1))
	if err != nil {
		return err
	}
	if len(body) == 0 {
		return nil
	}
	return json.Unmarshal(body, v)
}

// createSession 是 POST /sessions 的业务接线（升级自 501 占位）。处理序
// （顺序即安全论证，不得重排——api.go 的处理序注释继续有效）：
//
//	认证（盾）→ 越权判据（请求体 alias 冒用）→ 业务：授权门 → 题序固化
//
// 服务未装配时保留「授权门 → 501」的兼容形态（既有调用方/测试不变，
// 生产行为由 NewRouterWithSessions 装配）.
func createSession(store compliance.ConsentStore, svc *session.Service) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p, ok := middleware.FromContext(r.Context())
		if !ok {
			// 纵深防御，同 sessionPrincipal。
			log.Printf("auth denied class=%q reason=principal-missing route=%s", middleware.ErrorClassUnauthorized, r.Pattern) // route=服务端常量，禁落 r.URL.Path（go/log-injection）
			writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
			return
		}
		var req startSessionRequest
		body, err := io.ReadAll(io.LimitReader(r.Body, maxPlaceholderBodyBytes+1))
		if err == nil && len(body) <= maxPlaceholderBodyBytes && len(body) > 0 {
			// 解析失败/字段缺省留给业务层的契约校验（T-W5-008 错误映射统一）；
			// 此处只关心「能确凿读出的 alias 是否冒用他人身份」。
			if jerr := json.Unmarshal(body, &req); jerr == nil && req.StudentAliasID != "" {
				if aerr := auth.AssertOwnsAlias(p, req.StudentAliasID); aerr != nil {
					middleware.WriteAuthErrorResponse(w, aerr)
					return
				}
			}
		} else if err != nil {
			log.Printf("create_session body read error_class=%s", errClass(err))
		}
		if svc == nil {
			// 兼容形态：授权门 → 501 占位（api.go 原行为，授权门前置不变）.
			if cerr := requireConsentGate(r, store, p.AliasID, w); cerr {
				return
			}
			notImplemented(w, r)
			return
		}
		res, err := svc.Start(r.Context(), p.AliasID, session.StartParams{
			Scene:          req.Scene,
			Gradeband:      req.Gradeband,
			PaperID:        req.PaperID,
			ItemVersionIDs: req.ItemVersionIDs,
			RetestWrong:    req.RetestWrong,
		})
		if err != nil {
			writeSessionError(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, startSessionResponse{
			SessionID:    res.SessionID,
			Status:       res.Status,
			Scene:        res.Scene,
			Gradeband:    res.Gradeband,
			Total:        res.Total,
			TimeLimitSec: res.TimeLimitSec,
		})
	}
}

// requireConsentGate 是兼容形态的授权门（T-W5-010 行为逐字保留：分型审计
// 日志 + 统一错误映射）。返回 true 表示已写出响应.
func requireConsentGate(r *http.Request, store compliance.ConsentStore, aliasID string, w http.ResponseWriter) bool {
	cerr := session.RequireOnlinePracticeConsent(r.Context(), store, aliasID)
	if cerr == nil {
		return false
	}
	var consentErr *compliance.ConsentRequiredError
	if errors.As(cerr, &consentErr) {
		// 审计行：细分 reason 令牌 + Error() 载荷（alias 已 Quote 防日志注入；
		// 细分仅落本日志，响应体仍为粗粒度 403）.
		log.Printf("consent denied class=%q reason=consent_%s %s",
			middleware.ErrorClassForbidden, consentErr.State, consentErr.Error())
	} else {
		log.Printf("consent gate unavailable class=%q reason_class=%T fail_closed=denied",
			middleware.ErrorClassInternal, cerr)
	}
	middleware.HandleError(w, cerr)
	return true
}

// sessionState 是 GET /sessions/{session_id}（会话状态投影）.
func sessionState(svc *session.Service) http.HandlerFunc {
	if svc == nil {
		return sessionScoped
	}
	return func(w http.ResponseWriter, r *http.Request) {
		alias, ok := sessionPrincipal(w, r)
		if !ok {
			return
		}
		res, err := svc.State(r.Context(), alias, r.PathValue("session_id"), zeroTime)
		if err != nil {
			writeSessionError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, sessionStateJSON(res))
	}
}

// sessionNext 是 GET /sessions/{session_id}/next（取下一题；完成 → done）.
func sessionNext(svc *session.Service) http.HandlerFunc {
	if svc == nil {
		return sessionScoped
	}
	return func(w http.ResponseWriter, r *http.Request) {
		alias, ok := sessionPrincipal(w, r)
		if !ok {
			return
		}
		res, err := svc.GetNext(r.Context(), alias, r.PathValue("session_id"), zeroTime)
		if err != nil {
			writeSessionError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, nextItemResponse{
			Done:           res.Done,
			PlacementToken: res.PlacementToken,
			ItemVersionID:  res.ItemVersionID,
		})
	}
}

// sessionSubmit 是 POST /sessions/{session_id}/responses（提交作答：评分
// 先行 → 提交临界区落账 → 复习队列同步 → 反馈投影）。评分链路未装配时
// fail-closed 501——绝不以「无评分」作答入账（残缺评分不落账，submit.go
// 契约）。复习队列同步失败不拒绝已落账的提交（派生队列可全量重建——
// 见 ReviewSyncer 端口注释），错误类名落日志供运维定位.
func sessionSubmit(svc *session.Service, scorer ResponseScorer, syncer ReviewSyncer) http.HandlerFunc {
	if svc == nil {
		return sessionScoped
	}
	return func(w http.ResponseWriter, r *http.Request) {
		alias, ok := sessionPrincipal(w, r)
		if !ok {
			return
		}
		var req submitResponseRequest
		if err := decodeSessionBody(r, &req); err != nil {
			log.Printf("submit decode failure class=%q reason_class=%T", middleware.ErrorClassBadRequest, err)
			middleware.WriteError(w, http.StatusBadRequest, middleware.ErrorClassBadRequest)
			return
		}
		if req.ItemVersionID == "" || req.Response == nil {
			writeSessionError(w, errInvalidSubmitBody)
			return
		}
		if scorer == nil {
			log.Printf("submit rejected class=%q reason=scorer-not-wired fail_closed=denied", ErrorClassNotImplemented)
			writeErrorClass(w, http.StatusNotImplemented, ErrorClassNotImplemented)
			return
		}
		trace, inferences, serr := scorer.ScoreSubmit(r.Context(), req.ItemVersionID, req.Response)
		if serr != nil {
			// 评分失败不落账（评分先行的原子前提）；基础设施语义交统一矩阵.
			middleware.HandleError(w, serr)
			return
		}
		res, err := svc.Submit(r.Context(), alias, session.SubmitInput{
			SessionID:       r.PathValue("session_id"),
			ItemVersionID:   req.ItemVersionID,
			Response:        req.Response,
			DurationMs:      req.DurationMs,
			ScoringTrace:    trace,
			ErrorInferences: inferences,
		})
		if err != nil {
			writeSessionError(w, err)
			return
		}
		if syncer != nil {
			// P0-4：作答链路触发的复习队列同步（v1 默认策略）。同步在提交事务
			// 之外（派生账独立事务）；失败仅记日志——队列由全量重放自愈.
			if _, sErr := syncer.SyncQueue(r.Context(), alias, review.DefaultPolicyID, review.DefaultPolicyVersion, time.Now().UTC()); sErr != nil {
				log.Printf("review sync failure route=%s error_class=%s", r.Pattern, errClass(sErr))
			}
		}
		writeJSON(w, http.StatusOK, feedbackResponse{
			EventID:         res.EventID,
			Correct:         res.Correct,
			DimensionScores: res.DimensionScores,
			ErrorInferences: res.ErrorInferences,
			Progress: progressResponse{
				Total:         res.Progress.Total,
				MainAnswered:  res.Progress.MainAnswered,
				AnsweredCount: res.Progress.AnsweredCount,
				CorrectCount:  res.Progress.CorrectCount,
			},
			SessionStatus: res.SessionStatus,
		})
	}
}

// sessionResume 是 POST /sessions/{session_id}/resume（休息确认）.
func sessionResume(svc *session.Service) http.HandlerFunc {
	if svc == nil {
		return sessionScoped
	}
	return func(w http.ResponseWriter, r *http.Request) {
		alias, ok := sessionPrincipal(w, r)
		if !ok {
			return
		}
		res, err := svc.Resume(r.Context(), alias, r.PathValue("session_id"), zeroTime)
		if err != nil {
			writeSessionError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, sessionStateJSON(res))
	}
}

// sessionAbandon 是 POST /sessions/{session_id}/abandon（放弃会话）.
func sessionAbandon(svc *session.Service) http.HandlerFunc {
	if svc == nil {
		return sessionScoped
	}
	return func(w http.ResponseWriter, r *http.Request) {
		alias, ok := sessionPrincipal(w, r)
		if !ok {
			return
		}
		res, err := svc.Abandon(r.Context(), alias, r.PathValue("session_id"), zeroTime)
		if err != nil {
			writeSessionError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, sessionStateJSON(res))
	}
}
