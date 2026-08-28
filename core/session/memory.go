package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/events"
	"github.com/jackc/pgx/v5/pgtype"
)

// 本文件是 SubmissionStore 的进程内实现面（方法挂在共享 MemoryStore 上，
// 类型声明见 topicorder_memory.go——T-W5-004 题序固化面与本提交面共用同一个
// 进程内存储与同一把互斥锁）：互斥锁临界区完整承载「幂等判定 → 状态/时长/
// 题序校验 → 事件入账 → 幂等登记 → 会话推进」的原子语义，供单测（go test
// -race 并发测试）与单体进程内嵌使用；PG 生产实现在 W6 服务化时接线.
//
// 为什么内存实现也要严格模拟 PG 的三层并发保证（与 core/estimator/core/
// compliance 同款论证）：本卡验收的并发正确性必须在不依赖真实 PG 的前提下
// 可验证——互斥锁即 per-session advisory xact lock 的内存等效物（题序固化
// 与作答提交同锁串行化，正是「会话域单写者」的内存投影）；幂等键唯一性由
// submissions map 的键结构模拟（= 0031 的 pk_response_submission 复合主键）；
// 事件账由 ledger 切片模拟（= response_event 分区账的结构投影）.
//
// 本 face 专属字段（now/sessions/submissions/ledger）的结构声明见
// topicorder_memory.go 的 MemoryStore，仅由本文件的提交方法读写.

// submissionKey 是幂等键三元组（与迁移 0031 的 pk_response_submission 同构）.
type submissionKey struct {
	sessionID     string
	itemVersionID string
	digest        string
}

// submissionRecord 是幂等登记账一行：首次提交的事件指针（event_id +
// event_created_at 复合回指 response_event 主键，0003 实现注记）.
type submissionRecord struct {
	EventID        string
	EventCreatedAt time.Time
}

// EventRecord 是 response_event 一行的输出面投影（字段与 core/events.Input
// 同构；对外只交深拷贝，调用方改不动内部账）.
type EventRecord struct {
	EventID         string
	StudentAliasID  string
	ItemVersionID   string
	Scene           events.Scene
	RawPayload      map[string]any
	DurationMs      *int32
	ScoringTrace    map[string]any
	ErrorInferences []map[string]any
	SessionID       string
	CreatedAt       time.Time
}

// memorySession 是 practice_session 一行的内存运行态（0011 列的判定投影 +
// 推进面；身份字段 seed 后不可变，运行态字段只在临界区内变更）.
type memorySession struct {
	sessionID      string
	studentAliasID string
	scene          string
	status         string
	sequence       []string
	retestWrong    bool
	currentIndex   int
	timeLimitSec   int
	lastResumeAt   time.Time
	lastActivityAt time.Time
	answeredCount  int
	completedAt    *time.Time
}

// SeedInput 是内存会话的开立请求（测试/单体进程装配面；生产 PG 路径的会话
// 开立属 start_session 服务域，不在本卡范围——非目标：会话状态机重构）.
type SeedInput struct {
	SessionID      string
	StudentAliasID string
	// Scene 场景（practice/diagnosis 在线二值，0011 CHECK；落事件 scene 列）.
	Scene string
	// Sequence 主序列 item_version_id（题序不可变，004 冻结面）；空序列拒绝.
	Sequence []string
	// TimeLimitSec 时长保护阈值（秒）；零值拒绝（0011 CHECK time_limit_sec>0）.
	TimeLimitSec int32
	// Status 初始状态；零值回落 active.
	Status string
	// RetestWrong 回测开关；true 时序列走完不自动完结（回测轮未在本卡范围，
	// 与 PG 推进语句的完结条件严格同构——避免内存/PG 终态漂移）.
	RetestWrong bool
	// CurrentIndex 初始进度（默认 0）.
	CurrentIndex int
	// LastResumeAt 时长保护计时起点；零值回落开立时刻.
	LastResumeAt time.Time
}

