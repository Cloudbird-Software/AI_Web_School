package gate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5/pgtype"
)

// ErrInvalidFailure 表示失败留痕输入违反契约（0028 表结构 + 最小四元组必填面），
// 细分原因见 wrap 文本——留痕账只记事实，不记残缺事实.
var ErrInvalidFailure = errors.New("gate: 失败留痕输入违反契约")

// ArtifactType 被拒产物类型六值域（迁移 0028 ck_gf_artifact_type_domain 的同值
// 投影；与冻结编排器 run_gate 的 artifact_type 文档域一致）.
type ArtifactType string

// 产物类型六值（DB CHECK 同域兜底）.
const (
	ArtifactItem      ArtifactType = "item"
	ArtifactMaterial  ArtifactType = "material"
	ArtifactCorpus    ArtifactType = "corpus"
	ArtifactGroup     ArtifactType = "group"
	ArtifactBlueprint ArtifactType = "blueprint"
	ArtifactAudio     ArtifactType = "audio"
)

// ValidArtifactType 报告 t 是否在产物类型六值域内.
func ValidArtifactType(t ArtifactType) bool {
	switch t {
	case ArtifactItem, ArtifactMaterial, ArtifactCorpus,
		ArtifactGroup, ArtifactBlueprint, ArtifactAudio:
		return true
	}
	return false
}

// FailureInput 是一条门失败留痕的最小可审四元组（0028 gate_failure 一行）：
//   - 什么规则：ValidatorID/ValidatorVersion（+PolicyVersion 判定语境）
//   - 什么输入：ArtifactType/ArtifactRef
//   - 何时：FailedAt
//   - 为何拒：Reason（人读拒因，审账直读）；Evidence（结构化证据细节，
//     与冻结实现 evidence 同构；nil 记 '{}' 空对象而非 JSON null）
//
// 可空语义对齐 DB：无默认占位、无 NULL 兜底列——失败也是账面事实，留痕必须完整.
type FailureInput struct {
	// FailureID 留痕唯一 id，应用层生成（与 events.EventID 同惯例：账行身份由
	// 调用方定型，重试同 id 可天然幂等到同一行）.
	FailureID string
	// ArtifactType 被拒产物类型六值之一.
	ArtifactType ArtifactType
	// ArtifactRef 被拒产物引用（§5 minLength=1）.
	ArtifactRef string
	// ValidatorID 拒绝规则的验证器身份（§5 minLength=1）.
	ValidatorID string
	// ValidatorVersion 验证器版本（规则可演进，历史拒绝须锚定当时版本）.
	ValidatorVersion string
	// PolicyVersion 门策略链版本（判定语境）.
	PolicyVersion string
	// Reason 人读拒因（§5 minLength=1；机器细节进 Evidence）.
	Reason string
	// Evidence 结构化证据（nil → '{}' 空对象；序列化原文落库不转义）.
	Evidence map[string]any
	// FailedAt 失败发生时刻 UTC（零值即契约必填项违例，前置拒绝）.
	FailedAt time.Time
}

// FailureTrail 是绑定显式事务的门失败留痕写入服务（gate_failure 的唯一 Go 写入
// 面；Python 冻结基线 orchestrator.py「失败只写留痕、不签证书」的 Go 重锚定，
// 且终结其 cert:none 占位反模式——X11）。append-only 由迁移触发器物理兜底：
// 本域只有 INSERT 查询面可写，UPDATE/DELETE 不存在.
//
// 审计副作用纪律（D11）：留痕在调用方的显式事务内 INSERT、不自 commit——
// 「门失败且业务方决定回滚」时留痕随之消失是正确的账实一致；需要跨业务事务
// 存活的审计（如旁路观测流）由调用方另起显式独立事务自行承担（D11 语义面）.
type FailureTrail struct {
	tx Executor // 外层已 begin 的执行面；nil 即非事务上下文（fail-closed 拒绝）
	qs *dbgen.Queries
}

