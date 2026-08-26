package events

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// Scene 作答场景三值域（D5：分场景独立统计禁止混估）。写入时即定型为下游
// 估计器的取数键，与迁移 0003 的 response_event_scene_enum 物理约束同值.
type Scene string

// 场景三值（契约 §1 scene 枚举）.
const (
	ScenePractice    Scene = "practice"
	SceneDiagnosis   Scene = "diagnosis"
	SceneMeasurement Scene = "measurement"
)

// scenes 固定展示顺序的三值域（越域报错信息用）.
var scenes = []Scene{ScenePractice, SceneDiagnosis, SceneMeasurement}

// ValidScene 报告 s 是否在 D5 三值域内.
func ValidScene(s Scene) bool {
	for _, v := range scenes {
		if s == v {
			return true
		}
	}
	return false
}

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoTransaction 表示写调用没有显式事务执行面。D11 fail-closed：
	// 事件写入只接受外层已 begin 的事务，绝不在无事务面上「先写先得」.
	ErrNoTransaction = errors.New("events: 无显式事务执行面（D11 fail-closed：作答事件只接受外层已 begin 的事务写入）")

	// ErrInvalidEvent 表示入参违反契约 response_event.md §1/§5，细分原因见
	// wrap 文本. 契约违例在出 Go 进程前拦截，不烧事务语句、不给 PG 报错晚到.
	ErrInvalidEvent = errors.New("events: 作答事件输入违反契约 response_event.md")
)

// Executor 是事件写入所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX 同构
// （与本仓 core/estimator 的同名接口同形）。
//
// 为什么不复用 estimator 的接口而本地重声明：领域端口按需各自声明最小依赖面，
// 六边形核心域之间不为一个三方法接口建立编译耦合；两者方法集一致，pgx.Tx 与
// 连接池事务面天然同时满足。全部语句文本只住在 db/queries/events.sql
// （SQL-2：不在 Go 拼 SQL），经 sqlc 生成为类型安全的 dbgen 方法，本包仅作
// 调用方；因此本包源码不可能发出 UPDATE/DELETE——append-only 无查询面可写.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// 编译期锚定一：pgx.Tx 必须满足 Executor（W6 装配直通的假设防线）.
var _ Executor = (pgx.Tx)(nil)

// 编译期锚定二：Executor 必须满足生成层执行面 dbgen.DBTX——WithTx 内部用
// dbgen.New(tx) 构造类型安全查询器；sqlc 升级改形状时在此第一时间红.
var _ dbgen.DBTX = Executor(nil)

// Writer 是绑定显式事务的作答事件 append-only 写入服务（Python 冻结实现
// src/core/events/writer.py record_event 的 Go 重锚定：同样的 INSERT-only
// 语义，但移除其内部 commit——那是 T-W5-017 要归位的边界缺陷）.
//
// 事务纪律（S4/D11）：Writer 不持有连接、不自 begin、永不 Commit/Rollback；
// 提交/回滚由最外层调用方统一持有，作答事件与会话状态因此在同一事务里同进同退.
type Writer struct {
	tx Executor // 外层已 begin 的执行面；nil 即非事务上下文（fail-closed 只读拒绝）
	qs *dbgen.Queries
}

// WithTx 把调用方已 begin 的显式事务执行面绑定为事件写入器（一次业务事务内
// 多条事件免重复传参）。tx 允许 nil——构造不报错，但所有写调用立即返回
// ErrNoTransaction：「无显式事务面的写调用直接被拒」是验收 #1 的字面语义，
// fail-closed 落在写路径而非构造路径，也免去调用方的判空分叉.
func WithTx(tx Executor) *Writer { return &Writer{tx: tx, qs: dbgen.New(tx)} }

