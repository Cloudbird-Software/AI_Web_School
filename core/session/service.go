package session

// service.go 承载 GO-RW-002 的会话全链路服务面：把 consent 门（consent.go）、
// 题序固化账（topicorder.go）、作答提交账（submit.go）与作答事件写入服务
// （core/events/writer.go）聚合为单个 SessionService，对协议层暴露
// Start / GetNext / Submit / Resume / Abandon（+ State 只读投影）六个动词，
// 对齐冻结契约 specs/contracts/api/openapi-v1.1.json 的会话端点行为
// （Python 冻结实现 src/core/session/service.py 的语义基准）.
//
// 事务纪律（S4/D11，显式事务面）：本服务不自管理连接、不在领域内零散 begin
// ——依赖清单里的 TxRunner 是装配层注入的显式事务执行面（生产 = pgxpool 的
// Begin/Commit/Rollback 包装；内存面 = LocalRunner 传 nil 执行器），每个服务
// 方法的读写都发生在同一次 InTx 调用里。作答事件由此与幂等登记、会话推进
// 同进同退：提交临界区（SubmissionStore.SubmitAnswer）内部经
// events.WithTx(q).Record 写事件（T-W5-017 的显式事务写入服务——事件写入
// 面的聚合点在提交账内、事务边界由本服务的 TxRunner 定夺），任一环失败
// 整体回滚，不存在「事件已入账、会话未推进」的中间态.
//
// 授权门前置（宪法红线 / X12）：Start 的第一件事是家长授权门
// （PurposeOnlinePractice，见 consent.go）——api 层的前置门不变（越权判据
// 仍先于授权门），本服务在业务写入之前再判一次：业务规则住在 core，
// 协议层只做映射。账本未装配 / 读取失败 fail-closed 拒绝，绝不放行.
//
// 归属断言（D9）：全部子资源动词先读会话行比对 student_alias_id == 调用
// 主体 alias（T-W5-006 留痕的「第二道校验」在本卡落地）；归属锚列不可变
// （0030 触发器），预读不存在 TOCTOU 可改写面.

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// 哨兵错误：调用方按 errors.Is 分支处理（协议层据此映射 HTTP 状态）.
var (
	// ErrNotSessionOwner 表示会话归属断言失败：调用主体 alias ≠ 会话行
	// student_alias_id（D9 的机器强制；对外统一粗粒度 forbidden）.
	ErrNotSessionOwner = errors.New("session: 会话不属于调用主体")

	// ErrRetestRoundUnavailable 表示主序列走完且回测开启，但回测轮的状态机
	// 迁移归 W6 域（本波次会话域只覆盖主序列全链路）——明确拒绝而非伪造
	// done 或错题重放.
	ErrRetestRoundUnavailable = errors.New("session: 错题回测轮尚未接线（W6 会话状态机域）")

	// ErrPaperSequenceUnavailable 表示会话启动选择了静态卷来源，但静态卷
	// 题序解析（paper_item.item_number，W2 追溯表读模型）尚未在 Go 侧接线
	// ——显式拒绝而非返回空序列（fail-closed）.
	ErrPaperSequenceUnavailable = errors.New("session: 静态卷题序解析面尚未接线（W2 追溯表读模型）")
)

// TxRunner 是显式事务执行面：在单一事务里执行 fn（D11）。fn 收到的 Executor
// 供账本方法使用；内存实现接受 nil（LocalRunner），PG 实现收到已 begin 的
// pgx.Tx（PGRunner）。fn 返回错误 → 整体回滚，服务方法的读写在同一次 InTx
// 内完成原子闭合.
type TxRunner interface {
	InTx(ctx context.Context, fn func(q Executor) error) error
}

// LocalRunner 是内存面的事务执行器：进程内账本无需真事务，fn(nil) 直通——
// 账本方法的互斥锁临界区即原子边界（memory.go 的单锁论证）。生产面执行器
// 不住本包：pgxpool 的 Begin/Commit/Rollback 包装属装配层职责（D11 静态守卫
// ——领域源码零 Commit/Rollback 调用面），见 cmd/school 的 poolTxRunner.
type LocalRunner struct{}

// InTx 实现 TxRunner：fn 以 nil 执行器直通（内存账本契约内形态）.
func (LocalRunner) InTx(_ context.Context, fn func(q Executor) error) error {
	if fn == nil {
		return errors.New("session: LocalRunner 收到 nil 事务函数（装配错误）")
	}
	return fn(nil)
}