// SetClock 是测试注入点（生产留零值）：固定时钟让事件 created_at 与时长
// 保护判定确定（与 core/scoring.Runner.SetClock 同一惯例）；零值时钟回落
// time.Now（见 SubmitAnswer 的 nowFn 归一）.
func (m *MemoryStore) SetClock(now func() time.Time) { m.now = now }

// nowFn 归一时钟来源：共享构造器已初始化 time.Now，测试可经 SetClock 注入；
// 零值安全回落（对未经理构造器装配的零值 MemoryStore 也成立）.
func (m *MemoryStore) nowFn() func() time.Time {
	if m.now != nil {
		return m.now
	}
	return time.Now
}

// SeedSession 在内存账开立一个会话（幂等键判定、题序推进全以它为前提）.
// 非法输入显式拒绝：身份非 UUID、场景越域、空序列、非正时长、进度越界.
func (m *MemoryStore) SeedSession(in SeedInput) error {
	var sid, alias pgtype.UUID
	if err := sid.Scan(in.SessionID); err != nil || !sid.Valid {
		return fmt.Errorf("%w: seed session_id=%q 不是合法 UUID", ErrInvalidSubmission, in.SessionID)
	}
	if err := alias.Scan(in.StudentAliasID); err != nil || !alias.Valid {
		return fmt.Errorf("%w: seed student_alias_id=%q 不是合法 UUID", ErrInvalidSubmission, in.StudentAliasID)
	}
	if !events.ValidScene(events.Scene(in.Scene)) {
		return fmt.Errorf("%w: seed scene=%q 不在场景域（D5）", ErrInvalidSubmission, in.Scene)
	}
	// 在线会话场景二值域（0011 ck_practice_session_scene_domain）：与 PG CHECK
	// 同构——measurement 无在线会话入口，内存账同样拒绝（实现间零漂移面）.
	if in.Scene != ScenePractice && in.Scene != SceneDiagnosis {
		return fmt.Errorf("%w: seed scene=%q 不在在线会话二值域 practice/diagnosis（0011 CHECK）", ErrInvalidSubmission, in.Scene)
	}
	if len(in.Sequence) == 0 {
		return fmt.Errorf("%w: seed 主序列不能为空（会话以题为纲）", ErrInvalidSubmission)
	}
	for i, id := range in.Sequence {
		if id == "" {
			return fmt.Errorf("%w: seed 序列第 %d 项缺 item_version_id", ErrLedgerCorrupted, i)
		}
	}
	if in.TimeLimitSec <= 0 {
		return fmt.Errorf("%w: seed time_limit_sec=%d 必须为正（0011 CHECK）", ErrInvalidSubmission, in.TimeLimitSec)
	}
	if in.CurrentIndex < 0 || in.CurrentIndex > len(in.Sequence) {
		return fmt.Errorf("%w: seed current_index=%d 越界（序列长 %d）", ErrInvalidSubmission, in.CurrentIndex, len(in.Sequence))
	}
	status := in.Status
	if status == "" {
		status = StatusActive
	}
	resume := in.LastResumeAt
	if resume.IsZero() {
		resume = m.nowFn()()
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.initSubmitState()
	if _, exists := m.sessions[in.SessionID]; exists {
		return fmt.Errorf("session: 内存会话 %q 重复开立（装配错误）", in.SessionID)
	}
	m.sessions[in.SessionID] = &memorySession{
		sessionID:      in.SessionID,
		studentAliasID: in.StudentAliasID,
		scene:          in.Scene,
		status:         status,
		sequence:       append([]string(nil), in.Sequence...),
		retestWrong:    in.RetestWrong,
		currentIndex:   in.CurrentIndex,
		timeLimitSec:   int(in.TimeLimitSec),
		lastResumeAt:   resume,
		lastActivityAt: resume,
	}
	return nil
}

// SubmitAnswer 实现 SubmissionStore：整个提交序列在单一互斥锁临界区内完成，
// 并发调用被串行化——同一 (session, item, 指纹) 的并发提交恰一次越过幂等键
// 位（map 键结构=0031 复合主键的内存等效），其余全部幂等命中。q 为内存实现
// 未使用的事务执行面（契约见 Executor），传 nil 即可.
func (m *MemoryStore) SubmitAnswer(_ context.Context, _ Executor, in SubmitInput) (string, bool, error) {
	p, err := prepareSubmit(in, m.nowFn())
	if err != nil {
		return "", false, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.initSubmitState()

	// 1) 幂等判定先行：命中即返回首次结果、零副作用——先于状态/题序校验，
	// 已完成会话上的迟到重试依然是幂等成功而非报错（幂等语义全时态成立）.
	key := submissionKey{sessionID: p.rawSessionID, itemVersionID: p.itemVersionID, digest: p.digest}
	if rec, ok := m.submissions[key]; ok {
		return rec.EventID, true, nil
	}
	// 2) 会话行：内存实现无独立行锁——互斥锁即整个临界区（advisory+行锁的
	// 双锁分层在单进程内由同一把锁承担）.
	s, ok := m.sessions[p.rawSessionID]
	if !ok {
		return "", false, fmt.Errorf("%w: %q", ErrSessionNotFound, p.rawSessionID)
	}
	// 3) 状态/时长/题序校验（共享纯函数核，与 PG 严格同判）.
	if _, err := validateSubmitAgainstSession(s.view(), p); err != nil {
		var rre *RestRequiredError
		if errors.As(err, &rre) {
			s.status = StatusRestPrompted // 时长保护置位（零事件写入，Python 同语义）
		}
		return "", false, err
	}
	// 4) 事件入账 → 幂等登记 → 会话推进：三写同处一个临界区（= 一个外层
	// 事务的内存等效），任一步失败整体未发生.
	eventID, err := newEventID()
	if err != nil {
		return "", false, err
	}
	m.ledger = append(m.ledger, newEventRecord(p, s, eventID))
	m.submissions[key] = submissionRecord{EventID: eventID, EventCreatedAt: p.at}
	s.currentIndex++
	s.answeredCount++
	s.lastActivityAt = p.at
	// 完结判定（Python 同构：主序列走完且未开启回测；回测轮归 W6 状态机域，
	// 开启回测的会话维持 active——与 PG 推进语句的 CASE 条件严格同构）.
	if s.currentIndex >= len(s.sequence) && !s.retestWrong {
		completed := p.at
		s.status = StatusCompleted
		s.completedAt = &completed
	}
	return eventID, false, nil
}

// SessionSnapshot 是会话运行态的只读投影（深拷贝；断言与进度展示面）.
type SessionSnapshot struct {
	SessionID      string
	StudentAliasID string
	Scene          string
	Status         string
	CurrentIndex   int
	AnsweredCount  int
	TimeLimitSec   int
	LastResumeAt   time.Time
	LastActivityAt time.Time
	CompletedAt    *time.Time
}

// SessionSnapshot 返回会话当前运行态快照；会话不存在报 ErrSessionNotFound.
func (m *MemoryStore) SessionSnapshot(sessionID string) (*SessionSnapshot, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[sessionID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrSessionNotFound, sessionID)
	}
	out := &SessionSnapshot{
		SessionID:      s.sessionID,
		StudentAliasID: s.studentAliasID,
		Scene:          s.scene,
		Status:         s.status,
		CurrentIndex:   s.currentIndex,
		AnsweredCount:  s.answeredCount,
		TimeLimitSec:   s.timeLimitSec,
		LastResumeAt:   s.lastResumeAt,
		LastActivityAt: s.lastActivityAt,
	}
	if s.completedAt != nil {
		t := *s.completedAt
		out.CompletedAt = &t
	}
	return out, nil
}

// Events 返回事件账的深拷贝投影（append-only 只读面：测试断言恰一条入账、
// 调用方改不动历史）.
func (m *MemoryStore) Events() []EventRecord {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]EventRecord, len(m.ledger))
	for i := range m.ledger {
		out[i] = cloneEventRecord(m.ledger[i])
	}
	return out
}

