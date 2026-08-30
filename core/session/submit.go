// submit.go 承载 T-W5-018 的作答提交幂等与并发安全核心（Python 冻结实现
// src/core/session/service.py submit_answer 的 Go 重锚定——事务与并发面）：
//
//   - 幂等：同一 (session, item, 作答指纹) 重复提交 → 幂等成功返回原事件 id，
//     不重复落账、不重复推进（board 验收「重复提交返回首次结果且不写新事件」）；
//   - 并发：同一会话的并发提交在临界区内串行化——内存实现=互斥锁，PG 实现=
//     per-session advisory xact lock + 会话行 FOR UPDATE（双锁分层，序恒定：
//     先 advisory 后行锁），恰一条 response_event 入账、current_index 恰推进 1；
//     幂等键唯一性由迁移 0031 的 pk_response_submission 复合主键兜底（23505 →
//     ErrSubmissionConflict 明确失败，异常不泄漏）。
//
// 提交指纹：input 规范化摘要（core/gate/validators.ContentDigest——D3 摘要
// 口径唯一源，键序/空白不敏感；时长等重试噪声不入指纹，见 fingerprint）。
//
// 事务纪律（S4/D11，与 T-W5-017 的事件写入边界同构）：SubmitAnswer 只接受
// 调用方已 begin 的显式事务执行面（Executor），本域不自 begin/commit/
// rollback——作答事件（经 core/events.Writer）、幂等登记账与会话状态推进
// 在同一外层事务里同进同退（验收 #4「整个提交是单事务」）。
package session

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/jackc/pgx/v5/pgtype"
)

// 会话状态四值域（与迁移 0011 的 ck_practice_session_status_domain 同值）.
const (
	StatusActive       = "active"
	StatusRestPrompted = "rest_prompted"
	StatusCompleted    = "completed"
	StatusAbandoned    = "abandoned"
)

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
// ErrNoTransaction / ErrSessionNotFound 是本包题序固化面（T-W5-004）与作答
// 提交面（本卡）的共享哨兵，声明见 topicorder.go——判据单一来源.
var (
	// ErrInvalidSubmission 表示提交入参违反契约（细分原因见 wrap 文本）：
	// session_id 非 UUID、载荷/评分轨迹缺失、作答内容不可规范化等。契约违例
	// 在出 Go 进程前拦截，不烧临界区语句（与 core/events.ErrInvalidEvent 同构）.
	ErrInvalidSubmission = errors.New("session: 作答提交入参违反契约 response_event.md")

	// ErrSessionCompleted 表示会话已完成或题目已走完，不能再作答
	// （Python 冻结实现 SessionCompletedError 的哨兵化）.
	ErrSessionCompleted = errors.New("session: 会话已完成，不能再作答")

	// ErrSessionState 表示会话状态不允许提交作答（已放弃/状态域外账损）
	// （Python 冻结实现 SessionStateError 的哨兵化）.
	ErrSessionState = errors.New("session: 会话状态不允许提交作答")

	// ErrRestRequired 表示时长保护触发（§4.8 用眼保护）：连续作答超过学段
	// 阈值，须休息确认后继续（resume 重置计时）。细分 elapsed/limit 载荷见
	// RestRequiredError；提交路径零事件写入，仅把会话置 rest_prompted.
	ErrRestRequired = errors.New("session: 时长保护触发（连续作答超过学段阈值，须休息确认后继续）")

	// ErrOutOfSequence 表示作答题目不是当前应答题目（会话按序列逐题推进，
	// 不允许跳答/补答）。细分期望题/实收题见 OutOfSequenceError.
	ErrOutOfSequence = errors.New("session: 作答题目不是当前应答题目（会话按序列逐题推进）")

	// ErrSubmissionConflict 表示幂等登记账唯一主键拒绝了本次插入（SQLSTATE
	// 23505）。advisory lock 正常工作时不应出现；出现即视为数据库层防线的
	// 明确失败信号——返回本错误而非让驱动异常穿透（验收 #3「返回结果一致」
	// 的失败面：调用方重试后走幂等命中分支）.
	ErrSubmissionConflict = errors.New("session: 作答提交幂等键唯一性冲突（23505），请重试")

	// ErrLedgerCorrupted 表示会话账面损坏（item_sequence 反序列化失败/
	// 序列条目缺 item_version_id）：提交链路的取数前提被破坏，fail-closed
	// 拒绝而非猜进度推进（账损不修复、不带病入账）.
	ErrLedgerCorrupted = errors.New("session: 会话账面损坏（item_sequence）")
)