// TxRunnerFunc 是函数形态的事务执行器（测试/装配便捷面）.
type TxRunnerFunc func(ctx context.Context, fn func(q Executor) error) error

// InTx 实现 TxRunner.
func (f TxRunnerFunc) InTx(ctx context.Context, fn func(q Executor) error) error {
	return f(ctx, fn)
}

// RuntimeStore 是会话运行态账的服务面契约（内存/PG 双实现，见
// runtime_memory.go / runtime_pg.go）：运行态只读投影 + 三个状态迁移.
//
// 并发契约：迁移与提交推进在同一实现内互斥（内存=同一把锁；PG=调用方
// 事务内的行级语句），不存在半迁移状态可被观察到.
type RuntimeStore interface {
	// RuntimeState 返回会话运行态投影；会话不存在 → ErrSessionNotFound.
	RuntimeState(ctx context.Context, q Executor, sessionID string) (*SessionRuntime, error)
	// Resume 休息确认（rest_prompted/active → active，计时锚点重置）；
	// completed/abandoned → ErrSessionState. 返回迁移后投影.
	Resume(ctx context.Context, q Executor, sessionID string, at time.Time) (*SessionRuntime, error)
	// Abandon 放弃会话（completed → ErrSessionState；已作答事件保留在账）.
	Abandon(ctx context.Context, q Executor, sessionID string, at time.Time) (*SessionRuntime, error)
	// MarkRestPrompted 时长保护置位（零事件写入，Python _check_time_protection
	// 同语义；resume 是恢复作答的唯一出口）.
	MarkRestPrompted(ctx context.Context, q Executor, sessionID string) error
}

// 编译期锚定：双实现兑现 RuntimeStore.
var (
	_ RuntimeStore = (*MemoryStore)(nil)
	_ RuntimeStore = (*PGStore)(nil)
)

// SessionRuntime 是会话运行态投影（SessionState/取题判定的判定源；Entries
// 为 Seq 升序 canonical 题序快照，WrongMarks 为错题标记深拷贝）.
type SessionRuntime struct {
	SessionID      string
	StudentAliasID string
	Scene          string
	Gradeband      string
	PaperID        *string
	Status         string
	RetestWrong    bool
	Entries        []TopicEntry
	CurrentIndex   int
	AnsweredCount  int
	CorrectCount   int
	WrongMarks     []map[string]any
	TimeLimitSec   int
	StartedAt      time.Time
	LastResumeAt   time.Time
	LastActivityAt time.Time
	CompletedAt    *time.Time
}

// Deps 是 SessionService 的依赖清单（六边形端口；装配层注入具体实现）.
//
// 事件写入服务的进入路径说明：events.Writer 是绑定显式事务的写入服务
// （events.WithTx(q)），作答事件在其唯一合法入口——提交账
// SubmissionStore.SubmitAnswer 的事务临界区内落账（PG 面经
// events.WithTx(q).Record，内存面经 ledger 投影账）；本服务以 TxRunner 为
// 其提供外层事务执行面，保证「事件—幂等登记—会话推进」同进同退。清单里
// 不单列 Writer 端口：事件写入不是本服务的第二个入口，避免出现绕过提交
// 临界区的第二条事件通道.
type Deps struct {
	// Consents 家长授权账（Start 的授权门；nil 属装配错误，构造期拒绝）.
	Consents compliance.ConsentStore
	// Orders 题序固化账（Start 经其开立会话；幂等/冲突语义见 TopicOrderStore）.
	Orders TopicOrderStore
	// Submissions 作答提交账（Submit 的临界区：幂等判定/校验/事件/推进）.
	Submissions SubmissionStore
	// Accounts 会话运行态账（状态投影 + 休息/放弃/时长保护迁移）.
	Accounts RuntimeStore
	// Runner 显式事务执行面（D11；生产 PGRunner，内存面 LocalRunner）.
	Runner TxRunner
	// Reader 非事务读面（归属预读等；生产 = pgxpool，内存面可 nil——内存
	// 账本方法本就接受 nil 执行器）.
	Reader Executor
	// Now 时钟（测试注入确定性；零值回落 time.Now）.
	Now func() time.Time
}

// Service 是会话全链路服务（无状态聚合点；并发安全——全部状态住在账本）.
type Service struct {
	d Deps
}

