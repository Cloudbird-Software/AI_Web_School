// Package paperdb 是 core/assembly.PaperItemSource 的 DB 实现（审计 #148
// 交付 4；编排层 orchestrator.go 文件头注释的既定归宿）。
//
// 分层纪律：只读 SELECT（宪法铁律 1 读侧）、零事务、零业务语义——published
// 过滤在 v_serving_item_version 视图、学科包×学段过滤在
// db/queries/serving.sql 的 WHERE（语句只住 queries 目录，SQL-2），本包只做
// 行→item_version dict 的 JSONB 解码与 fail-loud 包装。与 core/review /
// core/report 的查询服务同构：不持有连接、不开事务；db 为 nil 构造不报错
// 但全部查询立即返回 ErrNoExecutor（fail-closed）。
//
// dict 键面 = item_version_id / item_id / template_version_id / objective /
// interaction_ref / lineage / content——content 块是编排渲染（render.ItemToIR）
// 的必要输入，serving.sql 的投影已含 v.content（#147 落定），本包不解码即
// 透传其值树。坏 JSONB 行在装载期 fail-loud：坏行绝不静默混入候选池（与
// orchestrator.servingRowFromDict 对身份字段缺失的同一纪律）。
package paperdb

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/assembly"
	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ErrNoExecutor 是查询面未装配（db 为 nil）的哨兵（core/review 同名语义）.
var ErrNoExecutor = errors.New("paperdb: 查询面未装配")

// Executor 是只读执行面最小接口（pgxpool 连接 / pgx.Tx 均满足）.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// 编译期锚定：本实现即编排层题源端口（W6 装配直通的假设防线，api.ContentQueries
// 同款）.
var _ assembly.PaperItemSource = (*ItemSource)(nil)

// ItemSource 是组卷候选池的 sqlc 只读题源.
type ItemSource struct {
	db Executor
	qs *dbgen.Queries
}

// NewItemSource 把只读执行面绑定为组卷题源.
func NewItemSource(db Executor) *ItemSource {
	return &ItemSource{db: db, qs: dbgen.New(db)}
}

// LoadPublishedItemVersions 装载某 学科包×学段 的 published 候选题（dict
// 形态直通编排层；item_version_id 升序 = 池加载序确定，R-Z-01，排序语义在
// SQL）。任一行 JSONB 解码失败即整体失败（fail-loud，不返回部分池）.
func (s *ItemSource) LoadPublishedItemVersions(ctx context.Context, packID, gradeband string) ([]map[string]any, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	rows, err := s.qs.ListServingItemVersionsByPackGradeband(ctx, dbgen.ListServingItemVersionsByPackGradebandParams{
		PackID:    packID,
		Gradeband: gradeband,
	})
	if err != nil {
		return nil, fmt.Errorf("paperdb: 候选题装载失败: %w", err)
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		d, err := rowToDict(row)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, nil
}

// rowToDict 把 serving 行整形为编排层的 item_version dict：身份三键直通，
// JSONB 四块解码为值树。template_version_id 为 SQL NULL 时缺省该键（编排层
// servingRowFromDict 的形态约定：缺键 = 无母题版本引用）.
func rowToDict(row dbgen.ListServingItemVersionsByPackGradebandRow) (map[string]any, error) {
	objective, err := decodeJSONB(row.Objective)
	if err != nil {
		return nil, fmt.Errorf("paperdb: item_version %s 的 objective 块解码失败: %w", row.ItemVersionID, err)
	}
	interactionRef, err := decodeJSONB(row.InteractionRef)
	if err != nil {
		return nil, fmt.Errorf("paperdb: item_version %s 的 interaction_ref 块解码失败: %w", row.ItemVersionID, err)
	}
	lineage, err := decodeJSONB(row.Lineage)
	if err != nil {
		return nil, fmt.Errorf("paperdb: item_version %s 的 lineage 块解码失败: %w", row.ItemVersionID, err)
	}
	content, err := decodeJSONB(row.Content)
	if err != nil {
		return nil, fmt.Errorf("paperdb: item_version %s 的 content 块解码失败: %w", row.ItemVersionID, err)
	}
	d := map[string]any{
		"item_version_id": row.ItemVersionID,
		"item_id":         row.ItemID,
		"objective":       objective,
		"interaction_ref": interactionRef,
		"lineage":         lineage,
		"content":         content,
	}
	if row.TemplateVersionID.Valid {
		d["template_version_id"] = row.TemplateVersionID.String
	}
	return d, nil
}

// decodeJSONB JSONB 列字节 → 值树；空/null 列返回 nil map（语义与 JSON null
// 一致，由下游候选规范化面 fail-loud，本包不代行业务校验）.
func decodeJSONB(b []byte) (map[string]any, error) {
	if len(b) == 0 {
		return nil, nil
	}
	var v map[string]any
	if err := json.Unmarshal(b, &v); err != nil {
		return nil, err
	}
	return v, nil
}