// RestRequiredError 是时长保护触发的结构化载体：elapsed/limit 供协议层与
// 审计使用，Error() 文本即休息提示文案（§4.8 用眼保护，API 层原样透出——
// Python 冻结实现 RestRequiredError 的同义移植）.
type RestRequiredError struct {
	ElapsedSec   int
	TimeLimitSec int
	message      string
}

// Error 实现 error；message 为用户向休息提示文案（不含作答内容与内部地址）.
func (e *RestRequiredError) Error() string {
	if e.message == "" {
		return ErrRestRequired.Error()
	}
	return ErrRestRequired.Error() + ": " + e.message
}

// Unwrap 锚定哨兵：errors.Is(err, ErrRestRequired) 对本类型恒真.
func (e *RestRequiredError) Unwrap() error { return ErrRestRequired }

// OutOfSequenceError 是题序违例的结构化载体：期望题/实收题对（审计面；
// 均为内容寻址 id，非 PII）.
type OutOfSequenceError struct {
	Expected string
	Got      string
}

// Error 实现 error.
func (e *OutOfSequenceError) Error() string {
	return fmt.Sprintf("%s: 当前应答题为 %q，收到 %q", ErrOutOfSequence.Error(), e.Expected, e.Got)
}

// Unwrap 锚定哨兵：errors.Is(err, ErrOutOfSequence) 对本类型恒真.
func (e *OutOfSequenceError) Unwrap() error { return ErrOutOfSequence }

// SubmitInput 是一次作答提交请求（Python 冻结实现 submit_answer 参集的
// Go 重锚定，评分产物由调用方先行产出后随事件落账——评分链路见 core/scoring）.
type SubmitInput struct {
	// SessionID 作答会话 id（合法 UUID；学生身份取会话行的 student_alias_id，
	// 本输入不携带 alias——防止跨学生事件注入，D9 同源纪律）.
	SessionID string
	// ItemVersionID 作答题目版本（必须是当前应答题，序列纪律）.
	ItemVersionID string
	// Response 原始作答载荷（作答内容本身，R-D-01；幂等指纹的摘要对象）.
	// 结构由交互类型 response_schema 保证；nil 拒绝（契约 §1 JSON object 必填）.
	Response map[string]any
	// DurationMs 作答耗时毫秒；nil=NULL=未知（禁止填 0 冒充，契约 §1）.
	// 不入幂等指纹：网络重试的耗时天然不同，入指纹会把合法重试误判为新作答.
	DurationMs *int32
	// ScoringTrace 评分轨迹（契约 §3 必填）。残缺评分不落账——trace 缺失在
	// 临界区外前置拒绝（fail-closed，不烧事务语句）.
	ScoringTrace map[string]any
	// ErrorInferences 错误推断数组（契约 §4）；nil 与空数组同义记空集.
	ErrorInferences []map[string]any
	// At 提交时刻（事件 created_at 与时长保护的统一时间基准）；
	// 零值取当前时刻（测试注入确定性时钟）.
	At time.Time
}