// NewService 构造服务：账本/事务面缺失即装配错误（fail fast，与
// middleware.RequireAuth 的 nil-signer 同一纪律——绝不带病装配）.
func NewService(d Deps) (*Service, error) {
	if d.Consents == nil {
		return nil, errors.New("session: Service 缺家长授权账（Consents 未装配）")
	}
	if d.Orders == nil {
		return nil, errors.New("session: Service 缺题序固化账（Orders 未装配）")
	}
	if d.Submissions == nil {
		return nil, errors.New("session: Service 缺作答提交账（Submissions 未装配）")
	}
	if d.Accounts == nil {
		return nil, errors.New("session: Service 缺会话运行态账（Accounts 未装配）")
	}
	if d.Runner == nil {
		return nil, errors.New("session: Service 缺显式事务执行面（Runner 未装配，D11）")
	}
	if d.Now == nil {
		d.Now = time.Now
	}
	return &Service{d: d}, nil
}

// StartParams 是一次会话开立请求（协议无关；字段口径对齐 openapi-v1.1
// StartSessionRequest——身份取自令牌主体，不由请求体承载）.
type StartParams struct {
	// Scene 会话场景；空回落 practice（契约 default 同义）.
	Scene string
	// Gradeband 学段（L/M/H）——时长保护阈值定型来源；实例池会话必填
	// （静态卷会话的缺省取 paper.gradeband，该来源面未接线前同样必填）.
	Gradeband string
	// PaperID 静态卷 id（与 ItemVersionIDs 互斥；本波次静态卷解析面未接线，
	// 选择即 ErrPaperSequenceUnavailable）.
	PaperID *string
	// ItemVersionIDs 实例池序列（调用方给定顺序即题序；Seq 按位赋 1..n）.
	ItemVersionIDs []string
	// RetestWrong 主序列走完后是否对错题回测一轮（回测轮迁移归 W6）.
	RetestWrong bool
	// StartedAt 开始时刻；零值取服务时钟.
	StartedAt time.Time
}

// StartResult 是会话开立的返回投影（openapi-v1.1 StartSessionResponse 域）.
type StartResult struct {
	SessionID    string
	Status       string
	Scene        string
	Gradeband    string
	Total        int
	TimeLimitSec int32
}

// Start 开立会话：家长授权门（业务规则，前置于一切写入）→ 题序固化 +
// 运行态开立（同一事务，幂等语义见 TopicOrderStore.Create）.
func (s *Service) Start(ctx context.Context, callerAlias string, p StartParams) (*StartResult, error) {
	in, err := s.startInput(callerAlias, p)
	if err != nil {
		return nil, err
	}
	limit := GradebandTimeLimitSec[in.Gradeband]
	var order *TopicOrder
	terr := s.d.Runner.InTx(ctx, func(q Executor) error {
		// 家长授权门：前置于题序固化（X12 fail-closed；拒绝零写入）.
		if cerr := requireOnlinePracticeConsentExec(ctx, s.d.Consents, q, callerAlias); cerr != nil {
			return cerr
		}
		order, err = s.d.Orders.Create(ctx, q, in)
		return err
	})
	if terr != nil {
		return nil, terr
	}
	return &StartResult{
		SessionID:    order.SessionID,
		Status:       StatusActive,
		Scene:        in.Scene,
		Gradeband:    in.Gradeband,
		Total:        len(order.Entries),
		TimeLimitSec: limit,
	}, nil
}

// startInput 装配题序固化请求：来源互斥校验（契约「paper_id 与
// item_version_ids 二选一」）+ 实例池序列的题序条目化（placement_token 全
// nil——冻结 pool 路径 placement_tokens=[None]*n 同形；静态卷路径显式拒绝）.
func (s *Service) startInput(callerAlias string, p StartParams) (StartInput, error) {
	switch {
	case p.PaperID != nil && len(p.ItemVersionIDs) > 0:
		return StartInput{}, fmt.Errorf("%w: paper_id 与 item_version_ids 必须且只能提供一个", ErrInvalidSessionStart)
	case p.PaperID != nil:
		return StartInput{}, fmt.Errorf("%w: paper_id=%s", ErrPaperSequenceUnavailable, *p.PaperID)
	case len(p.ItemVersionIDs) == 0:
		return StartInput{}, fmt.Errorf("%w: 题目来源缺失（paper_id / item_version_ids 二选一）", ErrInvalidSessionStart)
	}
	entries := make([]TopicEntry, len(p.ItemVersionIDs))
	for i, id := range p.ItemVersionIDs {
		entries[i] = TopicEntry{Seq: i + 1, ItemVersionID: id}
	}
	startedAt := p.StartedAt
	if startedAt.IsZero() {
		startedAt = s.d.Now()
	}
	return StartInput{
		StudentAliasID: callerAlias,
		Scene:          p.Scene,
		Gradeband:      p.Gradeband,
		PaperID:        p.PaperID,
		Entries:        entries,
		RetestWrong:    p.RetestWrong,
		StartedAt:      startedAt,
	}, nil
}

