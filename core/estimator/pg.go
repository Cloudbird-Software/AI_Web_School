package estimator

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// PGStore 是 ActivePointerStore 的 PG 生产实现.
//
// 并发临界区构成（SetActive 在调用方显式事务内依次执行，语句全部来自
// db/queries/estimator.sql 的类型安全生成方法）：LockEstimatorScope（per-scope
// advisory xact lock）→ GetActiveEstimatorRun（幂等判定）→ RetireActiveEstimatorRun
// （先退役腾出偏唯一索引谓词槽位）→ InsertEstimatorRun（后插入）；偏唯一索引
// uq_estimator_run_one_active_per_scope 是最后一道防线，其拒绝（SQLSTATE 23505）
// 被翻译为哨兵错误 ErrActiveConflict 而非驱动异常穿透（验收 #2「不出现异常泄漏」）。
//
// 为什么用 advisory xact lock 而不是对活跃行 SELECT ... FOR UPDATE：首次登记时该
// scope 尚无行可锁，行锁方案只能退化为完全依赖唯一索引兜底（冲突即重试）；advisory
// 锁在空表场景同样串行化「检查→退役→插入」，把并发正确性前移到应用可控层。事务结束
// 自动释放，无需手工解锁。（行锁备选语义留档：对 retired_at IS NULL 行加 FOR UPDATE，
// 若采用须接受首插竞态下的 ErrActiveConflict 重试面。）
//
// 事务纪律（S4/D11）：本类型不持有连接、不自 begin/commit——一次业务切换 =
// 一个外层事务；q 必须是调用方已 begin 的事务执行面，连接装配在 W6 服务化接线.
type PGStore struct{}

// NewPGStore 构造 PG 实现.
func NewPGStore() *PGStore { return &PGStore{} }

// SetActive 实现 ActivePointerStore：完整切换临界区见类型注释.
func (s *PGStore) SetActive(ctx context.Context, q Executor, in SetInput) (*EstimatorRun, bool, error) {
	if q == nil {
		return nil, false, errors.New("estimator/pg: 未传入事务执行面（D11 要求显式传递事务边界）")
	}
	if err := validateSetInput(in); err != nil {
		return nil, false, err
	}
	actor := in.ActivatedBy
	if actor == "" {
		actor = SystemActor
	}
	ts := in.ActivatedAt
	if ts.IsZero() {
		ts = time.Now()
	}
	qs := dbgen.New(q)

	// 1) per-scope advisory lock：串行化该 scope 的全部切换者（含首次登记竞态）.
	if err := qs.LockEstimatorScope(ctx, string(in.PurposeScope)); err != nil {
		return nil, false, fmt.Errorf("estimator/pg advisory lock: %w", err)
	}
	cur, err := s.getActive(ctx, qs, in.PurposeScope, nil)
	if err != nil {
		return nil, false, err
	}
	// 2) 幂等判定：请求版本已是当前活跃版本——原样返回、不入账、不打退役戳
	// （天然幂等重放，验收 #2 允许的「幂等成功」分支）.
	if cur != nil && cur.ModelVersion == in.ModelVersion {
		return cur, false, nil
	}
	// 3) 先退役旧行：锁保证无人与我们并发改写这批行，两步序不撞偏唯一索引.
	if cur != nil {
		err := qs.RetireActiveEstimatorRun(ctx, dbgen.RetireActiveEstimatorRunParams{
			PurposeScope: string(in.PurposeScope),
			RetiredAt:    tsTZ(ts),
		})
		if err != nil {
			return nil, false, fmt.Errorf("estimator/pg retire old: %w", mapUniqueViolation(err))
		}
	}
	// 4) 后插入新活跃行；activated_by 为留痕的操作者维度（0025 列，验收 #3）.
	run := &EstimatorRun{
		RunID:           newRunID(),
		PurposeScope:    in.PurposeScope,
		ModelVersion:    in.ModelVersion,
		CodeDigest:      in.CodeDigest,
		InputSnapshotID: in.InputSnapshotID,
		GraphReleaseID:  in.GraphReleaseID,
		ActivatedBy:     actor,
		ActivatedAt:     ts,
	}
	err = qs.InsertEstimatorRun(ctx, dbgen.InsertEstimatorRunParams{
		RunID:           run.RunID,
		PurposeScope:    string(run.PurposeScope),
		ModelVersion:    run.ModelVersion,
		CodeDigest:      run.CodeDigest,
		InputSnapshotID: run.InputSnapshotID,
		GraphReleaseID:  run.GraphReleaseID,
		ActivatedBy:     run.ActivatedBy,
		ActivatedAt:     tsTZ(ts),
	})
	if err != nil {
		return nil, false, fmt.Errorf("estimator/pg insert new: %w", mapUniqueViolation(err))
	}
	return run.clone(), true, nil
}

