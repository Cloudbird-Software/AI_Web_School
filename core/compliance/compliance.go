package compliance

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// EventType 授权事件二值域（与 0015 的 ck_parental_consent_event_type_domain
// CHECK 同值域）.
type EventType string

const (
	EventGrant  EventType = "grant"
	EventRevoke EventType = "revoke"
)

// State 是 check_consent 口径的授权状态四值域（Python 冻结实现同值集）.
type State string

const (
	StateGranted State = "granted" // 最新事件为 grant 且未过期
	StateRevoked State = "revoked" // 最新事件为 revoke
	StateExpired State = "expired" // 最新事件为 grant 但 valid_until <= now
	StateMissing State = "missing" // 从未授权该 purpose
)

// SystemActor 是登记主体不可考时的留痕回落值（0027 列默认值）.
const SystemActor = "system"

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoTransaction 表示写调用没有显式事务执行面。D11 fail-closed：
	// per-chain advisory xact lock 的生命周期绑在事务上，无事务面即无临界区，
	// 并发正确性随之瓦解——宁拒不放.
	ErrNoTransaction = errors.New("compliance: 无显式事务执行面（D11 fail-closed：授权写入只接受外层已 begin 的事务）")

	// ErrInvalidScope 表示 scope 非法（purpose 为空 / Extra 抢占 purpose 键 /
	// Extra 含不可 JSON 序列化的维度）。对应 Python 冻结实现 ConsentScopeError.
	ErrInvalidScope = errors.New("compliance: 授权 scope 非法（scope 必含非空 purpose，且扩展维度不得覆盖 purpose）")

	// ErrInvalidWindow 表示 grant 有效窗口非法（valid_until 缺省或不在
	// valid_from 之后）.
	ErrInvalidWindow = errors.New("compliance: 授权窗口非法（valid_until 必须晚于 valid_from）")

	// ErrInvalidStudentAlias 表示 student_alias_id 不是合法 UUID（主库只有
	// 别名 id，格式违例在出 Go 进程前拦截，不给 PG 错误晚到机会）.
	ErrInvalidStudentAlias = errors.New("compliance: student_alias_id 不是合法 UUID")

	// ErrNoActiveConsent 表示无有效授权可撤回（从未授权 / 已撤回 / 已过期）。
	// 对应 Python 冻结实现 NoActiveConsentError；失败在临界区内判定，零副作用、
	// 不烧版本号.
	ErrNoActiveConsent = errors.New("compliance: 无有效的授权可撤回（missing/revoked/expired 均拒绝，避免审计噪声）")

	// ErrConsentConflict 表示偏唯一索引 uq_parental_consent_version_per_purpose
	// 拒绝了本次插入（SQLSTATE 23505）。advisory lock 正常工作时不应出现；出现即
	// 视为数据库层防线的明确失败信号——返回本错误而非让驱动异常穿透.
	ErrConsentConflict = errors.New("compliance: 授权链版本唯一性冲突（23505），请重试")
)

// ────────────────────────────────────────────────────────────────────
// 输入预检管线：grant / revoke 入参的统一校验序——内存与 PG 两实现对同一非法
// 输入必然给出同一条哨兵错误，判据单一来源，不存在实现间漂移面.
// ────────────────────────────────────────────────────────────────────

// preparedGrant 是校验定影后的 grant 写入载荷.
type preparedGrant struct {
	sid    [16]byte // student_alias_id 解析字节（PG 形参直用）
	rawSID string   // 调用方原始书写（回显输出面）
	scope  []byte   // {"purpose":..., ...extra} JSONB 载荷
	actor  string   // 归一后登记主体
	at     time.Time
	vfrom  time.Time
	vuntil time.Time
}