// StateResult 是会话状态投影（openapi-v1.1 SessionState 域；时长判定字段由
// 服务装配，协议层零业务计算）.
type StateResult struct {
	SessionID        string
	Status           string
	Scene            string
	Gradeband        string
	PaperID          *string
	Total            int
	MainAnswered     int
	AnsweredCount    int
	CorrectCount     int
	WrongCount       int
	RetestPending    int
	ElapsedActiveSec int
	TimeLimitSec     int
	RemainingSec     int
	StartedAt        time.Time
	CompletedAt      *time.Time
}

// State 返回会话状态（进度/已用时长/时长保护余量；Python get_session_state
// 同语义）.
func (s *Service) State(ctx context.Context, callerAlias, sessionID string, at time.Time) (*StateResult, error) {
	rt, err := s.loadRuntime(ctx, callerAlias, sessionID)
	if err != nil {
		return nil, err
	}
	return stateResult(rt, s.resolveAt(at)), nil
}

// NextItemResult 是取题结果（openapi-v1.1 NextItemResponse 域：done=true 时
// 其余字段零值；否则携带 A4 追溯锚——题目渲染载荷属内容域，不在会话域）.
type NextItemResult struct {
	Done           bool
	ItemVersionID  string
	PlacementToken *string
}

// GetNext 取下一题（Python get_next_item 的 Go 重锚定）：
//
//	完成 → done=true；放弃 → ErrSessionState；时长保护超限 → 置位
//	rest_prompted + ErrRestRequired；主序列逐题推进。主序列走完且开回测
//	→ ErrRetestRoundUnavailable（回测轮迁移归 W6）；走完未开回测的会话
//	由提交面在末题提交时原子完结（见 submit 推进语句），此处按 done 报告.
func (s *Service) GetNext(ctx context.Context, callerAlias, sessionID string, at time.Time) (*NextItemResult, error) {
	now := s.resolveAt(at)
	var out *NextItemResult
	terr := s.d.Runner.InTx(ctx, func(q Executor) error {
		rt, err := s.loadRuntimeTx(ctx, callerAlias, sessionID, q)
		if err != nil {
			return err
		}
		switch rt.Status {
		case StatusCompleted:
			out = &NextItemResult{Done: true}
			return nil
		case StatusAbandoned:
			return fmt.Errorf("%w: 会话已放弃，不能取题", ErrSessionState)
		}
		// 时长保护：取题与提交都查——保护的是「连续作答时长」本身
		// （Python _check_time_protection 同语义）；超限置位 rest_prompted
		// 并给休息提示，rest_prompted 未 resume 时 elapsed 仍超限、继续拒绝.
		if elapsed := elapsedSince(rt.LastResumeAt, now); elapsed > rt.TimeLimitSec {
			if merr := s.d.Accounts.MarkRestPrompted(ctx, q, sessionID); merr != nil {
				return merr
			}
			return &RestRequiredError{
				ElapsedSec:   elapsed,
				TimeLimitSec: rt.TimeLimitSec,
				message:      restPromptMessage(rt.TimeLimitSec),
			}
		}
		if rt.CurrentIndex >= len(rt.Entries) {
			if rt.RetestWrong {
				return ErrRetestRoundUnavailable
			}
			// 走完未开回测：提交面本应已完结；防御性按完成报告
			// （不在读路径补写终态——终态迁移是提交面的职责）.
			out = &NextItemResult{Done: true}
			return nil
		}
		entry := rt.Entries[rt.CurrentIndex]
		out = &NextItemResult{ItemVersionID: entry.ItemVersionID, PlacementToken: entry.PlacementToken}
		return nil
	})
	if terr != nil {
		return nil, terr
	}
	return out, nil
}

