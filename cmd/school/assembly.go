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

// dbResponseScorer 是 api.ResponseScorer 的生产实现：内容账取 item_version
// → scoring_ref 解析（{scorer_id, scorer_params}，D4 冻结注册表键）→
// core/scoring.Runner 执行 → 落账形态 trace 直接上交（协议层零二次加工）.
type dbResponseScorer struct {
	pool       *pgxpool.Pool
	runner     *scoring.Runner
	errorTypes *scoring.ErrorTypeRegistry
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
func (s *dbResponseScorer) ScoreSubmit(ctx context.Context, itemVersionID string, response map[string]any) (map[string]any, []map[string]any, error) {
	row, err := dbgen.New(s.pool).GetItemVersion(ctx, itemVersionID)
	if err != nil {
		return nil, nil, fmt.Errorf("school: read item_version %s: %w", itemVersionID, err)
	}
	return scoreAgainstRef(ctx, row.ScoringRef, response, s.runner, s.errorTypes)
}

// scoreAgainstRef 是评分桥的纯函数面（无 DB，可测）：scoring_ref JSONB →
// 评分器执行 → trace + error_inferences。作答载荷整体 JSON 序列化为评分器
// answer 面（作答原文只落 response_event.raw_payload，trace 只留摘要——职责
// 分离）。
//
// 卡 #185 改造：从 run.Trace 的 evidence 面提取 error_inferences，经
// error_type 注册中心校验后回填返回数组（修复 assembly.go:100 恒空断点）。
// 未登记的 error_type_id 逐条丢弃——不伪造归因，不污染 response_error_type.
func scoreAgainstRef(ctx context.Context, scoringRef []byte, response map[string]any, runner *scoring.Runner, errorTypes *scoring.ErrorTypeRegistry) (map[string]any, []map[string]any, error) {
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
	// 错误推断数组：从评分 trace 的 evidence 面提取 error_inferences，经注册
	// 中心校验（未登记 id 丢弃）。桥对外恒返回非 nil 数组（nil 归空集）——
	// 协议层契约「可为空数组」的形态保证.
	inferences := scoring.ExtractErrorInferences(run.Trace, errorTypes)
	if inferences == nil {
		inferences = []map[string]any{}
	}
	return run.Trace, inferences, nil
}