// initSubmitState 惰性初始化提交面专属字段（在 m.mu 临界区内调用）：共享
// 构造器已初始化，这里兜底零值装配的 MemoryStore——nil map 写入会 panic.
func (m *MemoryStore) initSubmitState() {
	if m.sessions == nil {
		m.sessions = make(map[string]*memorySession)
	}
	if m.submissions == nil {
		m.submissions = make(map[submissionKey]submissionRecord)
	}
}

// view 装配共享校验视图（调用方持锁）.
func (s *memorySession) view() sessionView {
	return sessionView{
		Status:         s.status,
		Scene:          s.scene,
		StudentAliasID: s.studentAliasID,
		Sequence:       s.sequence,
		CurrentIndex:   s.currentIndex,
		TimeLimitSec:   s.timeLimitSec,
		LastResumeAt:   s.lastResumeAt,
	}
}

// newEventRecord 装配事件账条目并独立拷贝引用型载荷：账本持有的 map 与调用方
// 输入彻底脱钩（调用方提交后再改 payload 不得污染 append-only 账——-race
// 干净的结构前提）.
func newEventRecord(p *preparedSubmit, s *memorySession, eventID string) EventRecord {
	return EventRecord{
		EventID:         eventID,
		StudentAliasID:  s.studentAliasID,
		ItemVersionID:   p.itemVersionID,
		Scene:           events.Scene(s.scene),
		RawPayload:      cloneJSONMap(p.response),
		DurationMs:      cloneDuration(p.duration),
		ScoringTrace:    cloneJSONMap(p.trace),
		ErrorInferences: cloneInferences(p.inferences),
		SessionID:       s.sessionID,
		CreatedAt:       p.at,
	}
}

