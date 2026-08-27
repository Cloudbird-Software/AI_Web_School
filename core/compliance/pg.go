package compliance

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

// PGStore 是 ConsentStore 的 PG 生产实现.
//
// 并发临界区构成（写入方法在调用方显式事务内依次执行，语句全部来自
// db/queries/consent.sql 的类型安全生成方法）：LockConsentChain（per-chain
// advisory xact lock）→ GetLatestConsentEvent（链顶版本与撤回前置校验）→
// InsertConsentEvent（版本 = 链顶+1 追加）；唯一索引
// uq_parental_consent_version_per_purpose 是最后一道防线，其拒绝（SQLSTATE
// 23505）被翻译为哨兵错误 ErrConsentConflict 而非驱动异常穿透。
//
// 为什么用 advisory xact lock 而不是对链顶行 SELECT ... FOR UPDATE：首次授权时该
// 链尚无行可锁，行锁方案只能退化为完全依赖唯一索引兜底（冲突即重试）；advisory
// 锁在空账场景同样串行化「读链顶→算版本→追加」，把并发正确性前移到应用可控层。
// 事务结束自动释放，无需手工解锁。append-only 触发器（0015）另行物理禁止任何
// UPDATE/DELETE 路径——本包 SQL 面（db/queries/consent.sql）根本没有此类语句.
//
// 事务纪律（S4/D11）：本类型不持有连接、不自 begin/commit——一次业务授权变更 =
// 一个外层事务；q 必须是调用方已 begin 的事务执行面，连接装配在 W6 服务化接线.
type PGStore struct{}

// NewPGStore 构造 PG 实现.
func NewPGStore() *PGStore { return &PGStore{} }

// RecordGrant 实现 ConsentStore：完整临界区见类型注释.
func (s *PGStore) RecordGrant(ctx context.Context, q Executor, in GrantInput) (*ConsentEvent, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	p, err := prepareGrant(in, time.Now)
	if err != nil {
		return nil, err
	}
	qs := dbgen.New(q)

	// 1) per-chain advisory lock：串行化该链的全部写入者（含首次授权竞态）.
	if err := qs.LockConsentChain(ctx, dbgen.LockConsentChainParams{
		Column1: formatUUID(p.sid),
		Column2: in.Purpose,
	}); err != nil {
		return nil, fmt.Errorf("compliance/pg advisory lock: %w", err)
	}
	// 2) 链顶版本 → 分配下一版本号（唯一索引兜底保证全局无重）.
	top, _, err := s.top(ctx, qs, p.sid, in.Purpose)
	if err != nil {
		return nil, err
	}
	next := 1
	if top != nil {
		next = top.Version + 1
	}
	// 3) 追加 grant 事件；recorded_by 为留痕的登记主体维度（0027 列）.
	return insertEvent(ctx, qs, rowArgs{
		sid: p.sid, rawSID: p.rawSID, scope: p.scope, actor: p.actor,
		at: p.at, version: next, eventType: EventGrant,
		vfrom: tsTZ(p.vfrom), vuntil: tsTZ(p.vuntil),
	})
}

// Revoke 实现 ConsentStore：有效性前提与追加在同一 advisory 锁内判定完成，
// 失败路径零副作用、不烧版本号.
func (s *PGStore) Revoke(ctx context.Context, q Executor, in RevokeInput) (*ConsentEvent, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	p, err := prepareRevoke(in, time.Now)
	if err != nil {
		return nil, err
	}
	qs := dbgen.New(q)

	if err := qs.LockConsentChain(ctx, dbgen.LockConsentChainParams{
		Column1: formatUUID(p.sid),
		Column2: in.Purpose,
	}); err != nil {
		return nil, fmt.Errorf("compliance/pg advisory lock: %w", err)
	}
	top, ok, err := s.top(ctx, qs, p.sid, in.Purpose)
	if err != nil {
		return nil, err
	}
	status := stateAt(p.rawSID, in.Purpose, top, p.at)
	if !ok || !status.IsValid {
		return nil, fmt.Errorf("%w: 当前状态 %q（学生 %s 的 %q 授权）",
			ErrNoActiveConsent, status.State, in.StudentAliasID, in.Purpose)
	}
	return insertEvent(ctx, qs, rowArgs{
		sid: p.sid, rawSID: p.rawSID, scope: p.scope, actor: p.actor,
		at: p.at, version: top.Version + 1, eventType: EventRevoke,
		vfrom: nullTZ(), vuntil: nullTZ(),
	})
}

// CheckConsent 实现 ConsentStore：now=nil 判当前态；永远取链顶唯一行
// （0027 唯一索引保证 ORDER BY version DESC LIMIT 1 的确定性）。无事件返回
// missing 态而非驱动级 ErrNoRows.
func (s *PGStore) CheckConsent(ctx context.Context, q Executor, studentAliasID, purpose string, now *time.Time) (*ConsentStatus, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	sid, err := validateChainKey(studentAliasID, purpose)
	if err != nil {
		return nil, err
	}
	top, _, err := s.top(ctx, dbgen.New(q), sid, purpose)
	if err != nil {
		return nil, err
	}
	ts := currentOr(now, time.Now)
	return stateAt(studentAliasID, purpose, top, ts), nil
}

// History 实现 ConsentStore：全量账升序只读投影（who/when/from/to 时间线还原面）.
func (s *PGStore) History(ctx context.Context, q Executor, studentAliasID, purpose string) ([]ConsentEvent, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	sid, err := validateChainKey(studentAliasID, purpose)
	if err != nil {
		return nil, err
	}
	rowsGen, err := dbgen.New(q).ListConsentHistory(ctx, dbgen.ListConsentHistoryParams{
		StudentAliasID: pgtype.UUID{Bytes: sid, Valid: true},
		Column2:        purpose,
	})
	if err != nil {
		return nil, fmt.Errorf("compliance/pg history: %w", err)
	}
	out := make([]ConsentEvent, 0, len(rowsGen))
	for i := range rowsGen {
		ev, err := eventFromGen(&rowsGen[i])
		if err != nil {
			return nil, err
		}
		out = append(out, *ev)
	}
	return out, nil
}