// NewFailureTrail 把调用方已 begin 的显式事务执行面绑定为失败留痕写入器。
// tx 允许 nil——构造不报错，但所有 Record 调用立即返回 ErrNoTransaction
// （fail-closed 落在写路径而非构造路径，与 core/events.WithTx 同惯例）.
func NewFailureTrail(tx Executor) *FailureTrail { return &FailureTrail{tx: tx, qs: dbgen.New(tx)} }

// Record 把一次被拒事实 append 进门失败留痕账，返回 failure_id（与入参一致，
// 便于调用方链式引用）。
//
// 预期失败面：无显式事务面 → ErrNoTransaction；契约违例 → ErrInvalidFailure；
// 驱动/约束错误原样 wrap 放行（append-only 触发器拒绝的 SQLSTATE 证据不吞）.
func (t *FailureTrail) Record(ctx context.Context, in FailureInput) (string, error) {
	if t == nil || t.tx == nil {
		return "", ErrNoTransaction
	}
	arg, err := in.params()
	if err != nil {
		return "", err
	}
	if err := t.qs.InsertGateFailure(ctx, arg); err != nil {
		return "", fmt.Errorf("gate: insert gate_failure: %w", err)
	}
	return in.FailureID, nil
}

// params 校验入参并映射为生成层的类型安全参数（契约逐项对照前置到进程内）.
func (in FailureInput) params() (dbgen.InsertGateFailureParams, error) {
	var arg dbgen.InsertGateFailureParams

	if in.FailureID == "" {
		return arg, fmt.Errorf("%w: failure_id 不能为空", ErrInvalidFailure)
	}
	if !ValidArtifactType(in.ArtifactType) {
		return arg, fmt.Errorf("%w: artifact_type %q 不在 item/material/corpus/group/blueprint/audio 六值域内",
			ErrInvalidFailure, string(in.ArtifactType))
	}
	if in.ArtifactRef == "" {
		return arg, fmt.Errorf("%w: artifact_ref 不能为空", ErrInvalidFailure)
	}
	if in.ValidatorID == "" {
		return arg, fmt.Errorf("%w: validator_id 不能为空（什么规则必须有名字）", ErrInvalidFailure)
	}
	if in.ValidatorVersion == "" {
		return arg, fmt.Errorf("%w: validator_version 不能为空（拒绝必须锚定规则版本）", ErrInvalidFailure)
	}
	if in.PolicyVersion == "" {
		return arg, fmt.Errorf("%w: policy_version 不能为空（判定语境必填）", ErrInvalidFailure)
	}
	if in.Reason == "" {
		return arg, fmt.Errorf("%w: reason 不能为空（为何拒不许多语焉不详）", ErrInvalidFailure)
	}
	if in.FailedAt.IsZero() {
		return arg, fmt.Errorf("%w: failed_at 必填（何时为零值即契约违例）", ErrInvalidFailure)
	}

	evidence, err := jsonb("evidence", orEmptyObject(in.Evidence))
	if err != nil {
		return arg, err
	}

	return dbgen.InsertGateFailureParams{
		FailureID:        in.FailureID,
		ArtifactType:     string(in.ArtifactType),
		ArtifactRef:      in.ArtifactRef,
		ValidatorID:      in.ValidatorID,
		ValidatorVersion: in.ValidatorVersion,
		PolicyVersion:    in.PolicyVersion,
		Reason:           in.Reason,
		Evidence:         evidence,
		FailedAt:         pgtype.Timestamptz{Time: in.FailedAt, Valid: true},
	}, nil
}

// orEmptyObject 收敛 Evidence：nil 记空对象而非 JSON null（DB 默认 '{}' 的应用侧
// 对应物，审账读到的是结构而非 null 哑弹）.
func orEmptyObject(e map[string]any) map[string]any {
	if e == nil {
		return map[string]any{}
	}
	return e
}

// jsonb 序列化 JSONB 字段：SetEscapeHTML(false) 与 Python ensure_ascii=False
// 同向——Unicode/HTML 字符按原文落库，便于人工审账时直读（与本仓 core/events
// 的同名 helper 同义；领域端口各自声明最小依赖面，不作跨包复用导出）.
func jsonb(field string, v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, fmt.Errorf("%w: %s JSON 序列化失败: %w", ErrInvalidFailure, field, err)
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}