// SubmitResult 是作答提交的返回投影（openapi-v1.1 Feedback 域：对错/维度分
// 来自评分轨迹契约 §3 的落账形态，进度与会话状态来自提交后的运行态投影；
// error_feedback/explanation 属内容域装配面，本域不伪造、由协议层缺省）.
type SubmitResult struct {
	EventID         string
	Duplicate       bool
	Correct         bool
	DimensionScores map[string]float64
	ErrorInferences []map[string]any
	Progress        ProgressResult
	SessionStatus   string
}

// ProgressResult 是进度投影（Feedback.progress：additionalProperties 均整数）.
type ProgressResult struct {
	Total         int
	MainAnswered  int
	AnsweredCount int
	CorrectCount  int
}

// Submit 提交作答：归属断言（D9）→ 显式事务面内的提交临界区（幂等判定 →
// 状态/时长/题序校验 → 事件入账 → 幂等登记 → 推进）→ 提交后运行态投影.
func (s *Service) Submit(ctx context.Context, callerAlias string, in SubmitInput) (*SubmitResult, error) {
	if _, err := s.loadRuntime(ctx, callerAlias, in.SessionID); err != nil {
		return nil, err
	}
	var out *SubmitResult
	terr := s.d.Runner.InTx(ctx, func(q Executor) error {
		eventID, duplicate, err := s.d.Submissions.SubmitAnswer(ctx, q, in)
		if err != nil {
			return err
		}
		post, err := s.d.Accounts.RuntimeState(ctx, q, in.SessionID)
		if err != nil {
			return err
		}
		out = &SubmitResult{
			EventID:         eventID,
			Duplicate:       duplicate,
			Correct:         traceCorrectExplicit(in.ScoringTrace),
			DimensionScores: traceDimensionScores(in.ScoringTrace),
			ErrorInferences: nonNilInferences(in.ErrorInferences),
			Progress: ProgressResult{
				Total:         len(post.Entries),
				MainAnswered:  post.CurrentIndex,
				AnsweredCount: post.AnsweredCount,
				CorrectCount:  post.CorrectCount,
			},
			SessionStatus: post.Status,
		}
		return nil
	})
	if terr != nil {
		return nil, terr
	}
	return out, nil
}

// Resume 休息确认（Python resume_session 同语义）：rest_prompted/active →
// active，计时锚点重置；返回迁移后状态.
func (s *Service) Resume(ctx context.Context, callerAlias, sessionID string, at time.Time) (*StateResult, error) {
	if _, err := s.loadRuntime(ctx, callerAlias, sessionID); err != nil {
		return nil, err
	}
	var rt *SessionRuntime
	terr := s.d.Runner.InTx(ctx, func(q Executor) error {
		var err error
		rt, err = s.d.Accounts.Resume(ctx, q, sessionID, at)
		return err
	})
	if terr != nil {
		return nil, terr
	}
	return stateResult(rt, s.resolveAt(at)), nil
}

// Abandon 放弃会话（Python abandon_session 同语义）：已作答事件保留在账
// （append-only 零删除）；返回放弃后状态.
func (s *Service) Abandon(ctx context.Context, callerAlias, sessionID string, at time.Time) (*StateResult, error) {
	if _, err := s.loadRuntime(ctx, callerAlias, sessionID); err != nil {
		return nil, err
	}
	var rt *SessionRuntime
	terr := s.d.Runner.InTx(ctx, func(q Executor) error {
		var err error
		rt, err = s.d.Accounts.Abandon(ctx, q, sessionID, at)
		return err
	})
	if terr != nil {
		return nil, terr
	}
	return stateResult(rt, s.resolveAt(at)), nil
}

// loadRuntime 归属断言 + 运行态读取（非事务读面；归属锚列不可变，预读
// 无 TOCTOU 可改写面——事务内的权威判定仍在各账本临界区）.
func (s *Service) loadRuntime(ctx context.Context, callerAlias, sessionID string) (*SessionRuntime, error) {
	rt, err := s.d.Accounts.RuntimeState(ctx, s.d.Reader, sessionID)
	if err != nil {
		return nil, err
	}
	if rt.StudentAliasID != callerAlias {
		return nil, fmt.Errorf("%w: alias=%s", ErrNotSessionOwner, callerAlias)
	}
	return rt, nil
}