// top 读链顶内部视图；无事件时 (nil, false, nil)。供写路径与 CheckConsent 复用.
func (s *PGStore) top(ctx context.Context, qs *dbgen.Queries, sid [16]byte, purpose string) (*ConsentEvent, bool, error) {
	rowGen, err := qs.GetLatestConsentEvent(ctx, dbgen.GetLatestConsentEventParams{
		StudentAliasID: pgtype.UUID{Bytes: sid, Valid: true},
		Column2:        purpose,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("compliance/pg get latest: %w", err)
	}
	ev, err := eventFromGen(&rowGen)
	if err != nil {
		return nil, false, err
	}
	return ev, true, nil
}

// rowArgs 是两实现共用的追加载荷形状（grant/revoke 仅时刻列与版本来源不同）.
type rowArgs struct {
	sid       [16]byte
	rawSID    string
	scope     []byte
	actor     string
	at        time.Time
	version   int
	eventType EventType
	vfrom     pgtype.Timestamptz
	vuntil    pgtype.Timestamptz
}

// insertEvent 发出 INSERT 并映射回输出面事件（consent_id 由本层 crypto/rand 发号；
// created_at 显式传值使时间线与内存实现逐字段同构）.
func insertEvent(ctx context.Context, qs *dbgen.Queries, a rowArgs) (*ConsentEvent, error) {
	id, err := randomUUIDV4()
	if err != nil {
		return nil, err
	}
	err = qs.InsertConsentEvent(ctx, dbgen.InsertConsentEventParams{
		ConsentID:      pgtype.UUID{Bytes: id, Valid: true},
		StudentAliasID: pgtype.UUID{Bytes: a.sid, Valid: true},
		EventType:      string(a.eventType),
		Scope:          a.scope,
		ValidFrom:      a.vfrom,
		ValidUntil:     a.vuntil,
		Version:        int32(a.version),
		RecordedBy:     a.actor,
		CreatedAt:      tsTZ(a.at),
	})
	if err != nil {
		return nil, fmt.Errorf("compliance/pg insert %s: %w", a.eventType, mapUniqueViolation(err))
	}
	// 回显行补齐时刻列：grant 带窗口，revoke 保持 NULL——返回值与 DB 行逐字段一致.
	var vfrom, vuntil *time.Time
	if a.vfrom.Valid {
		t := a.vfrom.Time
		vfrom = &t
	}
	if a.vuntil.Valid {
		t := a.vuntil.Time
		vuntil = &t
	}
	return &ConsentEvent{
		ConsentID:      formatUUID(id),
		StudentAliasID: a.rawSID,
		EventType:      a.eventType,
		Scope:          mustDecodeScope(a.scope),
		ValidFrom:      vfrom,
		ValidUntil:     vuntil,
		Version:        a.version,
		RecordedBy:     a.actor,
		CreatedAt:      a.at,
	}, nil
}

// eventFromGen 把生成层行模型映射为领域事件（pgtype 可空性 → *time.Time 语义，
// scope 每次全新反序列化——输出面不共享存储引用）.
func eventFromGen(r *dbgen.ParentalConsent) (*ConsentEvent, error) {
	scope, err := decodeScope(r.Scope)
	if err != nil {
		return nil, err
	}
	var vfrom, vuntil *time.Time
	if r.ValidFrom.Valid {
		t := r.ValidFrom.Time
		vfrom = &t
	}
	if r.ValidUntil.Valid {
		t := r.ValidUntil.Time
		vuntil = &t
	}
	return &ConsentEvent{
		ConsentID:      formatUUID(r.ConsentID.Bytes),
		StudentAliasID: formatUUID(r.StudentAliasID.Bytes),
		EventType:      EventType(r.EventType),
		Scope:          scope,
		ValidFrom:      vfrom,
		ValidUntil:     vuntil,
		Version:        int(r.Version),
		RecordedBy:     r.RecordedBy,
		CreatedAt:      r.CreatedAt.Time,
	}, nil
}

// mapUniqueViolation 把唯一索引拒绝翻译为哨兵错误 ErrConsentConflict
// （errors.Is 可判）；非唯一冲突原样放行——异常不泄漏，但也绝不吞真故障.
func mapUniqueViolation(err error) error {
	var pe *pgconn.PgError
	if errors.As(err, &pe) && pe.Code == sqlStateUniqueViolation {
		// 双 %w：哨兵错误与原始驱动错误都留在 wrap 链里——调用方既能 errors.Is
		// 分支，也能回溯 SQLSTATE 证据（%v 会斩断链路，属吞错反模式）.
		return fmt.Errorf("%w: %w", ErrConsentConflict, err)
	}
	return err
}

// sqlStateUniqueViolation 是 PostgreSQL 唯一约束违反的 SQLSTATE。本地常量化而非
// 引 github.com/jackc/pgerrcode：避免为单个字符串比较把间接依赖升直接面.
const sqlStateUniqueViolation = "23505"

// tsTZ 把领域时刻转为 pgtype 的 timestamptz 扫描/传参形状.
func tsTZ(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t, Valid: true}
}

// nullTZ SQL NULL 时刻（revoke 行的 valid_from/valid_until 列）.
func nullTZ() pgtype.Timestamptz { return pgtype.Timestamptz{} }