// GetActive 实现 ActivePointerStore：asOf=nil 当前活跃；否则按时间回溯（D6 历史报告
// 引用当时版本）。无行返回 (nil, nil)——Python 冻结实现 scalar_one_or_none 的同义.
func (s *PGStore) GetActive(ctx context.Context, q Executor, scope PurposeScope, asOf *time.Time) (*EstimatorRun, error) {
	if q == nil {
		return nil, errors.New("estimator/pg: 未传入事务执行面（D11 要求显式传递事务边界）")
	}
	run, err := s.getActive(ctx, dbgen.New(q), scope, asOf)
	if err != nil {
		return nil, err
	}
	return run, nil
}

// getActive 是面向生成查询器的内部读路径（供 SetActive 复用，免二次判空 q）.
func (s *PGStore) getActive(ctx context.Context, qs *dbgen.Queries, scope PurposeScope, asOf *time.Time) (*EstimatorRun, error) {
	var (
		row dbgen.EstimatorRun
		err error
	)
	if asOf == nil {
		row, err = qs.GetActiveEstimatorRun(ctx, string(scope))
	} else {
		row, err = qs.GetActiveEstimatorRunAsOf(ctx, dbgen.GetActiveEstimatorRunAsOfParams{
			PurposeScope: string(scope),
			ActivatedAt:  tsTZ(*asOf),
		})
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("estimator/pg get active: %w", err)
	}
	return fromGen(&row), nil
}

// SwitchTrail 实现 ActivePointerStore：按 activated_at 升序把 append-only 行账还原成
// 「who/when/from/to」时间线——本行 who/to 取 activated_by / model_version，
// when 取 activated_at，From 取前一行的 model_version（首行即首次登记，From 为空）.
func (s *PGStore) SwitchTrail(ctx context.Context, q Executor, scope PurposeScope) ([]SwitchRecord, error) {
	if q == nil {
		return nil, errors.New("estimator/pg: 未传入事务执行面（D11 要求显式传递事务边界）")
	}
	rowsGen, err := dbgen.New(q).ListEstimatorRunLineage(ctx, string(scope))
	if err != nil {
		return nil, fmt.Errorf("estimator/pg switch trail: %w", err)
	}
	out := make([]SwitchRecord, 0, len(rowsGen))
	for i := range rowsGen {
		var from string
		if i > 0 {
			from = rowsGen[i-1].ModelVersion
		}
		r := fromGen(&rowsGen[i])
		out = append(out, SwitchRecord{
			Who:   r.ActivatedBy,
			Scope: r.PurposeScope,
			From:  from,
			To:    r.ModelVersion,
			At:    r.ActivatedAt,
		})
	}
	return out, nil
}

// tsTZ 把领域时刻转为 pgtype 的 timestamptz 扫描/传参形状.
func tsTZ(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t, Valid: true}
}

// fromGen 把生成层行模型映射为包内领域模型（pgtype 可空性 → *time.Time 语义）.
func fromGen(r *dbgen.EstimatorRun) *EstimatorRun {
	out := &EstimatorRun{
		RunID:           r.RunID,
		PurposeScope:    PurposeScope(r.PurposeScope),
		ModelVersion:    r.ModelVersion,
		CodeDigest:      r.CodeDigest,
		InputSnapshotID: r.InputSnapshotID,
		GraphReleaseID:  r.GraphReleaseID,
		ActivatedBy:     r.ActivatedBy,
		ActivatedAt:     r.ActivatedAt.Time,
	}
	if r.RetiredAt.Valid {
		t := r.RetiredAt.Time
		out.RetiredAt = &t
	}
	return out
}

// mapUniqueViolation 把偏唯一索引拒绝翻译为哨兵错误 ErrActiveConflict
// （errors.Is 可判）；非唯一冲突原样放行——异常不泄漏，但也绝不吞真故障.
func mapUniqueViolation(err error) error {
	var pe *pgconn.PgError
	if errors.As(err, &pe) && pe.Code == sqlStateUniqueViolation {
		// 双 %w：哨兵错误与原始驱动错误都留在 wrap 链里——调用方既能 errors.Is
		// 分支，也能回溯 SQLSTATE 证据（%v 会斩断链路，属吞错反模式）.
		return fmt.Errorf("%w: %w", ErrActiveConflict, err)
	}
	return err
}

// sqlStateUniqueViolation 是 PostgreSQL 唯一约束违反的 SQLSTATE。本地常量化而非
// 引 github.com/jackc/pgerrcode：避免为单个字符串比较把间接依赖升直接面.
const sqlStateUniqueViolation = "23505"