// SubmissionStore 是作答提交的语义契约（内存/PG 双实现，W6 服务化换装）.
//
// 并发契约（本卡核心交付）：对同一会话的全部提交构成单一原子临界区，并发
// 调用互斥串行化（内存=互斥锁；PG=per-session advisory xact lock + 会话行
// FOR UPDATE + 幂等登记账唯一主键兜底）。并发提交同一题 → 恰一条
// response_event 入账、current_index 恰推进 1（board 验收）；其余提交要么
// 幂等命中（duplicate=true，返回首次 event_id、零副作用），要么被题序/
// 状态校验明确拒绝。幂等约定：同一 (session, item, 作答指纹) 重复提交恒定
// 返回首次 event_id——先查幂等账再校验状态/题序，已完成会话上的迟到重试
// 依然是幂等成功而非报错（幂等语义对重试全时态成立）.
type SubmissionStore interface {
	// SubmitAnswer 在调用方显式事务执行面 q 上提交一次作答：幂等判定 →
	// 状态/时长/题序校验 → 事件入账（经 core/events.Writer）→ 幂等登记 →
	// 会话推进。duplicate=true 表示幂等命中（eventID 为首次提交的事件 id）；
	// false 表示本次为真实入账。任何失败路径零事件写入、零进度推进.
	SubmitAnswer(ctx context.Context, q Executor, in SubmitInput) (eventID string, duplicate bool, err error)
}

// Executor 是提交链路所需语句执行面的最小抽象——本包双面（题序固化/作答
// 提交）共用 topicorder.go 的本地声明：方法集与生成层 dbgen.DBTX 同构，pgx.Tx
// 与连接池事务面天然同时满足。全部语句文本只住在 db/queries/*.sql（SQL-2：
// 不在 Go 拼 SQL），经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方.

// preparedSubmit 是校验定影后的提交载荷（内存与 PG 两实现的共同前置管线：
// 对同一非法输入必然给出同一条哨兵错误，判据单一来源，不存在实现间漂移面——
// core/compliance 预检管线同款纪律）.
type preparedSubmit struct {
	sid           [16]byte // session_id 解析字节（PG 形参直用）
	rawSessionID  string   // 调用方原始书写（幂等键与内存账键）
	itemVersionID string
	digest        string // 作答提交指纹（fingerprint 输出，幂等键第三元）
	response      map[string]any
	duration      *int32
	trace         map[string]any
	inferences    []map[string]any
	at            time.Time
}

// prepareSubmit 执行身份/载荷/指纹三段前置校验；now 为实现的时钟回落
// （At 零值时取用）。全部检查在进入临界区之前完成——契约违例不烧锁语句.
func prepareSubmit(in SubmitInput, now func() time.Time) (*preparedSubmit, error) {
	var u pgtype.UUID
	if err := u.Scan(in.SessionID); err != nil || !u.Valid {
		return nil, fmt.Errorf("%w: session_id=%q 不是合法 UUID", ErrInvalidSubmission, in.SessionID)
	}
	if in.ItemVersionID == "" {
		return nil, fmt.Errorf("%w: item_version_id 不能为空（§5 minLength=1）", ErrInvalidSubmission)
	}
	if in.Response == nil {
		return nil, fmt.Errorf("%w: response 原始作答载荷必填（JSON object，R-D-01）", ErrInvalidSubmission)
	}
	if in.ScoringTrace == nil {
		return nil, fmt.Errorf("%w: scoring_trace 必填（§3）——评分先行、落账随后，残缺评分不落账", ErrInvalidSubmission)
	}
	if in.DurationMs != nil && *in.DurationMs < 0 {
		return nil, fmt.Errorf("%w: duration_ms=%d 负数非法（§5 minimum=0）", ErrInvalidSubmission, *in.DurationMs)
	}
	digest, err := fingerprint(in.ItemVersionID, in.Response)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrInvalidSubmission, err)
	}
	at := in.At
	if at.IsZero() {
		at = now()
	}
	return &preparedSubmit{
		sid:           u.Bytes,
		rawSessionID:  in.SessionID,
		itemVersionID: in.ItemVersionID,
		digest:        digest,
		response:      in.Response,
		duration:      in.DurationMs,
		trace:         in.ScoringTrace,
		inferences:    in.ErrorInferences,
		at:            at,
	}, nil
}