// prepareGrant 执行链身份/scope/窗口三段前置校验；now 为实现的时钟回落
// （At 零值时取用）.
func prepareGrant(in GrantInput, now func() time.Time) (*preparedGrant, error) {
	sid, err := validateChainKey(in.StudentAliasID, in.Purpose)
	if err != nil {
		return nil, err
	}
	scope, err := scopeJSON(in.Purpose, in.Extra)
	if err != nil {
		return nil, err
	}
	at := resolveAt(in.At, now)
	vfrom := in.ValidFrom
	if vfrom.IsZero() {
		vfrom = at
	}
	if in.ValidUntil.IsZero() || !in.ValidUntil.After(vfrom) {
		return nil, fmt.Errorf("%w: valid_until(%v) 须晚于 valid_from(%v)", ErrInvalidWindow, in.ValidUntil, vfrom)
	}
	return &preparedGrant{
		sid: sid, rawSID: in.StudentAliasID, scope: scope,
		actor: actorOf(in.RecordedBy), at: at,
		vfrom: vfrom, vuntil: in.ValidUntil,
	}, nil
}

// preparedRevoke 是校验定影后的 revoke 写入载荷.
type preparedRevoke struct {
	sid    [16]byte
	rawSID string
	scope  []byte
	actor  string
	at     time.Time
}

// prepareRevoke 执行链身份/scope 前置校验；有效性前提由各实现在临界区内判定
// （读链顶→stateAt），前置失败零副作用、不烧版本号.
func prepareRevoke(in RevokeInput, now func() time.Time) (*preparedRevoke, error) {
	sid, err := validateChainKey(in.StudentAliasID, in.Purpose)
	if err != nil {
		return nil, err
	}
	scope, err := scopeJSON(in.Purpose, in.Extra)
	if err != nil {
		return nil, err
	}
	return &preparedRevoke{
		sid: sid, rawSID: in.StudentAliasID, scope: scope,
		actor: actorOf(in.RecordedBy), at: resolveAt(in.At, now),
	}, nil
}

// Executor 是授权账读写所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX
// 同构（与本仓 core/estimator、core/events 的同名接口同形）。
//
// 为什么本地重声明而不跨包复用：领域端口按需各自声明最小依赖面，六边形核心域
// 之间不为一个三方法接口建立编译耦合；两者方法集一致，pgx.Tx 与连接池事务面天然
// 同时满足。全部语句文本只住在 db/queries/consent.sql（SQL-2：不在 Go 拼 SQL），
// 经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方；因此本包源码不可能发出
// UPDATE/DELETE——append-only 无查询面可写.
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

// 编译期锚定三：两种实现都必须兑现 ConsentStore 的并发契约.
var (
	_ ConsentStore = (*MemoryStore)(nil)
	_ ConsentStore = (*PGStore)(nil)
)

// GrantInput 是一次新授权（grant 事件）的登记请求（字段口径对齐 Python 冻结实现
// record_consent 参集 + 新增 RecordedBy 以满足留痕「谁」）.
type GrantInput struct {
	StudentAliasID string
	// Purpose 是 scope 的主键语义键（如 "practice"/"diagnosis"/"measurement"），
	// 即授权链的第二级身份；空串拒绝.
	Purpose string
	// Extra 是 scope 的扩展维度（subject/time_period 等），可为 nil；
	// 与 Purpose 合并序列化为 scope JSONB，禁止携带 "purpose" 键抢占.
	Extra map[string]any
	// ValidFrom 生效时刻；零值回落 At（或当前时刻）.
	ValidFrom time.Time
	// ValidUntil 截止时刻；必须晚于有效起点，零值拒绝.
	ValidUntil time.Time
	// RecordedBy 登记「谁」做的授权；空值回落 SystemActor.
	RecordedBy string
	// At 事件登记时刻（created_at 与「当前时刻」判定的统一基准）；零值取当前时刻.
	At time.Time
}

// RevokeInput 是一次撤回（revoke 事件）的登记请求。Extra 允许携带撤回侧扩展
// 维度（Python 冻结实现 revoke_consent 同样收 scope dict）；purpose 键语义同上.
type RevokeInput struct {
	StudentAliasID string
	Purpose        string
	Extra          map[string]any
	RecordedBy     string
	At             time.Time
}