// Input 是一条作答事件的契约字段集，十三列与 response_event.md §1 一一对应；
// 可空性以指针/容器 nil 表达契约 NULL（如 duration_ms nil=未知，禁止填 0 冒充）.
type Input struct {
	// EventID 事件唯一 id，应用层生成（分区表 PK 为 (event_id, created_at)，
	// 全局唯一性由应用侧保证——契约 §2 实现注记）.
	EventID string
	// StudentAliasID 匿名学生 id（D7：直接标识只在 PII 保险库 schema，本表只存 alias）.
	StudentAliasID string
	// ItemVersionID 作答题目版本（A/B 级实例=内容寻址哈希，D3；§5 minLength=1）.
	ItemVersionID string
	// Scene 场景三值之一（D5 禁止混估）.
	Scene Scene
	// RawPayload 原始作答载荷（作答内容本身非仅对错，R-D-01）；JSON object 必填.
	RawPayload map[string]any
	// DurationMs 作答耗时毫秒；nil=NULL=未知（纸卷回录 S2 无真实耗时），≥0.
	DurationMs *int32
	// ScoringTrace 评分轨迹（契约 §3 结构）；JSON object 必填.
	ScoringTrace map[string]any
	// ErrorInferences 错误推断数组（契约 §4）；nil 与空数组同义记空集（§1「可为空数组」）.
	ErrorInferences []map[string]any
	// TestletID 题组 id（R-Z-06 题组内相关性统计）；nil=NULL 合法.
	TestletID *string
	// SessionID 作答会话 id；nil=NULL=无会话（纸卷录入场景勿伪造会话；S2 批次标识放 SourceRef）.
	SessionID *string
	// AudioPlayEvents 音频播放行为（音频题必填性由上层交互类型保证）；nil=NULL 合法.
	AudioPlayEvents []map[string]any
	// SourceRef 来源追溯 {paper_id, placement_token} / {assembly_run_id}（A4 入水口）；nil=NULL 合法.
	SourceRef map[string]any
	// CreatedAt 事件时间戳 UTC，分区键必填（零值即契约必填项违例，前置拒绝——
	// 不让 INSERT 在 PG 找不到分区才失败）.
	CreatedAt time.Time
}

// Record 把一条作答事件 append 进响应事件账，返回 event_id（与入参一致，
// 便于调用方链式引用——Python 冻结实现同义）.
//
// 预期失败面：无显式事务面 → ErrNoTransaction；契约违例 → ErrInvalidEvent；
// 驱动/约束错误原样 wrap 放行（append-only 触发器拒绝的 SQLSTATE 证据不吞）.
func (w *Writer) Record(ctx context.Context, in Input) (string, error) {
	if w == nil || w.tx == nil {
		return "", ErrNoTransaction
	}
	arg, err := in.params()
	if err != nil {
		return "", err
	}
	if err := w.qs.InsertResponseEvent(ctx, arg); err != nil {
		return "", fmt.Errorf("events: insert response_event: %w", err)
	}
	return in.EventID, nil
}