// fingerprint 计算作答提交指纹：提交输入（题目版本 + 原始作答载荷）的规范化
// 摘要。口径唯一源纪律（D3）：复用 core/gate/validators.ContentDigest——
// CanonicalJSON 键序升序、空白无关，同一作答内容的任意键序重排必得同一指纹
// （幂等判定的正确性前提）；时长/评分轨迹等重试噪声不入指纹。输出形如
// "sha256:<64hex>"，与迁移 0031 的 ck_response_submission_digest_shape 物理锚定.
func fingerprint(itemVersionID string, response map[string]any) (string, error) {
	return validators.ContentDigest(map[string]any{
		"item_version_id": itemVersionID,
		"response":        response,
	})
}

// sessionView 是提交校验所需的会话行投影（内存行与 PG 行的共同判定视图，
// 投影只留校验所需字段——两实现从各自账面装配，装配错误按账损处理）.
type sessionView struct {
	Status         string
	Scene          string
	StudentAliasID string
	Sequence       []string // item_version_id 主序列（题序不可变，004 冻结面）
	CurrentIndex   int
	TimeLimitSec   int
	LastResumeAt   time.Time
}

// restPromptMessage 是休息提示文案（§4.8 用眼保护；提交与取题两个触发点
// 共用同一文案源——文案分叉即用户体验与审计口径的漂移面）.
func restPromptMessage(timeLimitSec int) string {
	minutes := timeLimitSec / 60
	return fmt.Sprintf("已连续作答超过 %d 分钟，该休息了——站起来活动一下、看看远处，休息好后回来继续。", minutes)
}

// validateSubmitAgainstSession 是提交前置校验的纯函数核（Python 冻结实现
// submit_answer 校验序的同义移植，顺序即语义、不得重排）：
//
//	状态（completed/abandoned 即拒）→ 时长保护（超限拒且须置 rest_prompted）
//	→ 题序（期望题=主序列 current_index；题目走完即拒）。
//
// 返回期望题 id；时长保护触发时返回 *RestRequiredError（调用方负责把会话置
// rest_prompted——纯函数不做副作用，置位的持久化由各实现在同一事务内完成）.
func validateSubmitAgainstSession(v sessionView, p *preparedSubmit) (string, error) {
	switch v.Status {
	case StatusCompleted:
		return "", fmt.Errorf("%w: 会话已完成，不能再作答", ErrSessionCompleted)
	case StatusAbandoned:
		return "", fmt.Errorf("%w: 会话已放弃，不能再作答", ErrSessionState)
	case StatusActive, StatusRestPrompted:
		// 时长保护（Python _check_time_protection 同语义）：completed/abandoned
		// 豁免，其余状态都查——rest_prompted 未 resume 时 last_resume_at 未动，
		// elapsed 仍超限，继续拒绝（休息确认是恢复作答的唯一出口）.
		elapsed := int(p.at.Sub(v.LastResumeAt).Seconds())
		if elapsed < 0 {
			elapsed = 0
		}
		if elapsed > v.TimeLimitSec {
			return "", &RestRequiredError{
				ElapsedSec:   elapsed,
				TimeLimitSec: v.TimeLimitSec,
				message:      restPromptMessage(v.TimeLimitSec),
			}
		}
	default:
		return "", fmt.Errorf("%w: status=%q 不在会话状态域（账损防御）", ErrSessionState, v.Status)
	}
	if v.CurrentIndex >= len(v.Sequence) {
		return "", fmt.Errorf("%w: 会话题目已走完，不能再作答", ErrSessionCompleted)
	}
	expected := v.Sequence[v.CurrentIndex]
	if expected == "" {
		return "", fmt.Errorf("%w: 序列第 %d 项缺 item_version_id", ErrLedgerCorrupted, v.CurrentIndex)
	}
	if p.itemVersionID != expected {
		return "", &OutOfSequenceError{Expected: expected, Got: p.itemVersionID}
	}
	return expected, nil
}