// loadRuntimeTx 事务面版本的 loadRuntime（供 GetNext 等读写同事务的动词）.
func (s *Service) loadRuntimeTx(ctx context.Context, callerAlias, sessionID string, q Executor) (*SessionRuntime, error) {
	rt, err := s.d.Accounts.RuntimeState(ctx, q, sessionID)
	if err != nil {
		return nil, err
	}
	if rt.StudentAliasID != callerAlias {
		return nil, fmt.Errorf("%w: alias=%s", ErrNotSessionOwner, callerAlias)
	}
	return rt, nil
}

// requireOnlinePracticeConsentExec 是家长授权门的执行器可注入形态
// （consent.go 的 RequireOnlinePracticeConsent 与本函数共享同一判定口径，
// 见 consent.go 的三态分型说明）.
func requireOnlinePracticeConsentExec(ctx context.Context, store compliance.ConsentStore, q Executor, studentAliasID string) error {
	if store == nil {
		return errors.New("session: 家长授权账未装配（装配错误，在线入口 fail-closed）")
	}
	status, err := store.CheckConsent(ctx, q, studentAliasID, compliance.PurposeOnlinePractice, nil)
	if err != nil {
		return fmt.Errorf("session: 授权账读取失败（fail-closed，不放行）: %w", err)
	}
	return compliance.RequireGranted(status, err)
}

// resolveAt 归一可选时刻（零值回落服务时钟）.
func (s *Service) resolveAt(at time.Time) time.Time {
	if at.IsZero() {
		return s.d.Now()
	}
	return at
}

// stateResult 从运行态投影装配状态结果（时长判定字段在此定型：协议层零
// 业务计算；Python _build_state 同构）.
func stateResult(rt *SessionRuntime, now time.Time) *StateResult {
	elapsed := elapsedSince(rt.LastResumeAt, now)
	out := &StateResult{
		SessionID:        rt.SessionID,
		Status:           rt.Status,
		Scene:            rt.Scene,
		Gradeband:        rt.Gradeband,
		PaperID:          rt.PaperID,
		Total:            len(rt.Entries),
		MainAnswered:     rt.CurrentIndex,
		AnsweredCount:    rt.AnsweredCount,
		CorrectCount:     rt.CorrectCount,
		WrongCount:       len(rt.WrongMarks),
		RetestPending:    retestPendingCount(rt.WrongMarks),
		ElapsedActiveSec: elapsed,
		TimeLimitSec:     rt.TimeLimitSec,
		RemainingSec:     rt.TimeLimitSec - elapsed,
		StartedAt:        rt.StartedAt,
		CompletedAt:      rt.CompletedAt,
	}
	return out
}

// elapsedSince 距锚点的连续秒数（负值截断为 0——时钟回拨不产生负时长）.
func elapsedSince(anchor, now time.Time) int {
	elapsed := int(now.Sub(anchor).Seconds())
	if elapsed < 0 {
		return 0
	}
	return elapsed
}

// retestPendingCount 统计待回测错题数（mark.retest_status == "pending"）.
func retestPendingCount(marks []map[string]any) int {
	n := 0
	for _, m := range marks {
		if st, _ := m["retest_status"].(string); st == "pending" {
			n++
		}
	}
	return n
}

// traceCorrectExplicit 是 traceCorrect 的单值形态（协议投影只关心判定值；
// 非契约 §3 形态的轨迹按未判对处理——不猜对错）.
func traceCorrectExplicit(trace map[string]any) bool {
	explicit, correct := traceCorrect(trace)
	return explicit && correct
}

// traceDimensionScores 从评分轨迹提取维度分（契约 §3 trace.dimension_scores；
// 数值化失败的键跳过——协议投影是 number 域，不伪造 0 分）.
func traceDimensionScores(trace map[string]any) map[string]float64 {
	out := map[string]float64{}
	raw, ok := trace["dimension_scores"].(map[string]any)
	if !ok {
		return out
	}
	for k, v := range raw {
		if f, ok := v.(float64); ok {
			out[k] = f
		}
	}
	return out
}

// nonNilInferences 归一推断数组（nil → 空切片——契约 error_inferences 为
// required 数组，JSON null 不是合法响应形态）.
func nonNilInferences(in []map[string]any) []map[string]any {
	if in == nil {
		return []map[string]any{}
	}
	return in
}
