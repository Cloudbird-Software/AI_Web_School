// assembly.go 承载 GO-RW-002 的生产装配面：pgx 连接池事务执行器与作答评分桥。
//
// 为什么住在这里而不是 core/session：D11 静态守卫（core/session 的 guard 测试）
// 禁止领域源码出现 Commit/Rollback 调用面——事务边界归装配层；评分桥同理是
// 组合根的粘合（内容账 dbgen.GetItemVersion × core/scoring 注册表），不是会话
// 域知识。api.ResponseScorer 端口由本文件兑现.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/review"
	"github.com/Cloudbird-Software/AI_Web_School/core/scoring"
	"github.com/Cloudbird-Software/AI_Web_School/core/session"
	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
	"github.com/jackc/pgx/v5/pgxpool"
)

// poolTxRunner 是会话域 TxRunner 的生产实现：pgxpool 上 Begin → fn(tx) →
// Commit / Rollback。fn 错误即回滚；回滚自身失败与原错误一并上抛（绝不吞
// 驱动故障）。pgx.Tx 满足 session.Executor（core/session 编译期锚定一）.
type poolTxRunner struct {
	pool *pgxpool.Pool
}

// InTx 实现 session.TxRunner.
func (r *poolTxRunner) InTx(ctx context.Context, fn func(q session.Executor) error) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("school: begin session tx: %w", err)
	}
	if err := fn(tx); err != nil {
		if rbErr := tx.Rollback(ctx); rbErr != nil {
			return errors.Join(err, fmt.Errorf("school: rollback session tx: %w", rbErr))
		}
		return err
	}
	return tx.Commit(ctx)
}

// txReviewSyncer 是 api.ReviewSyncer 的生产实现（P0-4，2026-08-31）：每次
// 同步开一个独立事务——读事件投影 + upsert 队列条目同进同退（一次派生状态
// 写入 = 一个事务，S4/D11；与提交事务分离：派生队列滞后可由全量重放自愈，
// 绝不反向拖垮已入账的作答证据）.
type txReviewSyncer struct {
	pool *pgxpool.Pool
}

// SyncQueue 实现 api.ReviewSyncer：Begin → review.SyncService.SyncQueue →
// Commit / Rollback。fn 错误即回滚；回滚自身失败与原错误一并上抛.
func (t *txReviewSyncer) SyncQueue(ctx context.Context, studentAliasID, policyID, policyVersion string, now time.Time) (int, error) {
	tx, err := t.pool.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("school: begin review sync tx: %w", err)
	}
	n, err := review.NewSyncService(tx).SyncQueue(ctx, studentAliasID, policyID, policyVersion, now)
	if err != nil {
		if rbErr := tx.Rollback(ctx); rbErr != nil {
			return 0, errors.Join(err, fmt.Errorf("school: rollback review sync tx: %w", rbErr))
		}
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("school: commit review sync tx: %w", err)
	}
	return n, nil
}

// dbResponseScorer 是 api.ResponseScorer 的生产实现：内容账取 item_version
// → scoring_ref 解析（{scorer_id, scorer_params}，D4 冻结注册表键）→
// core/scoring.Runner 执行 → 落账形态 trace 直接上交（协议层零二次加工）.
type dbResponseScorer struct {
	pool   *pgxpool.Pool
	runner *scoring.Runner
}

// newDeterministicScorerTable 装配确定性评分器注册表（exact_match /
// math_equivalence / keypoint_hit / stepwise_rubric）。ai_rubric 需 LLM Caller
// 装订（core/ai 总线适配器），归 AI 波次接线；本装配不含——ai_rubric 条目的
// 作答在提交入口以 scorer 执行失败显式拒绝，绝不静默降级.
func newDeterministicScorerTable() (*registry.ScorerTable, error) {
	tb := registry.NewScorerTable()
	if err := scoring.RegisterDeterministicScorers(tb); err != nil {
		return nil, fmt.Errorf("school: 装配确定性评分器注册表: %w", err)
	}
	return tb, nil
}

// ScoreSubmit 实现 api.ResponseScorer。残缺 scoring_ref / 评分器执行失败
// 原样上抛——评分失败不落账（评分先行的原子前提），由协议层映射.
//
// 错误推断（2026-08-31 E2E 补齐，北极星断点修复）：评分轨迹显式判错时
// 经 inferErrorBindings 从 error_bindings 产推断（选项位次/选项值/answer 级
// 规则三形态，见 inference.go）——弱项报告与复习队列的数据源头.
func (s *dbResponseScorer) ScoreSubmit(ctx context.Context, itemVersionID string, response map[string]any) (map[string]any, []map[string]any, error) {
	row, err := dbgen.New(s.pool).GetItemVersion(ctx, itemVersionID)
	if err != nil {
		return nil, nil, fmt.Errorf("school: read item_version %s: %w", itemVersionID, err)
	}
	trace, _, err := scoreAgainstRef(ctx, row.ScoringRef, response, s.runner)
	if err != nil {
		return nil, nil, err
	}
	wrongExplicit := traceWrongExplicit(trace)
	inferences := inferErrorBindings(row.InteractionRef, row.Content, row.ErrorBindings, response, itemVersionID, wrongExplicit)
	return trace, inferences, nil
}

// traceWrongExplicit 从评分轨迹提取显式判错（契约 §3 trace.process.correct；
// 缺失=未显式判定，不产推断——不猜对错）.
func traceWrongExplicit(trace map[string]any) bool {
	process, ok := trace["process"].(map[string]any)
	if !ok {
		return false
	}
	c, ok := process["correct"].(bool)
	return ok && !c
}

// scoreAgainstRef 是评分桥的纯函数面（无 DB，可测）：scoring_ref JSONB →
// 评分器执行 → trace。作答载荷整体 JSON 序列化为评分器 answer 面（作答原文
// 只落 response_event.raw_payload，trace 只留摘要——职责分离）.
func scoreAgainstRef(ctx context.Context, scoringRef []byte, response map[string]any, runner *scoring.Runner) (map[string]any, []map[string]any, error) {
	var ref struct {
		ScorerID     string         `json:"scorer_id"`
		ScorerParams map[string]any `json:"scorer_params"`
	}
	if err := json.Unmarshal(scoringRef, &ref); err != nil {
		return nil, nil, fmt.Errorf("school: scoring_ref 解析失败: %w", err)
	}
	if ref.ScorerID == "" {
		return nil, nil, errors.New("school: scoring_ref 缺 scorer_id（D4：评分器只能来自注册表）")
	}
	answer, err := json.Marshal(response)
	if err != nil {
		return nil, nil, fmt.Errorf("school: 作答载荷序列化失败: %w", err)
	}
	run, err := runner.Run(ctx, scoring.RunInput{
		ScorerID: ref.ScorerID,
		Answer:   string(answer),
		Params:   ref.ScorerParams,
	})
	if err != nil {
		return nil, nil, fmt.Errorf("school: 评分执行失败: %w", err)
	}
	// 错误推断数组：确定性评分器的 evidence 面不含错误推断——推断由调用方
	// ScoreSubmit 经 inferErrorBindings 从 error_bindings 加工（见 inference.go）.
	return run.Trace, []map[string]any{}, nil
}
