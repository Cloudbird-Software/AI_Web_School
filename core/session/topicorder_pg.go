package session

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// PGStore 是会话题序账的 PG 实现（语句面全在 db/queries/practice_session.sql，
// 经 sqlc 生成为 dbgen 类型安全方法；本文件零 SQL 字符串——SQL-2）。
//
// 结构性不可变的最后防线在 DB（0030）：锚列 UPDATE / 整行 DELETE 被触发器拒绝，
// 本实现的语句面只有 INSERT/SELECT，三层互为纵深（见 topicorder.go 包注释）.
type PGStore struct{}

// NewPGStore 构造 PG 实现（无状态：执行面按调用传入，事务归最外层调用方，D11）.
func NewPGStore() *PGStore { return &PGStore{} }

// Create 实现 TopicOrderStore：显式事务面内 INSERT 固化题序（冻结 start_session
// 的创建形态在 prepareStart 一次定型）。
//
// 幂等语义：session_id 撞 PK（23505）= 同 session 已有题序——读存量语义比对：
// 完全相同 → 幂等成功返回存量；不同 → ErrTopicOrderConflict。撞 PK 后读存量
// 不需要额外加锁：PG 的 INSERT 在 PK 冲突上会等对方事务终结，收到 23505 时
// 对方行必已提交，同事务内可见（READ COMMITTED 读已提交快照）.
func (s *PGStore) Create(ctx context.Context, q Executor, in StartInput) (*TopicOrder, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	prepared, err := prepareStart(in, time.Now)
	if err != nil {
		return nil, err
	}
	qs := dbgen.New(q)
	if err := qs.InsertPracticeSession(ctx, prepared.params); err != nil {
		if !isUniqueViolation(err) {
			// 驱动/约束错误原样 wrap 放行（0030 触发器拒绝的 SQLSTATE 证据不吞：
			// 非 Go 直写撞 uq_session_topic_order_seq 亦在此如实上抛）.
			return nil, fmt.Errorf("session: insert practice_session: %w", err)
		}
		return s.resolveIdempotent(ctx, qs, prepared)
	}
	return &TopicOrder{SessionID: prepared.sessionID, Entries: cloneEntries(prepared.entries)}, nil
}

// resolveIdempotent 在 PK 23505 后读存量题序做语义比对（幂等成功或明确冲突）.
func (s *PGStore) resolveIdempotent(ctx context.Context, qs *dbgen.Queries, prepared *preparedStart) (*TopicOrder, error) {
	raw, err := qs.GetPracticeSessionItemSequence(ctx, prepared.params.SessionID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// 冲突对象读不到：撞的唯一性不是会话行（如非 Go 直写撞
			// uq_session_topic_order_seq 且行未落）——按题序冲突处理，不给
			// 「半固化」假象.
			return nil, fmt.Errorf("%w: session_id=%s", ErrTopicOrderConflict, prepared.sessionID)
		}
		return nil, fmt.Errorf("session: read stored item_sequence: %w", err)
	}
	stored, err := decodeEntries(raw)
	if err != nil {
		return nil, err
	}
	if !equalEntries(stored, prepared.entries) {
		return nil, fmt.Errorf("%w: session_id=%s", ErrTopicOrderConflict, prepared.sessionID)
	}
	return &TopicOrder{SessionID: prepared.sessionID, Entries: stored}, nil
}

// Read 实现 TopicOrderStore：存量按 Seq 升序稳定读出（decodeEntries 规整序）.
func (s *PGStore) Read(ctx context.Context, q Executor, sessionID string) (*TopicOrder, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	var sid pgtype.UUID
	if err := sid.Scan(sessionID); err != nil || !sid.Valid {
		// 不可能存在的 id → 会话不存在语义（与内存实现按键未命中同一条哨兵，
		// 实现间无漂移面）.
		return nil, fmt.Errorf("%w: session_id=%s", ErrSessionNotFound, sessionID)
	}
	raw, err := dbgen.New(q).GetPracticeSessionItemSequence(ctx, sid)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: session_id=%s", ErrSessionNotFound, sessionID)
		}
		return nil, fmt.Errorf("session: read item_sequence: %w", err)
	}
	entries, err := decodeEntries(raw)
	if err != nil {
		return nil, err
	}
	return &TopicOrder{SessionID: sessionID, Entries: entries}, nil
}