// cloneEventRecord 输出面再拷贝一层：Events() 交出的记录与内部账互不共享.
func cloneEventRecord(r EventRecord) EventRecord {
	out := r
	out.RawPayload = cloneJSONMap(r.RawPayload)
	out.ScoringTrace = cloneJSONMap(r.ScoringTrace)
	out.ErrorInferences = cloneInferences(r.ErrorInferences)
	out.DurationMs = cloneDuration(r.DurationMs)
	return out
}

// cloneJSONMap 经 JSON 往返深拷贝（载荷已过指纹的规范化校验，往返必成；
// 失败即内部管线破坏，panic 显式暴露而非带病入账）.
func cloneJSONMap(v map[string]any) map[string]any {
	if v == nil {
		return nil
	}
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("session: 账面载荷拷贝失败（内部管线破坏）: %v", err))
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		panic(fmt.Sprintf("session: 账面载荷回读失败（内部管线破坏）: %v", err))
	}
	return out
}

// cloneInferences 深拷贝推断数组（nil 语义保持——nil=未提供，非空集）.
func cloneInferences(in []map[string]any) []map[string]any {
	if in == nil {
		return nil
	}
	out := make([]map[string]any, len(in))
	for i := range in {
		out[i] = cloneJSONMap(in[i])
	}
	return out
}

// cloneDuration 拷贝可空耗时（输出面交独立指针）.
func cloneDuration(d *int32) *int32 {
	if d == nil {
		return nil
	}
	c := *d
	return &c
}

// newEventID 生成事件唯一 id（UUIDv4 标准连字符形）：事件 id 全局唯一性由
// 应用侧生成保证（契约 response_event.md §2 实现注记——分区表 PK 含分区键，
// event_id 无 DB 侧唯一约束可依）；发号复用包内 randomUUIDV4（熵源不可用时
// 宁可不发号也不发可重复 ID）.
func newEventID() (string, error) {
	b, err := randomUUIDV4()
	if err != nil {
		return "", fmt.Errorf("session: 熵源不可用无法生成 event_id: %w", err)
	}
	return formatUUID(b), nil
}
