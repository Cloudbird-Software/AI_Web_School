package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/events"
	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// 编译期锚定：两种实现都必须兑现 SubmissionStore 的并发契约（Executor 与
// dbgen.DBTX 的锚定见 topicorder.go 锚定一/二，本面共用）.
var (
	_ SubmissionStore = (*MemoryStore)(nil)
	_ SubmissionStore = (*PGStore)(nil)
)

// PGStore 是 SubmissionStore 的 PG 生产实现（类型与构造器声明在
// topicorder_pg.go——题序固化面与提交面共用同一个无状态 PG 装配点）.
//
// 并发临界区构成（SubmitAnswer 在调用方显式事务内依次执行，语句全部来自
// db/queries/session.sql 的类型安全生成方法，序恒定不可重排）：LockSessionSubmission
// （per-session advisory xact lock）→ GetSubmissionByIdempotencyKey（幂等判定
// 先行：命中即原样返回首次事件 id、零副作用——已完成会话上的迟到重试依然是
// 幂等成功而非报错）→ GetSessionForSubmit（会话行 FOR UPDATE，验收 #1 的行锁
// 字面语义）→ 状态/时长/题序校验（与内存实现共享同一纯函数核）→ 事件入账
// （core/events.Writer——T-W5-017 的显式事务写入面）→ InsertResponseSubmission
// （幂等登记）→ AdvanceSessionAfterSubmit（推进恰 +1）。
//
// 三层并发防线：advisory xact lock 串行化同一会话的全部提交者（含首次提交
// 竞态——幂等登记账尚无行可锁，行锁方案在该场景退化为纯索引兜底）；会话行
// FOR UPDATE 把本事务与其他运行态写路径（resume/abandon 等，W6 接线）互斥；
// 复合主键 pk_response_submission（0031）是最后一道防线，其拒绝（SQLSTATE
// 23505）被翻译为哨兵 ErrSubmissionConflict 而非驱动异常穿透。advisory 锁在
// 事务结束自动释放；幂等命中路径在锁内直返——锁的代价对重放同样成立，这
// 正是「重复提交恰返回首次结果」的串行化前提.
//
// 事务纪律（S4/D11）：本类型不持有连接、不自 begin/commit——一次提交 = 一个
// 外层事务；q 必须是调用方已 begin 的事务执行面，连接装配在 W6 服务化接线。
// 失败路径的副作用归属同一事务由调用方定夺：题序/状态拒绝零写入；时长保护
// 的 rest_prompted 置位与 Python 冻结实现同语义（错误上抛、置位随外层事务
// 提交或回滚——把错误映射为休息提示的调用方应提交，使其成为可恢复状态）.

// SubmitAnswer 实现 SubmissionStore：完整临界区见类型注释.
func (s *PGStore) SubmitAnswer(ctx context.Context, q Executor, in SubmitInput) (string, bool, error) {
	if q == nil {
		return "", false, ErrNoTransaction
	}
	p, err := prepareSubmit(in, time.Now)
	if err != nil {
		return "", false, err
	}
	qs := dbgen.New(q)
	sid := pgtype.UUID{Bytes: p.sid, Valid: true}

	// 1) per-session advisory lock：串行化该会话的全部提交者.
	if err := qs.LockSessionSubmission(ctx, p.rawSessionID); err != nil {
		return "", false, fmt.Errorf("session/pg advisory lock: %w", err)
	}
	// 2) 幂等判定先行：三元组命中即取回首次事件（复合回指 event_id +
	// event_created_at），零副作用、不触碰会话行（验收 #2 的字面语义）.
	rec, err := qs.GetSubmissionByIdempotencyKey(ctx, dbgen.GetSubmissionByIdempotencyKeyParams{
		SessionID:     sid,
		ItemVersionID: p.itemVersionID,
		AnswerDigest:  p.digest,
	})
	if err == nil {
		return formatUUID(rec.EventID.Bytes), true, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", false, fmt.Errorf("session/pg idempotency lookup: %w", err)
	}
	// 3) 会话行 FOR UPDATE：题序与推进的判定锚（验收 #1）.
	row, err := qs.GetSessionForSubmit(ctx, sid)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", false, fmt.Errorf("%w: %q", ErrSessionNotFound, p.rawSessionID)
		}
		return "", false, fmt.Errorf("session/pg load session: %w", err)
	}
	v, err := sessionViewFromGen(&row)
	if err != nil {
		return "", false, err
	}
	// 4) 状态/时长/题序校验（共享纯函数核，与内存实现同判据）.
	if _, err := validateSubmitAgainstSession(v, p); err != nil {
		var rre *RestRequiredError
		if errors.As(err, &rre) {
			// 时长保护置位（Python 同语义）：错误上抛，置位随外层事务定夺.
			if merr := qs.MarkSessionRestPrompted(ctx, sid); merr != nil {
				return "", false, fmt.Errorf("session/pg rest prompted: %w", merr)
			}
		}
		return "", false, err
	}
	// 5) 事件入账：经 core/events.Writer（T-W5-017 显式事务面）——事件与
	// 会话推进、幂等登记同处一个事务，任一步失败同进同退（验收 #4）.
	eventID, err := newEventID()
	if err != nil {
		return "", false, err
	}
	sessionRef := p.rawSessionID
	if _, err := events.WithTx(q).Record(ctx, events.Input{
		EventID:         eventID,
		StudentAliasID:  v.StudentAliasID,
		ItemVersionID:   p.itemVersionID,
		Scene:           events.Scene(v.Scene),
		RawPayload:      p.response,
		DurationMs:      p.duration,
		ScoringTrace:    p.trace,
		ErrorInferences: p.inferences,
		SessionID:       &sessionRef,
		CreatedAt:       p.at,
	}); err != nil {
		return "", false, fmt.Errorf("session/pg record event: %w", err)
	}
	// 6) 幂等登记：复合主键兜底（23505 → ErrSubmissionConflict，异常不泄漏）.
	if err := qs.InsertResponseSubmission(ctx, dbgen.InsertResponseSubmissionParams{
		SessionID:      sid,
		ItemVersionID:  p.itemVersionID,
		AnswerDigest:   p.digest,
		EventID:        pgtype.UUID{Bytes: mustParseUUID(eventID), Valid: true},
		EventCreatedAt: tsTZ(p.at),
		CreatedAt:      tsTZ(p.at),
	}); err != nil {
		return "", false, fmt.Errorf("session/pg register submission: %w", mapUniqueViolation(err))
	}
	// 7) 推进恰 +1 + 对错记账（board 验收「current_index 恰好推进 1」的物理面；
	// 2026-08-31 E2E 实证修复：与内存实现同构——显式判对累加 correct_count，
	// 显式判错追加 wrong_marks 错题标记；轨迹不含显式判定两账均不动）.
	explicit, correct := traceCorrect(p.trace)
	var correctDelta int32
	var wrongMark []byte
	if explicit {
		if correct {
			correctDelta = 1
		} else {
			mk, err := json.Marshal(newWrongMarkPG(p, v.CurrentIndex, row.RetestWrong))
			if err != nil {
				return "", false, fmt.Errorf("session/pg encode wrong mark: %w", err)
			}
			wrongMark = mk
		}
	}
	if err := qs.AdvanceSessionAfterSubmit(ctx, dbgen.AdvanceSessionAfterSubmitParams{
		SessionID:      sid,
		LastActivityAt: tsTZ(p.at),
		CorrectDelta:   correctDelta,
		WrongMark:      wrongMark,
	}); err != nil {
		return "", false, fmt.Errorf("session/pg advance session: %w", err)
	}
	return eventID, false, nil
}