// ConsentEvent 对应 parental_consent 一行（0015 全列 + 0027 recorded_by），
// 输出面字段均为独立拷贝——Scope 每次从存储 JSON 反序列化而来，调用方拿到的
// map 不可能回写内部账（-race 干净 + append-only 只读投影的结构前提）.
type ConsentEvent struct {
	ConsentID      string
	StudentAliasID string
	EventType      EventType
	Scope          map[string]any
	ValidFrom      *time.Time
	ValidUntil     *time.Time
	Version        int
	RecordedBy     string
	CreatedAt      time.Time
}

// ConsentStatus 是 check_consent 口径的状态判定结果；IsValid 为便捷谓词
// （StateGranted 即 true，语义与 Python is_valid property 一致）.
type ConsentStatus struct {
	StudentAliasID string
	Purpose        string
	State          State
	IsValid        bool
	// Version 是链顶事件版本；无任何事件时为 0.
	Version    int
	ValidFrom  *time.Time
	ValidUntil *time.Time
}

// ConsentStore 是家长授权账的语义契约.
//
// 并发契约（本卡核心交付）：对同一授权链 (student_alias_id, purpose) 的全部写入
// 构成单一原子临界区，并发调用互斥串行化（内存=互斥锁；PG=per-chain advisory
// xact lock + 唯一索引兜底），每次调用要么完整追加一条新版本事件，要么整体未发生
// 并返回明确 error（前置校验失败不烧版本号）。CheckConsent 永远读链顶唯一行，
// 并发下不存在「双活跃版本」或半写状态可被观察到.
type ConsentStore interface {
	// RecordGrant 追加一条 grant 事件（新版本），旧版本隐式失效.
	RecordGrant(ctx context.Context, q Executor, in GrantInput) (*ConsentEvent, error)
	// Revoke 追加一条 revoke 事件（新版本）；当前无有效授权时报 ErrNoActiveConsent.
	Revoke(ctx context.Context, q Executor, in RevokeInput) (*ConsentEvent, error)
	// CheckConsent 以 ts 为判定时刻返回链顶状态；now=nil 表示当前时刻.
	CheckConsent(ctx context.Context, q Executor, studentAliasID, purpose string, now *time.Time) (*ConsentStatus, error)
	// History 按 version 升序返回链的全量事件账（append-only 账的只读投影）；
	// 相邻行的 version n → n+1 即「从哪版到哪版」，who/when 取 RecordedBy/CreatedAt.
	History(ctx context.Context, q Executor, studentAliasID, purpose string) ([]ConsentEvent, error)
}

// resolveAt 把可选的登记时刻归一为确定时间基准（零值回落 now()）.
func resolveAt(at time.Time, now func() time.Time) time.Time {
	if at.IsZero() {
		return now()
	}
	return at
}

// actorOf 归一登记主体（空回落 system，与 0027 列默认一致）.
func actorOf(s string) string {
	if s == "" {
		return SystemActor
	}
	return s
}

// validateChainKey 校验授权链身份：student_alias_id 必须是合法 UUID，purpose
// 非空。返回解析出的 uuid 字节（供 PG 形参复用，同一校验不再走第二遍）.
func validateChainKey(studentAliasID, purpose string) ([16]byte, error) {
	if purpose == "" {
		return [16]byte{}, fmt.Errorf("%w: purpose 不能为空", ErrInvalidScope)
	}
	var u pgtype.UUID
	if err := u.Scan(studentAliasID); err != nil || !u.Valid {
		return [16]byte{}, fmt.Errorf("%w: %q", ErrInvalidStudentAlias, studentAliasID)
	}
	return u.Bytes, nil
}

// checkExtra 校验并规整扩展维度：不允许出现 "purpose" 键（它由 Purpose 字段独占
// 承载，双源定义会让链身份失明），且必须可 JSON 序列化.
func checkExtra(extra map[string]any) error {
	if _, ok := extra["purpose"]; ok {
		return fmt.Errorf("%w: Extra 不得包含 \"purpose\" 键（purpose 由输入字段独占承载）", ErrInvalidScope)
	}
	if extra == nil {
		return nil
	}
	if _, err := json.Marshal(extra); err != nil {
		return fmt.Errorf("%w: 扩展维度不可 JSON 序列化: %v", ErrInvalidScope, err)
	}
	return nil
}

