package session

// T-W5-004 会话题序写入/读取面（Go 重锚定；冻结语义基准
// src/core/session/service.py start_session 的题序固化）。
//
// 核心语义：会话题序一旦生成不可变（A4 追溯链路锚点：历史作答只有经题序才能
// 与题目对应）。不可变性不靠应用层 if，而是三层结构性保证：
//  1. DB（0030_session_topic_order_immutable）：锚列（session_id/student_alias_id/
//     scene/item_sequence）UPDATE 拒绝 + 整行 DELETE 拒绝（复用 0005
//     raise_append_only_error()）+ 题序行 (session_id, seq) 唯一与结构校验触发器；
//  2. 查询面（db/queries/practice_session.sql，SQL-2）：只有 INSERT/SELECT——
//     题序改写/删除语句无查询面可写（TestTopicOrderNoRewriteSurface 静态守卫）；
//  3. 本面：写入只接受显式事务执行面（D11 fail-closed），幂等语义——同 session
//     重复生成完全相同题序 → 幂等成功返回存量；不同题序 → ErrTopicOrderConflict
//     （明确错误，绝不静默改写）。
//
// 冻结语义保留（start_session）：题序来源二选一中的「顺序解析」归上游——静态卷
// 按 paper_item.item_number 的解析（W2 追溯表读模型）与实例池调用方给定顺序，由
// 调用方把最终顺序传入本面；本面固化该顺序并落 placement_token 可空追溯锚（冻结
// pool 路径 placement_tokens=[None]*n 同形）。会话只出 published 题目的门纪律
// （X2）由上游发布面/组装面判定后传入，与本面职责正交（同冻结：UnpublishedItemError
// 在 start_session 入口而非题序固化结构）。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 会话场景二值域（与 0011 的 ck_practice_session_scene_domain CHECK 同值域；
// measurement 首年不做，冻结 VALID_SCENES 同集）.
const (
	ScenePractice  = "practice"
	SceneDiagnosis = "diagnosis"
)

// 学段三值域（与 ck_practice_session_gradeband_domain CHECK 同值域）.
const (
	GradebandLow  = "L"
	GradebandMid  = "M"
	GradebandHigh = "H"
)

// GradebandTimeLimitSec 冻结时长保护阈值（架构 v2 §4.8：低段 ≤15 分钟、
// 中/高段 ≤60 分钟；建会话时定型落列，策略调整不回溯进行中的会话）.
var GradebandTimeLimitSec = map[string]int32{
	GradebandLow:  15 * 60,
	GradebandMid:  60 * 60,
	GradebandHigh: 60 * 60,
}

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoTransaction 表示写调用没有显式事务执行面。D11 fail-closed：题序固化
	// 与将来的作答推进在同一事务里同进同退，无事务面即无同进同退可言——宁拒不放.
	ErrNoTransaction = errors.New("session: 无显式事务执行面（D11 fail-closed：题序固化只接受外层已 begin 的事务）")

	// ErrInvalidSessionStart 表示会话启动输入非法（student_alias_id/scene/
	// gradeband/session_id）。对应冻结实现 start_session 的 ValueError 族.
	ErrInvalidSessionStart = errors.New("session: 会话启动输入非法（student_alias_id/scene/gradeband/session_id）")

	// ErrInvalidTopicOrder 表示题序结构非法：非空、条目 item_version_id 非空、
	// seq ≥ 1 且 (session_id, seq) 唯一。在出 Go 进程前拦截，不给 0030 触发器
	// 晚到机会（DB 侧同规则触发器为非 Go 直写的最后防线，纵深防御）.
	ErrInvalidTopicOrder = errors.New("session: 题序非法（非空；条目 item_version_id 非空、seq ≥ 1 且 (session_id, seq) 唯一）")

	// ErrSessionNotFound 表示会话不存在（读取面）.
	ErrSessionNotFound = errors.New("session: 会话不存在")

	// ErrTopicOrderConflict 表示同 session 已固化另一条题序（幂等语义的「明确
	// 错误」半边）：题序一经生成不可变，重复生成只接受语义完全相同的重放.
	ErrTopicOrderConflict = errors.New("session: 会话题序冲突（题序一经生成不可变，同 session 不接受第二条款序）")
)

// Executor 是题序读写所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX 同构
// （与本仓 core/compliance、core/events 的同名接口同形）。
//
// 为什么本地重声明而不跨包复用：领域端口按需各自声明最小依赖面，六边形核心域
// 之间不为一个三方法接口建立编译耦合；两者方法集一致，pgx.Tx 与连接池事务面天然
// 同时满足。全部语句文本只住在 db/queries/practice_session.sql（SQL-2：不在 Go
// 拼 SQL），经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方；因此本包源码
// 不可能发出题序改写语句——UPDATE/DELETE 无查询面可写（0030 触发器物理兜底）.
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