// newWrongMarkPG 构造错题标记（内存实现 newWrongMark 的 PG 面：字段形状
// 同构——item_version_id/item_number/error_type_ids/first_seen_at/retest_status；
// item_number 按推进前的 current_index + 1，与内存实现同一口径）.
func newWrongMarkPG(p *preparedSubmit, currentIndex int, retestWrong bool) map[string]any {
	status := "off"
	if retestWrong {
		status = "pending"
	}
	return map[string]any{
		"item_version_id": p.itemVersionID,
		"item_number":     currentIndex + 1,
		"error_type_ids":  inferenceErrorTypeIDs(p.inferences),
		"first_seen_at":   p.at,
		"retest_status":   status,
	}
}

// sessionViewFromGen 把 practice_session 行装配为共享校验视图。题序还原复用
// 包内 decodeEntries（T-W5-004 题序面同一解码与 Seq 升序 canonical——提交
// 判序与题序固化面同源，无漂移面）；反序列化失败按账损处理（fail-closed
// 拒绝提交，不带病推进进度）.
func sessionViewFromGen(row *dbgen.PracticeSession) (sessionView, error) {
	entries, err := decodeEntries(row.ItemSequence)
	if err != nil {
		return sessionView{}, fmt.Errorf("%w: %w", ErrLedgerCorrupted, err)
	}
	v := sessionView{
		Status:         row.Status,
		Scene:          row.Scene,
		StudentAliasID: formatUUID(row.StudentAliasID.Bytes),
		Sequence:       make([]string, len(entries)),
		CurrentIndex:   int(row.CurrentIndex),
		TimeLimitSec:   int(row.TimeLimitSec),
		LastResumeAt:   row.LastResumeAt.Time,
	}
	for i := range entries {
		v.Sequence[i] = entries[i].ItemVersionID
	}
	return v, nil
}

// mustParseUUID 解析本层自产的 event_id（newEventID 输出必为合法 UUID）：
// 失败只可能是内部管线被破坏，panic 显式暴露而非伪装成账损错误.
func mustParseUUID(s string) [16]byte {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil {
		panic(fmt.Sprintf("session: 自产 event_id 解析失败（内部管线破坏）: %v", err))
	}
	return u.Bytes
}

// mapUniqueViolation 把幂等登记账唯一主键拒绝翻译为哨兵错误
// ErrSubmissionConflict（errors.Is 可判）；非唯一冲突原样放行——异常不泄漏，
// 但也绝不吞真故障。（23505 判定复用包内题序面的 isUniqueViolation 与
// sqlStateUniqueViolation——判据单一来源。）
func mapUniqueViolation(err error) error {
	if isUniqueViolation(err) {
		// 双 %w：哨兵错误与原始驱动错误都留在 wrap 链里——调用方既能 errors.Is
		// 分支，也能回溯 SQLSTATE 证据（%v 会斩断链路，属吞错反模式）.
		return fmt.Errorf("%w: %w", ErrSubmissionConflict, err)
	}
	return err
}
