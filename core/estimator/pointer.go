package estimator

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// PurposeScope 估计器使用的场景域（D5：三值域，禁止跨场景混估）.
type PurposeScope string

// 场景三值域（与 0016 迁移的 ck_estimator_run_purpose_scope_domain CHECK 一致）.
const (
	ScopePractice    PurposeScope = "practice"
	ScopeDiagnosis   PurposeScope = "diagnosis"
	ScopeMeasurement PurposeScope = "measurement"
)

// purposeScopes 为固定展示顺序的三值域（越域报错信息用，避免 map 遍历乱序）.
var purposeScopes = []PurposeScope{ScopePractice, ScopeDiagnosis, ScopeMeasurement}

// SystemActor 是登记主体不可考时的留痕回落值（与 0025 迁移的列默认值一致）.
const SystemActor = "system"

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏，验收 #2）.
var (
	// ErrInvalidScope 表示 purpose_scope 不在 D5 三值域内.
	ErrInvalidScope = errors.New("estimator: purpose_scope 越域（合法域 practice/diagnosis/measurement，D5）")

	// ErrEmptyModelVersion 表示未指定要登记的估计器版本（D6 版本可追溯的前提是版本非空）.
	ErrEmptyModelVersion = errors.New("estimator: model_version 不能为空")

	// ErrActiveConflict 表示偏唯一索引 uq_estimator_run_one_active_per_scope 拒绝了
	// 本次插入（SQLSTATE 23505）。advisory lock 正常工作时不应出现；出现即视为
	// 数据库层防线的明确失败信号——返回本错误而非让驱动异常穿透（验收 #2 的
	// 「其余请求得到明确失败」）.
	ErrActiveConflict = errors.New("estimator: 同场景活跃指针唯一性冲突（23505），请重试")
)

// ValidPurposeScope 报告 scope 是否在 D5 三值域内.
func ValidPurposeScope(s PurposeScope) bool {
	for _, v := range purposeScopes {
		if s == v {
			return true
		}
	}
	return false
}

// validateSetInput 校验登记入参：越域与非空前置检查，把非法输入挡在临界区之外.
func validateSetInput(in SetInput) error {
	if !ValidPurposeScope(in.PurposeScope) {
		return fmt.Errorf("%w: %q", ErrInvalidScope, in.PurposeScope)
	}
	if in.ModelVersion == "" {
		return ErrEmptyModelVersion
	}
	return nil
}

// EstimatorRun 对应 estimator_run 表一行（0016 全列 + 0025 的 activated_by）.
//
// 为什么建模整行而非仅 (scope, version)：D6 要求历史报告可回溯「当时版本的
// 完整实证链」（代码 digest、输入快照、图谱 release），缺一列即断证.
type EstimatorRun struct {
	RunID           string
	PurposeScope    PurposeScope
	ModelVersion    string
	CodeDigest      string
	InputSnapshotID string
	GraphReleaseID  string
	ActivatedBy     string
	ActivatedAt     time.Time
	// RetiredAt 为 nil 表示该行是当前活跃指针；非 nil 即已被后续版本退役.
	RetiredAt *time.Time
}

// clone 返回深拷贝：内存实现对外交出副本而非内部状态指针，
// 避免调用方在读锁外触碰会被并发写翻牌的字段（-race 干净的前提）.
func (r *EstimatorRun) clone() *EstimatorRun {
	out := *r
	if r.RetiredAt != nil {
		t := *r.RetiredAt
		out.RetiredAt = &t
	}
	return &out
}

// SetInput 是一次 set_active 登记请求（对应 Python 冻结实现的同名列参集 +
// 新增 ActivatedBy 以满足验收 #3 的「谁」）.
type SetInput struct {
	PurposeScope    PurposeScope
	ModelVersion    string
	CodeDigest      string
	InputSnapshotID string
	GraphReleaseID  string
	// ActivatedBy 登记「谁」执行的切换；空值回落为 SystemActor（0025 列默认值）.
	ActivatedBy string
	// ActivatedAt 为零值 time.Time 时取当前时刻；允许显式回填历史登记时刻
	// （Python 冻结实现的 activated_at 可选参语义）.
	ActivatedAt time.Time
}

// SwitchRecord 是一条切换留痕：谁在何时把某 scope 从版本 From 切到 To（验收 #3）.
// 只增不改（append-only）：内存实现落在只追加切片；PG 侧即 estimator_run 的新增行
// （本行为谁/何时/去哪，前驱行的 ModelVersion 即从哪），并由 SwitchTrail 还原时间线.
type SwitchRecord struct {
	Who   string
	Scope PurposeScope
	// From 为空串表示该 scope 此前无活跃版本（首次登记不是「切换」但同样入账，
	// 保证账目与活跃指针的变化一一对应）.
	From string
	To   string
	At   time.Time
}

// Executor 是指针读写所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX 同构.
//
// 为什么抽象而不直接写死 pgx.Tx：生产装配方把调用方持有的 pgx.Tx / pgxpool 连接
// 作为 Executor 显式传入（两者方法集均覆盖本接口），事务边界因此留在最外层
// 调用方——领域服务不自 begin/commit（S4/D11）；内存实现不持久化，忽略该参数.
// 全部语句文本只住在 db/queries/estimator.sql（SQL-2：不在 Go 拼 SQL），
// 经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// 编译期锚定一：pgx.Tx 必须满足 Executor（W6 装配直通的假设防线）.
var _ Executor = (pgx.Tx)(nil)

// 编译期锚定二：Executor 必须满足生成层执行面 dbgen.DBTX——PGStore 内部用
// dbgen.New(q) 构造类型安全查询器；sqlc 升级改形状时在此第一时间红.
var _ dbgen.DBTX = Executor(nil)

// 编译期锚定三：两种实现都必须兑现 ActivePointerStore 的并发契约.
var (
	_ ActivePointerStore = (*MemoryStore)(nil)
	_ ActivePointerStore = (*PGStore)(nil)
)

// ActivePointerStore 是活跃指针存储的语义契约.
//
// 并发契约（本卡核心交付）：SetActive 对同一 PurposeScope 构成单一原子临界区，
// 并发调用互斥串行化（内存=互斥锁；PG=per-scope advisory xact lock + 偏唯一索引
// 兜底），每次调用要么完整完成「退役旧活跃 + 登记新活跃 + 追加留痕」，要么整体
// 未发生并返回明确 error。幂等约定：请求的 ModelVersion 已是该 scope 当前活跃
// 版本时，原样返回现指针且 switched=false、不入账（天然幂等重放，验收 #2 允许的
// 「幂等成功」分支）.
type ActivePointerStore interface {
	// SetActive 登记 scope 下新的活跃估计器版本；switched=false 表示幂等命中.
	SetActive(ctx context.Context, q Executor, in SetInput) (*EstimatorRun, bool, error)
	// GetActive 取 scope 当前活跃版本；asOf 非 nil 时回溯 asOf 当时活跃的版本
	// （D6 历史报告引用当时版本）；无则返回 (nil, nil).
	GetActive(ctx context.Context, q Executor, scope PurposeScope, asOf *time.Time) (*EstimatorRun, error)
	// SwitchTrail 按 At 升序返回 scope 的全部切换留痕（append-only 账的只读投影）.
	SwitchTrail(ctx context.Context, q Executor, scope PurposeScope) ([]SwitchRecord, error)
}