// 编译期锚定三：两种实现都必须兑现 TopicOrderStore 契约.
var (
	_ TopicOrderStore = (*MemoryStore)(nil)
	_ TopicOrderStore = (*PGStore)(nil)
)

// TopicEntry 是题序一行的语义三元组（冻结 sequence dict
// {item_version_id, placement_token, item_number} 的 Go 形）.
type TopicEntry struct {
	// Seq 题序位（冻结 item_number，1 起；(session_id, seq) 唯一由 0030 触发器
	// 为非 Go 直写兜底、本面在出进程前拦截）.
	Seq int
	// ItemVersionID 已发布 item_version id（A4 追溯链的题目端锚点）.
	ItemVersionID string
	// PlacementToken 静态卷放位令牌（A4 source_ref 追溯锚；实例池路径 nil=冻结
	// JSON null 同形）.
	PlacementToken *string
}

// TopicOrder 是一个会话的题序快照（只读投影）.
type TopicOrder struct {
	// SessionID 会话 id（标准连字符形 UUID）.
	SessionID string
	// Entries 按 Seq 升序的题序（每次返回深拷贝，内部账不可经返回值被改写——
	// -race 干净 + 「返回即不可变」的结构前提）.
	Entries []TopicEntry
}

// StartInput 是一次会话题序固化的请求（字段口径对齐冻结 start_session 参集的
// 题序相关子集：student_alias_id/scene/gradeband/paper_id/item 序列/retest_wrong/
// 起始时刻；来源顺序解析归上游，见包注释）.
type StartInput struct {
	// SessionID 会话 id；空则由 crypto/rand 生成 UUIDv4（冻结 uuid.uuid4 同义）.
	SessionID string
	// StudentAliasID 匿名学生 id（D7：主库只有别名 id）.
	StudentAliasID string
	// Scene 会话场景；空回落 practice（冻结 start_session 默认值同义）.
	Scene string
	// Gradeband 学段（L/M/H），决定时长保护阈值定型值.
	Gradeband string
	// PaperID 静态卷 id（可空——实例池会话 NULL，冻结同形；FK 由 0011 兜底）.
	PaperID *string
	// Entries 题序（调用方给定顺序；幂等判读按 (Seq, ItemVersionID,
	// PlacementToken) 语义相等，与 JSON 键序/输入顺序无关）.
	Entries []TopicEntry
	// RetestWrong 主序列走完后是否对错题回测一轮（冻结 start_session 同名列）.
	RetestWrong bool
	// StartedAt 开始时刻（= started_at/last_resume_at/last_activity_at，冻结 ts
	// 三列同源）；零值取当前时刻.
	StartedAt time.Time
}

// TopicOrderStore 是会话题序账的语义契约.
//
// 并发契约：同一 session 的并发 Create 要么恰好固化一份题序、其余调用方拿到
// 幂等成功或 ErrTopicOrderConflict（内存=互斥锁；PG=PK 23505 临界冲撞后读存量
// 判读），不存在半份题序或双条款序可被观察到；Read 返回的快照与内部账隔离.
type TopicOrderStore interface {
	// Create 固化会话题序（创建会话行，冻结 start_session 的 INSERT 形态）。
	// 幂等语义：同 session 重复生成完全相同题序 → 幂等成功返回存量；
	// 不同题序 → ErrTopicOrderConflict。q 允许 nil——内存实现不需要执行面；
	// PGStore 收到 nil 以 ErrNoTransaction fail-closed（consent 门同惯例）.
	Create(ctx context.Context, q Executor, in StartInput) (*TopicOrder, error)
	// Read 按 Seq 升序稳定读出题序快照；会话不存在 → ErrSessionNotFound.
	Read(ctx context.Context, q Executor, sessionID string) (*TopicOrder, error)
}

// preparedStart 是校验定影后的题序固化载荷（内存/PG 两实现的统一前置管线：
// 对同一非法输入必然给出同一条哨兵错误，判据单一来源，不存在实现间漂移面）.
type preparedStart struct {
	sessionID string                            // 归一（标准连字符形）后的会话 id
	params    dbgen.InsertPracticeSessionParams // PG 形参（内存实现只取 entries/timeLimit）
	entries   []TopicEntry                      // canonical：Seq 升序、深拷贝
}

