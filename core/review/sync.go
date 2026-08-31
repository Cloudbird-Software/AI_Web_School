// sync.go 承载复习队列入队写路径（P0-4，2026-08-31 补齐；Python 冻结实现
// src/core/review/service.py::sync_review_queue 的 Go 重锚定）。
//
// 全量重放语义：读 response_event（只读 SELECT——作答事件账永不被本模块写），
// 经 scheduler.RebuildQueue 纯函数重放，幂等 upsert 进 review_queue_entry
// （派生队列，非三本账，允许 UPDATE）。同一事件流 × 同一策略版本重放必得
// 同态（R-Z-07「队列版本可重建」），因此：
//   - 重复同步结果不变（upsert 冲突面 IS DISTINCT FROM 判据，无变化零写入）；
//   - 崩溃/并发中断的半程同步可由下次同步自愈（全量重建天然收敛）。
//
// 事务纪律（S4/D11）：本服务不自 commit——Executor 既可以是 pgxpool（逐语句
// 自动提交，幂等性兜底），也可以是调用方已 begin 的事务（推荐：一次同步 =
// 一个派生状态写入，装配层经事务包装兑现，见 cmd/school 的 txReviewSyncer）。
//
// 宪法 A5：本模块不 import 任何学科包/学段包。
package review

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// ErrPolicyNotFound 是策略版本不存在（迁移 0012 未执行或 policy_id/version
// 拼写错）的哨兵错误——派生队列无策略不可排程，fail-closed 上抛由调用方定夺.
var ErrPolicyNotFound = errors.New("review: 策略版本不存在")

// ErrLedgerCorrupted 是事件账投影不可解析的哨兵（JSONB 反序列化失败按账损
// 处理——fail-closed 拒绝同步，不带病排程；与 core/session 同判据）.
var ErrLedgerCorrupted = errors.New("review: 事件账投影损坏")

// SyncService 是复习队列的同步写面（sync_review_queue 的 Go 侧兑现）。
//
// 与 DueQueryService 同构的装配纪律：不持有连接、不开事务；db 为 nil 构造
// 不报错但 SyncQueue 立即返回 ErrNoExecutor（fail-closed）。
type SyncService struct {
	db Executor
	qs *dbgen.Queries
}

// NewSyncService 把执行面绑定为复习同步服务（pgxpool 连接 / pgx.Tx 均满足
// Executor；推荐事务形态见文件头注释）.
func NewSyncService(db Executor) *SyncService {
	return &SyncService{db: db, qs: dbgen.New(db)}
}

