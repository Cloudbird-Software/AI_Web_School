// replay.go 承载增量重判/年度全量重放的纯聚合核与 D6 可重放原语（T-W4-003；
// Python 冻结实现 src/core/data/replay.py + parquet_export.py 的 Go 重锚定）。
//
// 重判规则（specs/contracts/events/response_event.md §3）：新 scorer 上线时，
// 仅对指定范围写平行 score_run，原 response_event.scoring_trace 永不改动
// （D1 作答事件账只增不改；契约 §3 原文「原序列不动」）。
//
// 可重放性（D6 / 验收 §3）：同代码版本 + 同数据快照必同输出——本包不依赖
// 时间或随机源；摘要哈希（ComputeSummaryHash）= 输入快照 id + 事件指纹 +
// 重判结果摘要的 SHA256，序列化与冻结实现 json.dumps 逐字节对齐（固定哈希
// 断言见 replay_test.go，用冻结实现交叉验证）。
//
// IO 面（本波显式留白，如实声明）：
//   - 事件快照取数（response_event 按 item/scene 查询）——ReplayInput.Events
//     由调用方冻结注入；
//   - 「当前活跃估计器」解析（ActiveModelPointer）——ReplayInput.ScorerVersion
//     要求显式传入；
//   - 评分器调度（run_scorer + infer_option_errors）——Rescorer 接口；
//   - score_run 平行落账（append-only，幂等键 (event_id, event_created_at,
//     run_label)）——ScoreRunSink 接口，本波不实现；
//   - Parquet 归档（parquet_export.py，pyarrow 依赖：schema 对齐/原子写/
//     manifest 幂等）——ParquetExporter 接口，零新依赖约束下本波不实现；
//     仅交付纯命名/规范化/去重助手。
//
// 宪法 A5/X6：本包是核心域数据子模块，禁止 import 任何学科包/学段包。
package datastat

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"
)

// 默认输入快照标识前缀（对应冻结实现 _DEFAULT_SNAPSHOT_PREFIX）.
const defaultSnapshotPrefix = "snapshot"

// 重放面的错误.
var (
	// ErrScorerVersionRequired 表示未指定重判评分器版本（冻结实现从当前活跃
	// 估计器取 model_version——ActiveModelPointer 是 IO 面，本骨架要求显式传入）.
	ErrScorerVersionRequired = errors.New("datastat: 未指定重判评分器版本")
	// ErrTraceCorrectUnparsable 表示 scoring_trace 的 correct 存在但不可解析
	// 为数值（冻结实现 float() 抛异常穿透——fail-closed，不猜测）.
	ErrTraceCorrectUnparsable = errors.New("datastat: scoring_trace.correct 不可解析为数值")
)

// ReplayEvent 是重放输入快照中的单条事件（冻结实现从 response_event 取数的
// 行视图；快照由调用方冻结注入——D5：调用方保证已按场景过滤，本核不复检）.
type ReplayEvent struct {
	EventID       string
	ItemVersionID string
	Scene         string
	// OriginalTrace 原始 scoring_trace（取旧 correct / 旧 scorer_version，
	// 供新旧一致性比对与版本分布概览）.
	OriginalTrace map[string]any
}

// RescoreFingerprint 是单事件重判结果摘要（D6 可重放哈希的输入三元组）.
type RescoreFingerprint struct {
	EventID       string
	ScorerVersion string
	Correct       bool
}

// pyBool 按 Python bool() 语义求真值（JSONB 解码值域；对应冻结实现
// bool(proc["correct"])——非空字符串为真、0/0.0/空串/空容器为假）.
func pyBool(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case int:
		return x != 0
	case int64:
		return x != 0
	case json.Number:
		return pyNumberFloat(x) != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	default:
		return true
	}
}

// pyNumberFloat 将 json.Number 解析为数值（整数优先；不可解析返回 0——
// 调用方在数值语义路径上另有 fail-closed 校验）.
func pyNumberFloat(n json.Number) float64 {
	if i, err := strconv.ParseInt(n.String(), 10, 64); err == nil {
		return float64(i)
	}
	if f, err := strconv.ParseFloat(n.String(), 64); err == nil {
		return f
	}
	return 0
}