// scopeJSON 序列化 scope 账面：{"purpose": p, ...extra}（jsonb 列载荷）.
func scopeJSON(purpose string, extra map[string]any) ([]byte, error) {
	if err := checkExtra(extra); err != nil {
		return nil, err
	}
	m := make(map[string]any, len(extra)+1)
	for k, v := range extra {
		m[k] = v
	}
	m["purpose"] = purpose
	b, err := json.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("%w: scope 序列化失败: %v", ErrInvalidScope, err)
	}
	return b, nil
}

// decodeScope 从账面 JSON 反序列化 scope 拷贝（每次调用产出新 map，内部账不可经
// 返回值被改写）.
func decodeScope(raw []byte) (map[string]any, error) {
	out := make(map[string]any)
	if len(raw) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("compliance: scope 账面反序列化失败（账损防御路径）: %w", err)
	}
	return out, nil
}

// mustDecodeScope 反序列化本包自产的 scope 载荷（scopeJSON 的输出路径）：自产即
// 合法，失败只可能是内部管线被破坏——作为 panic 显式暴露而非伪装成账损错误.
func mustDecodeScope(raw []byte) map[string]any {
	m, err := decodeScope(raw)
	if err != nil {
		panic(fmt.Sprintf("compliance: 自产 scope 反序列化失败（内部管线破坏）: %v", err))
	}
	return m
}

// stateAt 是状态判定的纯函数核（Python 冻结实现 check_consent 的同义移植）：
// - 无链顶事件 → missing（Version=0）
// - 链顶为 revoke → revoked
// - 链顶为 grant 且 ts >= valid_until → expired（边界相等多算过期，>= 语义）
// - 其余 → granted（grant 行按 CHECK 约束必有 valid_until）
func stateAt(studentAliasID, purpose string, top *ConsentEvent, ts time.Time) *ConsentStatus {
	if top == nil {
		return &ConsentStatus{StudentAliasID: studentAliasID, Purpose: purpose, State: StateMissing}
	}
	if top.EventType == EventRevoke {
		return &ConsentStatus{
			StudentAliasID: studentAliasID,
			Purpose:        purpose,
			State:          StateRevoked,
			Version:        top.Version,
		}
	}
	// grant 行受 ck_parental_consent_event_type_time_consistency 约束必有
	// valid_until；nil 属不可能态，防御性按已过期处理比 panic 更符合 fail-closed.
	expired := top.ValidUntil == nil || !ts.Before(*top.ValidUntil)
	st := StateExpired
	if !expired {
		st = StateGranted
	}
	return &ConsentStatus{
		StudentAliasID: studentAliasID,
		Purpose:        purpose,
		State:          st,
		IsValid:        st == StateGranted,
		Version:        top.Version,
		ValidFrom:      cloneTime(top.ValidFrom),
		ValidUntil:     cloneTime(top.ValidUntil),
	}
}

// cloneTime 浅拷贝可空时刻（供输出面交出独立指针）.
func cloneTime(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	c := *t
	return &c
}

// formatUUID 把 16 字节渲染为标准连字符形（consent_id 出账形态）.
func formatUUID(b [16]byte) string {
	var buf [36]byte
	hex.Encode(buf[0:8], b[0:4])
	buf[8] = '-'
	hex.Encode(buf[9:13], b[4:6])
	buf[13] = '-'
	hex.Encode(buf[14:18], b[6:8])
	buf[18] = '-'
	hex.Encode(buf[19:23], b[8:10])
	buf[23] = '-'
	hex.Encode(buf[24:36], b[10:16])
	return string(buf[:])
}

// randomUUIDV4 由 crypto/rand 直接构造 UUIDv4 字节：标准库即可满足，不为一个
// 发号函数引第三方依赖（熵源不可用时报错而非发出可重复 ID）.
func randomUUIDV4() ([16]byte, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return b, fmt.Errorf("compliance: 熵源不可用无法生成 consent_id: %w", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return b, nil
}