// params 校验入参并映射为生成层的类型安全参数（契约逐项对照前置到进程内）.
func (in Input) params() (dbgen.InsertResponseEventParams, error) {
	var arg dbgen.InsertResponseEventParams

	if !ValidScene(in.Scene) {
		return arg, fmt.Errorf("%w: scene %q 不在 practice/diagnosis/measurement 三值域内（D5）", ErrInvalidEvent, string(in.Scene))
	}
	if in.ItemVersionID == "" {
		return arg, fmt.Errorf("%w: item_version_id 不能为空（§5 minLength=1）", ErrInvalidEvent)
	}
	if in.RawPayload == nil {
		return arg, fmt.Errorf("%w: raw_payload 必填（JSON object，R-D-01）", ErrInvalidEvent)
	}
	if in.ScoringTrace == nil {
		return arg, fmt.Errorf("%w: scoring_trace 必填（JSON object，§3）", ErrInvalidEvent)
	}
	if in.CreatedAt.IsZero() {
		return arg, fmt.Errorf("%w: created_at 必填（分区键）", ErrInvalidEvent)
	}
	if in.DurationMs != nil && *in.DurationMs < 0 {
		return arg, fmt.Errorf("%w: duration_ms=%d 负数非法（§5 minimum=0）", ErrInvalidEvent, *in.DurationMs)
	}

	eventID, err := uuidArg("event_id", in.EventID)
	if err != nil {
		return arg, err
	}
	aliasID, err := uuidArg("student_alias_id", in.StudentAliasID)
	if err != nil {
		return arg, err
	}
	sessionID := pgtype.UUID{}
	if in.SessionID != nil {
		sessionID, err = uuidArg("session_id", *in.SessionID)
		if err != nil {
			return arg, err
		}
	}

	rawPayload, err := jsonb("raw_payload", in.RawPayload)
	if err != nil {
		return arg, err
	}
	scoringTrace, err := jsonb("scoring_trace", in.ScoringTrace)
	if err != nil {
		return arg, err
	}
	inferences := in.ErrorInferences
	if inferences == nil {
		inferences = []map[string]any{} // nil 记空数组而非 JSON null（§4「可为空数组」）
	}
	errorInferences, err := jsonb("error_inferences", inferences)
	if err != nil {
		return arg, err
	}
	// 可空的 jsonb：nil 即 SQL NULL（列本身可空，语义=无该要素而非 json 'null'）.
	var audioPlayEvents []byte
	if in.AudioPlayEvents != nil {
		audioPlayEvents, err = jsonb("audio_play_events", in.AudioPlayEvents)
		if err != nil {
			return arg, err
		}
	}
	var sourceRef []byte
	if in.SourceRef != nil {
		sourceRef, err = jsonb("source_ref", in.SourceRef)
		if err != nil {
			return arg, err
		}
	}

	return dbgen.InsertResponseEventParams{
		EventID:         eventID,
		StudentAliasID:  aliasID,
		ItemVersionID:   in.ItemVersionID,
		Scene:           genResponseScene(in.Scene),
		RawPayload:      rawPayload,
		DurationMs:      int4Arg(in.DurationMs),
		ScoringTrace:    scoringTrace,
		ErrorInferences: errorInferences,
		TestletID:       textArg(in.TestletID),
		SessionID:       sessionID,
		AudioPlayEvents: audioPlayEvents,
		SourceRef:       sourceRef,
		CreatedAt:       pgtype.Timestamptz{Time: in.CreatedAt, Valid: true},
	}, nil
}

// uuidArg 解析并锚定 uuid 形参：应用侧字符串入账前先过格式校验——契约违例在
// 本地失败远比 PG 错误晚到清晰.
func uuidArg(field, s string) (pgtype.UUID, error) {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil {
		return pgtype.UUID{}, fmt.Errorf("%w: %s=%q 不是合法 UUID", ErrInvalidEvent, field, s)
	}
	return u, nil
}

// textArg 可空文本 → pgtype.Text（nil=NULL）.
func textArg(s *string) pgtype.Text {
	if s == nil {
		return pgtype.Text{}
	}
	return pgtype.Text{String: *s, Valid: true}
}

// int4Arg 可空毫秒数 → pgtype.Int4（nil=NULL=未知；负值已在 params 拦截）.
func int4Arg(n *int32) pgtype.Int4 {
	if n == nil {
		return pgtype.Int4{}
	}
	return pgtype.Int4{Int32: *n, Valid: true}
}

// genResponseScene 把域枚举转为生成层同值枚举（sqlc 对 enum 列生成的 string 别名；
// 三值域两侧一致性由迁移 0003 的物理 enum 约束兜底）.
func genResponseScene(s Scene) dbgen.ResponseEventSceneEnum {
	return dbgen.ResponseEventSceneEnum(string(s))
}

// jsonb 序列化 JSONB 字段：SetEscapeHTML(false) 与 Python ensure_ascii=False
// 同向——Unicode/HTML 字符按原文落库，便于人工审账时直读.
func jsonb(field string, v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, fmt.Errorf("%w: %s JSON 序列化失败: %w", ErrInvalidEvent, field, err)
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}