// SafeScorerVersionFromTrace 从原始事件 scoring_trace 取 scorer_version
// （兜底空串；对应冻结实现 replay._safe_scorer_version_from_trace）.
func SafeScorerVersionFromTrace(trace map[string]any) string {
	if trace == nil {
		return ""
	}
	if v, ok := trace["scorer_version"]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SafeCorrectFromTrace 从原始事件 scoring_trace 取 correct（process.correct
// 优先，其次 dimension_scores.correct ≥ 1.0；对应冻结实现
// replay._safe_correct_from_trace）。
// 返回 (nil, nil) = 无法判定（不计入一致性分母）；dimension_scores.correct
// 存在但不可解析为数值时返回错误（冻结实现 float() 异常穿透，fail-closed）.
func SafeCorrectFromTrace(trace map[string]any) (*bool, error) {
	if trace == nil {
		return nil, nil
	}
	if proc, ok := trace["process"].(map[string]any); ok {
		if v, ok := proc["correct"]; ok {
			b := pyBool(v)
			return &b, nil
		}
	}
	if dims, ok := trace["dimension_scores"].(map[string]any); ok {
		if v, ok := dims["correct"]; ok {
			switch x := v.(type) {
			case bool:
				b := x // float(True)=1.0 ≥ 1.0；float(False)=0.0 < 1.0
				return &b, nil
			case float64:
				b := x >= 1.0
				return &b, nil
			case int:
				b := float64(x) >= 1.0
				return &b, nil
			case int64:
				b := float64(x) >= 1.0
				return &b, nil
			case json.Number:
				b := pyNumberFloat(x) >= 1.0
				return &b, nil
			case string:
				f, err := strconv.ParseFloat(strings.TrimSpace(x), 64)
				if err != nil {
					return nil, fmt.Errorf("%w: %q", ErrTraceCorrectUnparsable, x)
				}
				b := f >= 1.0
				return &b, nil
			default:
				return nil, fmt.Errorf("%w: %T", ErrTraceCorrectUnparsable, v)
			}
		}
	}
	return nil, nil
}

// DefaultInputSnapshotID 构造默认输入快照标识（对应冻结实现
// _default_input_snapshot_id）："snapshot:<scope|all>:<count>"——count+scope
// 是输入数据的稳定摘要；不用时间戳（可重放性：同输入同标识）.
func DefaultInputSnapshotID(eventsCount int, purposeScope string) string {
	scope := purposeScope
	if scope == "" {
		scope = "all"
	}
	return fmt.Sprintf("%s:%s:%d", defaultSnapshotPrefix, scope, eventsCount)
}

// pyJSONString 按 Python json.dumps(ensure_ascii=False) 的字符串转义规则
// 序列化：仅转义 " \ 与控制字符（\b \t \n \f \r 短形式，其余 \u00xx），
// 非 ASCII 原样 UTF-8.
func pyJSONString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString("\\\"")
		case '\\':
			b.WriteString("\\\\")
		case '\b':
			b.WriteString("\\b")
		case '\f':
			b.WriteString("\\f")
		case '\n':
			b.WriteString("\\n")
		case '\r':
			b.WriteString("\\r")
		case '\t':
			b.WriteString("\\t")
		default:
			if r < 0x20 {
				b.WriteString(fmt.Sprintf("\\u%04x", r))
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// ComputeSummaryHash 计算重判摘要哈希（对应冻结实现 replay._compute_summary_hash，
// D6 可重放——同输入必同输出）：SHA256(输入快照 id + 事件指纹 + 重判结果摘要)。
// 事件指纹按 (event_id, item_version_id, scene) 升序；重判摘要按
// (event_id, scorer_version, correct) 升序（bool false < true）。序列化与
// Python json.dumps(ensure_ascii=False)（默认分隔符 ", "）逐字节对齐.
func ComputeSummaryHash(inputSnapshotID string, events []ReplayEvent, rescored []RescoreFingerprint) string {
	var b strings.Builder
	b.WriteString(inputSnapshotID)

	// 事件指纹：排序去序
	evSorted := append([]ReplayEvent(nil), events...)
	sort.Slice(evSorted, func(i, j int) bool {
		a, c := evSorted[i], evSorted[j]
		if a.EventID != c.EventID {
			return a.EventID < c.EventID
		}
		if a.ItemVersionID != c.ItemVersionID {
			return a.ItemVersionID < c.ItemVersionID
		}
		return a.Scene < c.Scene
	})
	b.WriteString("[")
	for i, e := range evSorted {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString("[" + pyJSONString(e.EventID) + ", " + pyJSONString(e.ItemVersionID) + ", " + pyJSONString(e.Scene) + "]")
	}
	b.WriteString("]")

	// 重判结果摘要：排序去序
	rsSorted := append([]RescoreFingerprint(nil), rescored...)
	sort.Slice(rsSorted, func(i, j int) bool {
		a, c := rsSorted[i], rsSorted[j]
		if a.EventID != c.EventID {
			return a.EventID < c.EventID
		}
		if a.ScorerVersion != c.ScorerVersion {
			return a.ScorerVersion < c.ScorerVersion
		}
		return !a.Correct && c.Correct // false < true
	})
	b.WriteString("[")
	for i, r := range rsSorted {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString("[" + pyJSONString(r.EventID) + ", " + pyJSONString(r.ScorerVersion) + ", " + strconv.FormatBool(r.Correct) + "]")
	}
	b.WriteString("]")

	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// CanonicalJSON 将值序列化为 canonical JSON 字符串（键序排序、紧凑分隔符、
// 非 ASCII 原样；对应冻结实现 parquet_export._canonical_json =
// json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))——
// Parquet 归档幂等内容哈希的基础）。
// 支持值域（JSONB 解码域）：nil/bool/string/int/int64/json.Number/float64/
// []any/map[string]any。数字保真：json.Number 按整/浮形态输出（与 Python
// json.loads 的 int/float 区分一致）；float64 按 Python repr（最短往返）.
func CanonicalJSON(v any) string {
	var b strings.Builder
	writeCanonicalJSON(&b, v)
	return b.String()
}

// writeCanonicalJSON 递归写出 canonical JSON.
func writeCanonicalJSON(b *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case string:
		b.WriteString(pyJSONString(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case json.Number:
		if i, err := strconv.ParseInt(x.String(), 10, 64); err == nil {
			b.WriteString(strconv.FormatInt(i, 10)) // 整数形态原样（Python json.loads → int）
			return
		}
		if f, err := strconv.ParseFloat(x.String(), 64); err == nil {
			b.WriteString(pyFloat(f)) // 浮点按 Python repr
			return
		}
		b.WriteString("null") // 不可解析数字（防御）
	case float64:
		b.WriteString(pyFloat(x))
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			writeCanonicalJSON(b, e)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteString(pyJSONString(k))
			b.WriteByte(':')
			writeCanonicalJSON(b, x[k])
		}
		b.WriteByte('}')
	default:
		b.WriteString("null") // 不支持的类型（防御；调用方应先规范化）
	}
}

// ────────────────────────────────────────────────────────────────────
// 重放聚合核
// ────────────────────────────────────────────────────────────────────

// RescoreFailure 是重判失败详情（对应冻结实现 RescoreReport.failures 条目）.
type RescoreFailure struct {
	EventID       string
	ItemVersionID string
	Reason        string
}

// RescoreReport 是重判报告（对应冻结实现 replay.RescoreReport）.
type RescoreReport struct {
	// RescoredCount 本次重判成功的事件数.
	RescoredCount int
	// SkippedCount 因幂等约束跳过的事件数（同事件同批次标签已存在）——纯聚合
	// 面恒 0（幂等查重是 score_run 落账面 ScoreRunSink 的职责）.
	SkippedCount int
	// FailedCount 评分失败的事件数（如评分器未注册；不阻断其他事件）.
	FailedCount int
	// Consistency 新旧 correct 一致率（0~1；无可比对事件时 0.0）.
	Consistency float64
	// SummaryHash 摘要哈希（D6 可重放——同输入必同输出）.
	SummaryHash string
	// ScorerVersion 本次重判所用评分器版本（第一个成功重判的自报版本；空 = 无成功）.
	ScorerVersion string
	// RunLabel / InputSnapshotID 批次标识.
	RunLabel        string
	InputSnapshotID string
	// Failures 失败详情.
	Failures []RescoreFailure
}

// ReplayParamSummary 是新旧参数摘要（对应冻结实现 ReplayReport.old/new_param_
// summary 的类型化重锚定）. 旧摘要 ScorerVersions 为版本分布（含 ""=trace 缺
// 版本键）；新摘要为单键（实际评分器版本），ScorerVersions 留空.
type ReplayParamSummary struct {
	ScorerVersions   map[string]int
	CorrectTrue      int
	CorrectFalse     int
	DifficultyApprox *float64 // correct 真占比（难度近似；无可判定事件时 nil）
}

// ReplayParamDiff 是新旧难度近似差异（对应冻结实现 param_diff_distribution；
// 仅在两侧均可估计时非 nil）.
type ReplayParamDiff struct {
	DifficultyOld   *float64
	DifficultyNew   *float64
	DifficultyDelta *float64 // new - old
}

// ReplayReport 是全量重放报告（对应冻结实现 replay.ReplayReport：继承
// RescoreReport，新增参数差异分布）.
type ReplayReport struct {
	RescoreReport
	OldParamSummary ReplayParamSummary
	NewParamSummary ReplayParamSummary
	ParamDiff       *ReplayParamDiff
}

// ReplayInput 是一次重放的输入快照（「输入快照 → 重算」的输入侧；事件取数
// 是 IO 面——调用方按场景从 response_event 冻结快照注入，D5 单场景）.
type ReplayInput struct {
	// PurposeScope 场景（D5 必填单值——按场景独立重放）.
	PurposeScope string
	// Events 输入快照事件流（已冻结；本核不复检 scene 字段，与冻结实现的
	// generate_ctt_report 同 stance）.
	Events []ReplayEvent
	// ScorerVersion 重判用评分器版本标签（D6——年度重放引用「当前活跃」版本；
	// 解析 ActiveModelPointer 是 IO 面，本骨架要求显式传入，空 = 报错）.
	ScorerVersion string
	// RunLabel 批次标签（如 'annual-replay-2026'；幂等保护与报告分组用）.
	RunLabel string
	// InputSnapshotID 输入数据快照标识；空 = DefaultInputSnapshotID 自动构造.
	InputSnapshotID string
}

// Rescorer 是单事件重算面：输入事件视图 → 新评分（评分器注册与调度是 IO 面；
// 对应冻结实现 run_scorer(iv, raw_payload)）.
type Rescorer interface {
	// Rescore 对单事件重判，返回 (实际评分器版本自报值, 新 correct, 错误).
	Rescore(ev ReplayEvent) (scorerVersion string, correct bool, err error)
}

// RunReplay 全量重放纯聚合面（对应冻结实现 replay_all 的聚合核）：对快照事件
// 流逐一重算，输出新旧一致性率、参数差异概览与 D6 摘要哈希。
//
// 语义对齐：旧版本统计（版本分布/correct 分布）对全部快照事件收集（含重判
// 失败者）；新统计仅计成功重判者；一致性 = 一致数 / 可比对数（旧 correct 可
// 判定且重判成功；无可比对时 0.0）。原 response_event 不动、score_run 平行
// 落账是 IO 面（ScoreRunSink，本波留白）。trace correct 不可解析时整体失败
// （冻结实现 float() 异常穿透——fail-closed，不做部分输出）.
func RunReplay(in ReplayInput, rescorer Rescorer) (ReplayReport, error) {
	if err := validatePurposeScope(in.PurposeScope); err != nil {
		return ReplayReport{}, err
	}
	if in.ScorerVersion == "" {
		return ReplayReport{}, ErrScorerVersionRequired
	}
	snapID := in.InputSnapshotID
	if snapID == "" {
		snapID = DefaultInputSnapshotID(len(in.Events), in.PurposeScope)
	}

	rep := ReplayReport{
		RescoreReport: RescoreReport{
			RunLabel:        in.RunLabel,
			InputSnapshotID: snapID,
			Failures:        []RescoreFailure{},
		},
		OldParamSummary: ReplayParamSummary{ScorerVersions: map[string]int{}},
		NewParamSummary: ReplayParamSummary{},
	}

	// 空快照：不取数不重判（对应冻结实现 rows 为空的早退——hash 为空串）
	if len(in.Events) == 0 {
		return rep, nil
	}

	var rescoredSummary []RescoreFingerprint
	consistent := 0
	comparable := 0
	actualScorerVersion := ""

	for _, ev := range in.Events {
		// 旧版本统计（无论重判成败都收——对应冻结实现顺序）
		oldSV := SafeScorerVersionFromTrace(ev.OriginalTrace)
		rep.OldParamSummary.ScorerVersions[oldSV]++
		oldCorrect, err := SafeCorrectFromTrace(ev.OriginalTrace)
		if err != nil {
			return ReplayReport{}, err
		}
		if oldCorrect != nil {
			if *oldCorrect {
				rep.OldParamSummary.CorrectTrue++
			} else {
				rep.OldParamSummary.CorrectFalse++
			}
		}

		sv, correct, rescoreErr := rescorer.Rescore(ev)
		if rescoreErr != nil {
			rep.FailedCount++
			rep.Failures = append(rep.Failures, RescoreFailure{
				EventID:       ev.EventID,
				ItemVersionID: ev.ItemVersionID,
				Reason:        rescoreErr.Error(),
			})
			continue
		}
		rep.RescoredCount++
		if actualScorerVersion == "" {
			actualScorerVersion = sv
		}
		rescoredSummary = append(rescoredSummary, RescoreFingerprint{
			EventID:       ev.EventID,
			ScorerVersion: sv,
			Correct:       correct,
		})
		if correct {
			rep.NewParamSummary.CorrectTrue++
		} else {
			rep.NewParamSummary.CorrectFalse++
		}
		// 一致性比对：新旧 correct 是否一致
		if oldCorrect != nil {
			comparable++
			if *oldCorrect == correct {
				consistent++
			}
		}
	}

	if comparable > 0 {
		rep.Consistency = float64(consistent) / float64(comparable)
	}
	rep.ScorerVersion = actualScorerVersion
	rep.SummaryHash = ComputeSummaryHash(snapID, in.Events, rescoredSummary)

	// 参数差异分布（用 correct 比例作为难度近似指标——避免重算 CTT 增加耦合）
	oldTotal := rep.OldParamSummary.CorrectTrue + rep.OldParamSummary.CorrectFalse
	newTotal := rep.NewParamSummary.CorrectTrue + rep.NewParamSummary.CorrectFalse
	if oldTotal > 0 {
		d := float64(rep.OldParamSummary.CorrectTrue) / float64(oldTotal)
		rep.OldParamSummary.DifficultyApprox = &d
	}
	if newTotal > 0 {
		d := float64(rep.NewParamSummary.CorrectTrue) / float64(newTotal)
		rep.NewParamSummary.DifficultyApprox = &d
	}
	if oldTotal > 0 && newTotal > 0 {
		delta := *rep.NewParamSummary.DifficultyApprox - *rep.OldParamSummary.DifficultyApprox
		rep.ParamDiff = &ReplayParamDiff{
			DifficultyOld:   rep.OldParamSummary.DifficultyApprox,
			DifficultyNew:   rep.NewParamSummary.DifficultyApprox,
			DifficultyDelta: &delta,
		}
	}

	return rep, nil
}

// ScoreRunRecord 是平行落账的最小字段集（与 db 层 score_run 行对齐；冻结实现
// ScoreRun ORM 的重判写入面。scorer_id 取自 item_version.scoring_ref）.
type ScoreRunRecord struct {
	EventID               string
	EventCreatedAt        time.Time
	PurposeScope          string // 事件的 scene（D5：按各自场景写对应 scope 的 score_run）
	ScorerID              string
	ScorerVersion         string // 评分器自报审计字段
	OriginalScorerVersion string // 原事件 scoring_trace 的 scorer_version
	Correct               bool
	RunLabel              string
	InputSnapshotID       string
}

// ScoreRunSink 是重放结果的平行落账面（IO 骨架——本波只声明接口，不实现）：
// 契约 §3「写平行 score_run，原 response_event 不动」；幂等键
// (event_id, event_created_at, run_label) 由实现方承担（同一批次同一事件只写
// 一条），D11 事务边界由最外层调用方确定.
type ScoreRunSink interface {
	WriteScoreRun(run ScoreRunRecord) error
}

// ────────────────────────────────────────────────────────────────────
// Parquet 归档面（IO 骨架——如实声明：本波只留接口，不实现）
// ────────────────────────────────────────────────────────────────────
//
// 对应冻结实现 parquet_export.py（pyarrow 依赖）：response_event 每日增量
// Parquet 归档（十年数据主权——开放列式格式，不绑定厂商）。零新依赖约束
// （AGENTS 规则 3）下不引入 parquet 库；写入面（PARQUET_SCHEMA 14 字段对齐 /
// snappy 压缩 / .tmp+replace 原子写 / manifest 内容哈希幂等）留待服务化波次
// 另行报批依赖后实现。本波仅交付纯命名/区间/去重/规范化助手。

// ParquetExporter 是 response_event 每日增量归档面（未实现——见上）.
type ParquetExporter interface {
	// ExportScene 导出单场景单日增量（对应冻结实现 export_scene；幂等：manifest
	// 内容哈希匹配则跳过重写，不重写文件 = 不动归档存储）。
	ExportScene(baseDir string, targetDate time.Time, scene string) (ParquetExportResult, error)
	// ExportDaily 导出全场景（对应冻结实现 export_daily；每场景一个文件）.
	ExportDaily(baseDir string, targetDate time.Time) ([]ParquetExportResult, error)
}

// ParquetExportResult 是单场景单日导出结果（对应冻结实现 ExportResult）.
type ParquetExportResult struct {
	// Scene / TargetDate 定位键.
	Scene      string
	TargetDate time.Time // UTC 日期
	// Path 输出文件路径；空 = 未创建文件（row_count==0 且无既有文件）.
	Path string
	// RowCount 去重后写入行数.
	RowCount int
	// ContentHash 内容 SHA256（排序后逐行 canonical JSON）；空集为空串.
	ContentHash string
	// SkippedUnchanged 既有 manifest 哈希匹配，本次未重写文件.
	SkippedUnchanged bool
}

// BuildParquetOutputPath 构造归档输出路径（纯函数；对应冻结实现
// build_output_path）：{base}/date=YYYY-MM-DD/scene={scene}/events-YYYYMMDD-
// {scene}.parquet。日期 + 场景双标记（验收 §1），与分区表 created_at 月度分区
// 对齐。POSIX 分隔符（对象存储键语义，跨平台同路径）.
func BuildParquetOutputPath(baseDir string, targetDate time.Time, scene string) string {
	dateStr := targetDate.Format("2006-01-02") // YYYY-MM-DD
	compact := targetDate.Format("20060102")   // YYYYMMDD
	return path.Join(baseDir,
		"date="+dateStr,
		"scene="+scene,
		fmt.Sprintf("events-%s-%s.parquet", compact, scene))
}

// ParquetDateRangeUTC 返回目标日的 UTC [00:00, 次日 00:00) 半开区间（纯函数；
// 对应冻结实现 _date_range_utc）——「昨日」按 UTC 划界保证全球时区一致.
func ParquetDateRangeUTC(targetDate time.Time) (time.Time, time.Time) {
	start := time.Date(targetDate.Year(), targetDate.Month(), targetDate.Day(), 0, 0, 0, 0, time.UTC)
	return start, start.AddDate(0, 0, 1)
}

// DedupByEventID 按 event_id 去重保留首条（纯函数；对应冻结实现
// _dedup_by_event_id）：行已按 (created_at, event_id) 排序——同 event_id 多行
// 保留最早一条；防御取数重复，保证归档幂等.
func DedupByEventID[T any](rows []T, eventID func(T) string) []T {
	seen := make(map[string]bool, len(rows))
	deduped := make([]T, 0, len(rows))
	for _, r := range rows {
		id := eventID(r)
		if seen[id] {
			continue
		}
		seen[id] = true
		deduped = append(deduped, r)
	}
	return deduped
}