// SyncQueue 重放学生作答事件流，幂等同步复习队列（全量重建语义）。
//
// 错题（含错误推断的事件）自动入队；答对推进 stage；走完末间隔出队。
// 返回本次在队（pending/done）的条目数。事件排序由 SQL 面 ORDER BY
// created_at, event_id 保证（乱序输入会破坏状态机语义，与冻结实现同口径）。
func (s *SyncService) SyncQueue(ctx context.Context, studentAliasID, policyID, policyVersion string, now time.Time) (int, error) {
	if s == nil || s.db == nil {
		return 0, ErrNoExecutor
	}
	alias, err := parseUUID(studentAliasID)
	if err != nil {
		return 0, err
	}

	// 1) 策略间隔表：派生队列的排程依据（缺失即 ErrPolicyNotFound——不猜间隔）.
	prow, err := s.qs.GetReviewPolicy(ctx, dbgen.GetReviewPolicyParams{
		PolicyID:      policyID,
		PolicyVersion: policyVersion,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, fmt.Errorf("%w: policy_id=%q policy_version=%q", ErrPolicyNotFound, policyID, policyVersion)
		}
		return 0, fmt.Errorf("review: load policy: %w", err)
	}
	var intervals []int
	if err := json.Unmarshal(prow.IntervalsDays, &intervals); err != nil {
		return 0, fmt.Errorf("%w: intervals_days 非整数数组: %w", ErrLedgerCorrupted, err)
	}

	// 2) 事件投影 → 排程视图（对错判定走 DeriveCorrectness：显式 correct >
	// 错误推断非空 ⇒ 答错 > nil 未知不猜——与冻结实现同判据）.
	rows, err := s.qs.ListStudentReviewEvents(ctx, alias)
	if err != nil {
		return 0, fmt.Errorf("review: list events: %w", err)
	}
	events := make([]ReviewEventView, 0, len(rows))
	for _, row := range rows {
		var trace map[string]any
		if err := json.Unmarshal(row.ScoringTrace, &trace); err != nil {
			return 0, fmt.Errorf("%w: event %s scoring_trace: %w", ErrLedgerCorrupted, formatUUID(row.EventID.Bytes), err)
		}
		var inferences []map[string]any
		if err := json.Unmarshal(row.ErrorInferences, &inferences); err != nil {
			return 0, fmt.Errorf("%w: event %s error_inferences: %w", ErrLedgerCorrupted, formatUUID(row.EventID.Bytes), err)
		}
		typeIDs := make([]string, 0, len(inferences))
		for _, inf := range inferences {
			if id, ok := inf["error_type_id"].(string); ok && id != "" {
				typeIDs = append(typeIDs, id)
			}
		}
		events = append(events, ReviewEventView{
			EventID:       formatUUID(row.EventID.Bytes),
			ItemVersionID: row.ItemVersionID,
			CreatedAt:     row.CreatedAt.Time.UTC(),
			Correct:       DeriveCorrectness(trace, inferences),
			ErrorTypeIDs:  typeIDs,
		})
	}

	// 3) 纯函数核全量重放 → 每题队列状态.
	states, err := RebuildQueue(events, intervals)
	if err != nil {
		return 0, fmt.Errorf("review: rebuild queue: %w", err)
	}

	// 4) 幂等 upsert（entry_id 每次发新号，冲突面不更新它——在队身份保持稳定）.
	updatedAt := pgtype.Timestamptz{Time: now.UTC(), Valid: true}
	for itemVersionID, state := range states {
		entryID, err := randomUUIDV4()
		if err != nil {
			return 0, err
		}
		source := pgtype.Text{String: state.SourceErrorTypeID, Valid: state.SourceErrorTypeID != ""}
		lastEvent := pgtype.UUID{Bytes: [16]byte{}, Valid: false}
		if id, err := parseUUID(state.LastEventID); err == nil {
			lastEvent = id
		}
		if err := s.qs.UpsertReviewQueueEntry(ctx, dbgen.UpsertReviewQueueEntryParams{
			EntryID:           pgtype.UUID{Bytes: entryID, Valid: true},
			StudentAliasID:    alias,
			ItemVersionID:     itemVersionID,
			PolicyID:          policyID,
			PolicyVersion:     policyVersion,
			Stage:             int32(state.Stage),
			Status:            state.Status,
			SourceErrorTypeID: source,
			LastEventID:       lastEvent,
			EnqueuedAt:        pgtype.Timestamptz{Time: state.EnqueuedAt.UTC(), Valid: true},
			DueAt:             pgtype.Timestamptz{Time: state.DueAt.UTC(), Valid: true},
			UpdatedAt:         updatedAt,
		}); err != nil {
			return 0, fmt.Errorf("review: upsert entry %s: %w", itemVersionID, err)
		}
	}
	return len(states), nil
}

// randomUUIDV4 由 crypto/rand 直接构造 UUIDv4（冻结 uuid.uuid4 同义；标准库
// 即可满足，不为一个发号函数引第三方依赖——熵源不可用时报错而非发可重复 ID）.
func randomUUIDV4() ([16]byte, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return b, fmt.Errorf("review: 熵源不可用无法生成 entry_id: %w", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return b, nil
}
