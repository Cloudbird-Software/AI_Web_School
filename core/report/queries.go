// 报告域只读查询服务（GO-RW-005 服务化接线 / 审计 #155）：
// GET /reports/weakness 的全部 DB 取证经本服务，api 层零 SQL、零行归零知识。
//
// 装配纪律：与 core/content.ContentQueryService 同构——不持有连接、不开事务
// （只读路径跑连接池默认读面，单语句自含一致性）；db 为 nil 构造不报错但
// 全部查询立即返回 ErrNoExecutor（fail-closed 落在调用路径）。
package report

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// ErrNoExecutor 是查询面未装配（db 为 nil）的哨兵。
var ErrNoExecutor = errors.New("report: 查询面未装配")

// Executor 是只读执行面最小接口（pgxpool 连接 / pgx.Tx 均满足）.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// WeaknessQueryService 是弱项报告的只读取证面。
type WeaknessQueryService struct {
	db Executor
	qs *dbgen.Queries
}

// NewWeaknessQueryService 把只读执行面绑定为报告查询服务。
func NewWeaknessQueryService(db Executor) *WeaknessQueryService {
	return &WeaknessQueryService{db: db, qs: dbgen.New(db)}
}

// InferenceEvents 取某学生的作答事件报告投影（按 D5 场景口径过滤；scene
// 为空表示跨场景汇总）。error_inferences JSONB 反序列化失败是账面数据被
// 越权写入的信号，fail-loud 不静默跳过。
func (s *WeaknessQueryService) InferenceEvents(ctx context.Context, studentAliasID string, scene string) ([]InferenceEventView, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	alias, err := parseUUID(studentAliasID)
	if err != nil {
		return nil, err
	}
	rows, err := s.qs.ListInferenceEventsByStudent(ctx, dbgen.ListInferenceEventsByStudentParams{
		StudentAliasID: alias,
		Column2:        scene,
	})
	if err != nil {
		return nil, fmt.Errorf("report: list inference events: %w", err)
	}
	views := make([]InferenceEventView, 0, len(rows))
	for _, row := range rows {
		var inferences []map[string]any
		if len(row.ErrorInferences) > 0 {
			if err := json.Unmarshal(row.ErrorInferences, &inferences); err != nil {
				return nil, fmt.Errorf("report: error_inferences 反序列化失败（账面数据非法）: %w", err)
			}
		}
		views = append(views, InferenceEventView{
			ItemVersionID:   row.ItemVersionID,
			ErrorInferences: inferences,
		})
	}
	return views, nil
}

// Recommended 为已定论错误类型取针对性练习小卷：已发布池按 error_bindings
// 含该错误类型过滤，剔除产生过证据的题目版本（ContributingItemVersionIDs
// 的排除集语义），确定性升序取 limit 条.
func (s *WeaknessQueryService) Recommended(ctx context.Context, errorTypeID string, exclude []string, limit int) ([]string, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	if limit <= 0 {
		return nil, nil
	}
	ids, err := s.qs.ListRecommendedItemVersions(ctx, dbgen.ListRecommendedItemVersionsParams{
		Column1: errorTypeID,
		Column2: exclude,
		Limit:   int32(limit),
	})
	if err != nil {
		return nil, fmt.Errorf("report: list recommended: %w", err)
	}
	return ids, nil
}

func parseUUID(s string) (pgtype.UUID, error) {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil {
		return pgtype.UUID{}, fmt.Errorf("report: student_alias_id 非法 UUID: %w", err)
	}
	return u, nil
}