// prepareStart 执行身份/场景/题序三段前置校验并定型创建形态（冻结 start_session
// 的校验序：参数互斥与 published 门归上游，此处锁 DB 约束同源的不可变量）.
func prepareStart(in StartInput, now func() time.Time) (*preparedStart, error) {
	if in.Scene == "" {
		in.Scene = ScenePractice
	}
	if in.Scene != ScenePractice && in.Scene != SceneDiagnosis {
		return nil, fmt.Errorf("%w: scene 必须 ∈ {%s, %s}，实际 %q", ErrInvalidSessionStart, ScenePractice, SceneDiagnosis, in.Scene)
	}
	limit, ok := GradebandTimeLimitSec[in.Gradeband]
	if !ok {
		return nil, fmt.Errorf("%w: gradeband 必须 ∈ {%s, %s, %s}，实际 %q", ErrInvalidSessionStart, GradebandLow, GradebandMid, GradebandHigh, in.Gradeband)
	}
	var alias pgtype.UUID
	if err := alias.Scan(in.StudentAliasID); err != nil || !alias.Valid {
		return nil, fmt.Errorf("%w: student_alias_id %q 不是合法 UUID", ErrInvalidSessionStart, in.StudentAliasID)
	}
	if len(in.Entries) == 0 {
		return nil, fmt.Errorf("%w: 题序为空（卷内无题或实例池为空）", ErrInvalidTopicOrder)
	}
	entries := cloneEntries(in.Entries)
	seen := make(map[int]struct{}, len(entries))
	for i, e := range entries {
		if e.ItemVersionID == "" {
			return nil, fmt.Errorf("%w: entries[%d].item_version_id 为空", ErrInvalidTopicOrder, i)
		}
		if e.Seq < 1 {
			return nil, fmt.Errorf("%w: entries[%d].seq = %d，必须 ≥ 1", ErrInvalidTopicOrder, i, e.Seq)
		}
		if _, dup := seen[e.Seq]; dup {
			return nil, fmt.Errorf("%w: seq=%d 重复（(session_id, seq) 必须唯一）", ErrInvalidTopicOrder, e.Seq)
		}
		seen[e.Seq] = struct{}{}
	}
	sortEntries(entries)

	startedAt := in.StartedAt
	if startedAt.IsZero() {
		startedAt = now()
	}

	var sidBytes [16]byte
	sessionID := in.SessionID
	if sessionID == "" {
		b, err := randomUUIDV4()
		if err != nil {
			return nil, err
		}
		sidBytes = b
		sessionID = formatUUID(b)
	} else {
		var sid pgtype.UUID
		if err := sid.Scan(sessionID); err != nil || !sid.Valid {
			return nil, fmt.Errorf("%w: session_id %q 不是合法 UUID", ErrInvalidSessionStart, sessionID)
		}
		sidBytes = sid.Bytes
		sessionID = formatUUID(sid.Bytes) // 归一大小写/形态，幂等判读键唯一
	}

	payload, err := encodeEntries(entries)
	if err != nil {
		return nil, err
	}

	// 冻结 start_session 的 INSERT 形态：status='active'、进度/计数清零、
	// wrong_marks 空账、completed_at NULL、时长阈值建会话时定型.
	params := dbgen.InsertPracticeSessionParams{
		SessionID:      pgtype.UUID{Bytes: sidBytes, Valid: true},
		StudentAliasID: alias,
		Scene:          in.Scene,
		Gradeband:      in.Gradeband,
		Status:         "active",
		PaperID:        pgtype.Text{String: deref(in.PaperID), Valid: in.PaperID != nil},
		ItemSequence:   payload,
		CurrentIndex:   0,
		RetestWrong:    in.RetestWrong,
		WrongMarks:     []byte("[]"),
		TimeLimitSec:   limit,
		StartedAt:      tsTZ(startedAt),
		LastResumeAt:   tsTZ(startedAt),
		LastActivityAt: tsTZ(startedAt),
	}
	return &preparedStart{sessionID: sessionID, params: params, entries: entries}, nil
}

// frozenSequenceEntry 是 item_sequence JSONB 的条目形状（键名与冻结 sequence
// dict 逐字一致：item_version_id / placement_token / item_number）.
type frozenSequenceEntry struct {
	ItemVersionID  string  `json:"item_version_id"`
	PlacementToken *string `json:"placement_token"`
	ItemNumber     int     `json:"item_number"`
}

// encodeEntries 序列化题序账面（placement_token 恒在键——nil 出 JSON null，与
// 冻结 pool 路径 placement_tokens=[None]*n 的落库形态同构）.
func encodeEntries(entries []TopicEntry) ([]byte, error) {
	out := make([]frozenSequenceEntry, len(entries))
	for i, e := range entries {
		out[i] = frozenSequenceEntry{ItemVersionID: e.ItemVersionID, PlacementToken: e.PlacementToken, ItemNumber: e.Seq}
	}
	b, err := json.Marshal(out)
	if err != nil {
		return nil, fmt.Errorf("%w: 题序序列化失败: %v", ErrInvalidTopicOrder, err)
	}
	return b, nil
}

// decodeEntries 从账面 JSON 还原题序并按 Seq 升序规整（每次调用产出新深拷贝，
// 调用方拿到的切片不可能回写内部账）；反序列化失败属账损防御路径，显式报错.
func decodeEntries(raw []byte) ([]TopicEntry, error) {
	var in []frozenSequenceEntry
	if err := json.Unmarshal(raw, &in); err != nil {
		return nil, fmt.Errorf("session: 题序账面反序列化失败（账损防御路径）: %w", err)
	}
	out := make([]TopicEntry, len(in))
	for i, e := range in {
		out[i] = TopicEntry{Seq: e.ItemNumber, ItemVersionID: e.ItemVersionID, PlacementToken: e.PlacementToken}
	}
	sortEntries(out)
	return out, nil
}

// sortEntries 按 Seq 升序原地规整（稳定排序：读取面「按 seq 升序稳定读」的锚）.
func sortEntries(entries []TopicEntry) {
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Seq < entries[j].Seq })
}

// cloneEntries 深拷贝题序（含 placement_token 指针目标，杜绝别名回写）.
func cloneEntries(entries []TopicEntry) []TopicEntry {
	out := make([]TopicEntry, len(entries))
	for i, e := range entries {
		out[i] = TopicEntry{Seq: e.Seq, ItemVersionID: e.ItemVersionID}
		if e.PlacementToken != nil {
			tok := *e.PlacementToken
			out[i].PlacementToken = &tok
		}
	}
	return out
}

// equalEntries 语义相等：规整（Seq 升序）后逐位比对三元组（幂等判读判据；
// 与输入顺序、JSON 键序无关）.
func equalEntries(a, b []TopicEntry) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Seq != b[i].Seq || a[i].ItemVersionID != b[i].ItemVersionID {
			return false
		}
		if (a[i].PlacementToken == nil) != (b[i].PlacementToken == nil) {
			return false
		}
		if a[i].PlacementToken != nil && *a[i].PlacementToken != *b[i].PlacementToken {
			return false
		}
	}
	return true
}

// isUniqueViolation 判定驱动错误是否为 PostgreSQL 唯一约束违反（SQLSTATE 23505）.
func isUniqueViolation(err error) bool {
	var pe *pgconn.PgError
	return errors.As(err, &pe) && pe.Code == sqlStateUniqueViolation
}

// sqlStateUniqueViolation 是 PostgreSQL 唯一约束违反的 SQLSTATE。本地常量化而非
// 引 github.com/jackc/pgerrcode：避免为单个字符串比较把间接依赖升直接面.
const sqlStateUniqueViolation = "23505"

// tsTZ 把领域时刻转为 pgtype 的 timestamptz 传参形状（compliance 同手法）.
func tsTZ(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t, Valid: true}
}

// normalizeSessionID 把任意合法 UUID 书写形态归一为标准连字符形（幂等判读与
// 读取键的唯一来源）；非法输入报错，由调用方按「会话不存在」语义落哨兵——
// 内存实现按键查找，PG 实现按 UUID 解析查询，两实现共用本函数保证无漂移面.
func normalizeSessionID(sessionID string) (string, error) {
	var sid pgtype.UUID
	if err := sid.Scan(sessionID); err != nil || !sid.Valid {
		return "", fmt.Errorf("session: session_id %q 不是合法 UUID", sessionID)
	}
	return formatUUID(sid.Bytes), nil
}

// deref 取可空字符串的值（nil → 空串；Valid 由调用方按指针判）.
func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// formatUUID 把 16 字节渲染为标准连字符形（会话 id 出账形态；compliance 同手法
// 的本地副本——六边形域间不为一个渲染函数建立耦合）.
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

// randomUUIDV4 由 crypto/rand 直接构造 UUIDv4 字节（冻结 uuid.uuid4 同义）：
// 标准库即可满足，不为一个发号函数引第三方依赖（熵源不可用时报错而非发出可
// 重复 ID）.
func randomUUIDV4() ([16]byte, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return b, fmt.Errorf("session: 熵源不可用无法生成 session_id: %w", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return b, nil
}
