// 内容只读查询服务（GO-RW-001）：GET /items、/item_versions、/templates、
// /gate_certificates 四条只读端点的领域取证面。
//
// 冻结基准（语义对齐、形态重锚定）：
//   - src/api/routers/items.py：GET /items 返回 item 身份 + current_version
//     （若有）；GET /item_versions 返回六大块 + 谱系；GET /templates 同 items
//     的母题版。冻结版用 ORM 两步取回（get(Item) → get(ItemVersion)），本服务
//     保持两步取证——不用 JOIN 把指针悬空静默折损成 NULL，账面残缺必须
//     fail-loud（ErrDanglingCurrentVersion）。
//   - src/api/routers/gate.py：GET /gate_certificates 返回证书 + 关联全部
//     gate_run（含 gate_verdict 明细）。冻结版 selectinload 两条 SQL 带全，
//     本服务同构（ListGateRunsByCertificate + ListGateVerdictsByCertificate
//     两条语句，按 run_id 在应用层归组）。
//
// 纪律：只读（本服务只调用 SELECT 语句面，无任何写路径可触达）；全部语句
// 文本只住在 db/queries/*.sql（SQL-2），经 sqlc 生成为类型安全 dbgen 方法，
// 本包仅作调用方。响应视图（*View/*Detail）字段与冻结契约
// specs/contracts/api/openapi-v1.1.json 的四个响应 schema 一一对应，json
// 标签即契约字段名——api 层零业务语义，直接序列化这些视图.
package content

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoExecutor 表示查询执行面未装配（nil）。fail-closed：没有执行面就
	// 不编造数据，构造不报错、调用即失败（与 NewPublishService 同惯例）.
	ErrNoExecutor = errors.New("content: 查询执行面未装配")

	// ErrUnknownItem 表示 item_id 在 item 表无行.
	ErrUnknownItem = errors.New("content: item 不存在")

	// ErrUnknownItemVersion 表示 item_version_id 在 item_version 无行（读侧；
	// 发布侧的同义哨兵是 ErrUnknownContentVersion，两者并存——语义场景不同，
	// 不共享避免发布路径的措辞污染读路径的判定）.
	ErrUnknownItemVersion = errors.New("content: item_version 不存在")

	// ErrUnknownTemplate 表示 template_id 在 item_template 无行.
	ErrUnknownTemplate = errors.New("content: 母题不存在")

	// ErrUnknownGateCertificate 表示 cert_id 在 gate_certificate 无行.
	ErrUnknownGateCertificate = errors.New("content: 门证书不存在")

	// ErrDanglingCurrentVersion 表示 item/item_template 的 current_version_id
	// 指针指向不存在的版本行：指针表不在 append-only 账列、无 FK 兜底，
	// 悬空即账面残缺——冻结实现把它静默折损成 current_version=null，本服务
	// 终结该反模式（D3：证不了的状态不输出，宁可 500 也不伪造空值）.
	ErrDanglingCurrentVersion = errors.New("content: current_version_id 指针悬空（指向的版本不存在）")
)

// ItemVersionView 是 item_version 行的读取投影（契约 ItemVersionPydantic）：
// 六大块与谱系按原文透传（json.RawMessage——内容寻址的口径就是行内字节，
// 重序列化等于重写口径）；rendered_snapshot 可空（draft/quarantined 阶段无
// 渲染快照），nil 序列化为 null.
type ItemVersionView struct {
	ItemVersionID     string          `json:"item_version_id"`
	ItemID            string          `json:"item_id"`
	Status            string          `json:"status"`
	Objective         json.RawMessage `json:"objective"`
	InteractionRef    json.RawMessage `json:"interaction_ref"`
	Content           json.RawMessage `json:"content"`
	ScoringRef        json.RawMessage `json:"scoring_ref"`
	ErrorBindings     json.RawMessage `json:"error_bindings"`
	Lineage           json.RawMessage `json:"lineage"`
	RenderedSnapshot  json.RawMessage `json:"rendered_snapshot"`
	GateCertificateID *string         `json:"gate_certificate_id"`
	PublishedAt       *time.Time      `json:"published_at"`
	RetiredAt         *time.Time      `json:"retired_at"`
	CreatedAt         *time.Time      `json:"created_at"`
}

// ItemDetail 是 GET /items/{item_id} 响应投影（契约 ItemDetailResponse）：
// item 不变身份 + current_version 指针解引用（无指针即 nil → null）.
type ItemDetail struct {
	ItemID            string           `json:"item_id"`
	PackID            string           `json:"pack_id"`
	Tier              string           `json:"tier"`
	TemplateVersionID *string          `json:"template_version_id"`
	CurrentVersionID  *string          `json:"current_version_id"`
	CreatedAt         *time.Time       `json:"created_at"`
	CurrentVersion    *ItemVersionView `json:"current_version"`
}

// TemplateVersionView 是 item_template_version 行的读取投影（契约
// ItemTemplateVersionPydantic）：spec 保持对象原文不强约束——母题 DSL 结构随
// DSL 版本演化，读取面不做形状收紧（冻结实现同款决策）.
type TemplateVersionView struct {
	TemplateVersionID string          `json:"template_version_id"`
	TemplateID        string          `json:"template_id"`
	DslVersion        string          `json:"dsl_version"`
	Spec              json.RawMessage `json:"spec"`
	Status            string          `json:"status"`
	CreatedAt         *time.Time      `json:"created_at"`
}

// TemplateDetail 是 GET /templates/{template_id} 响应投影（契约
// TemplateDetailResponse）：母题不变身份 + current_version（若有）.
type TemplateDetail struct {
	TemplateID       string               `json:"template_id"`
	PackID           string               `json:"pack_id"`
	CurrentVersionID *string              `json:"current_version_id"`
	CreatedAt        *time.Time           `json:"created_at"`
	CurrentVersion   *TemplateVersionView `json:"current_version"`
}

// GateVerdictView 是单条门判定明细（契约 GateVerdictRead）.
type GateVerdictView struct {
	VerdictID int64           `json:"verdict_id"`
	RunID     string          `json:"run_id"`
	Detail    json.RawMessage `json:"detail"`
	CreatedAt *time.Time      `json:"created_at"`
}

// GateRunView 是单次验证器运行记录（契约 GateRunRead）。confidence 以十进制
// 字符串过线（契约显式 pattern 约束 + 冻结实现 Decimal 原文语义）——浮点重
// 解析即口径漂移，读取面不做 float64 转换.
type GateRunView struct {
	RunID            string            `json:"run_id"`
	CertificateID    string            `json:"certificate_id"`
	PolicyVersion    string            `json:"policy_version"`
	ValidatorID      string            `json:"validator_id"`
	ValidatorVersion string            `json:"validator_version"`
	Verdict          string            `json:"verdict"`
	Evidence         json.RawMessage   `json:"evidence"`
	Confidence       string            `json:"confidence"`
	CostMs           int32             `json:"cost_ms"`
	CostTokens       int32             `json:"cost_tokens"`
	RunAt            *time.Time        `json:"run_at"`
	CreatedAt        *time.Time        `json:"created_at"`
	Verdicts         []GateVerdictView `json:"verdicts"`
}

// GateCertificateDetail 是 GET /gate_certificates/{cert_id} 响应投影（契约
// GateCertificateRead）：证书本体 + runs（冻结实现 default_factory=list，
// 空集序列化为 [] 而非 null）.
type GateCertificateDetail struct {
	CertID        string        `json:"cert_id"`
	ArtifactRef   string        `json:"artifact_ref"`
	CertType      string        `json:"cert_type"`
	PolicyVersion string        `json:"policy_version"`
	IssuedBy      string        `json:"issued_by"`
	IssuedAt      *time.Time    `json:"issued_at"`
	CreatedAt     *time.Time    `json:"created_at"`
	Runs          []GateRunView `json:"runs"`
}

// ContentQueryService 是绑定语句执行面的内容只读查询服务：四条 GET 端点的
// 全部 DB 取证经本服务，api 层零 SQL、零行归零知识.
//
// 装配纪律：不持有连接、不开事务——只读路径直接跑在连接池默认读面上
// （单语句自含一致性，读侧无需 D11 显式事务）；db 允许 nil，构造不报错但
// 所有查询立即返回 ErrNoExecutor（fail-closed 落在调用路径，与
// NewPublishService 同惯例）.
type ContentQueryService struct {
	db Executor // 只读执行面（pgxpool 连接 / pgx.Tx 均满足）；nil 即未装配
	qs *dbgen.Queries
}

// NewContentQueryService 把只读执行面绑定为内容查询服务。
func NewContentQueryService(db Executor) *ContentQueryService {
	return &ContentQueryService{db: db, qs: dbgen.New(db)}
}

// GetItem 取 item 身份 + current_version 解引用（两步取证：指针悬空是账面
// 残缺，fail-loud 而非静默 null——见 ErrDanglingCurrentVersion）.
func (s *ContentQueryService) GetItem(ctx context.Context, itemID string) (*ItemDetail, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	row, err := s.qs.GetItem(ctx, itemID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: item_id=%q 在 item 无行", ErrUnknownItem, itemID)
		}
		return nil, fmt.Errorf("content: get item: %w", err)
	}
	detail := &ItemDetail{
		ItemID:            row.ItemID,
		PackID:            row.PackID,
		Tier:              string(row.Tier),
		TemplateVersionID: textPtr(row.TemplateVersionID),
		CurrentVersionID:  textPtr(row.CurrentVersionID),
		CreatedAt:         timePtr(row.CreatedAt),
	}
	if cur := textPtr(row.CurrentVersionID); cur != nil {
		version, err := s.GetItemVersion(ctx, *cur)
		if err != nil {
			if errors.Is(err, ErrUnknownItemVersion) {
				return nil, fmt.Errorf("%w: item_id=%q 的 current_version_id=%q 无对应 item_version 行",
					ErrDanglingCurrentVersion, itemID, *cur)
			}
			return nil, err
		}
		detail.CurrentVersion = version
	}
	return detail, nil
}

// GetItemVersion 取版本行六大块 + 谱系（无行 → ErrUnknownItemVersion；驱动
// 故障原样 wrap 放行——DB 故障与"无行"是两类处置，不许混报）.
func (s *ContentQueryService) GetItemVersion(ctx context.Context, itemVersionID string) (*ItemVersionView, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	row, err := s.qs.GetItemVersion(ctx, itemVersionID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: item_version_id=%q 在 item_version 无行", ErrUnknownItemVersion, itemVersionID)
		}
		return nil, fmt.Errorf("content: get item_version: %w", err)
	}
	return newItemVersionView(row), nil
}

// GetTemplate 取母题身份 + current_version 解引用（悬空处理同 GetItem）.
func (s *ContentQueryService) GetTemplate(ctx context.Context, templateID string) (*TemplateDetail, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	row, err := s.qs.GetItemTemplate(ctx, templateID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: template_id=%q 在 item_template 无行", ErrUnknownTemplate, templateID)
		}
		return nil, fmt.Errorf("content: get item_template: %w", err)
	}
	detail := &TemplateDetail{
		TemplateID:       row.TemplateID,
		PackID:           row.PackID,
		CurrentVersionID: textPtr(row.CurrentVersionID),
		CreatedAt:        timePtr(row.CreatedAt),
	}
	if cur := textPtr(row.CurrentVersionID); cur != nil {
		versionRow, err := s.qs.GetItemTemplateVersion(ctx, *cur)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return nil, fmt.Errorf("%w: template_id=%q 的 current_version_id=%q 无对应 item_template_version 行",
					ErrDanglingCurrentVersion, templateID, *cur)
			}
			return nil, fmt.Errorf("content: get item_template_version: %w", err)
		}
		detail.CurrentVersion = &TemplateVersionView{
			TemplateVersionID: versionRow.TemplateVersionID,
			TemplateID:        versionRow.TemplateID,
			DslVersion:        versionRow.DslVersion,
			Spec:              json.RawMessage(versionRow.Spec),
			Status:            string(versionRow.Status),
			CreatedAt:         timePtr(versionRow.CreatedAt),
		}
	}
	return detail, nil
}

// GetGateCertificate 取证书 + 关联全部运行记录 + 判定明细（两条列表语句按
// run_id 应用层归组——冻结 selectinload 的同构形态，避免按 run 逐个 N+1）.
func (s *ContentQueryService) GetGateCertificate(ctx context.Context, certID string) (*GateCertificateDetail, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	certRow, err := s.qs.GetGateCertificate(ctx, certID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: cert_id=%q 在 gate_certificate 无行", ErrUnknownGateCertificate, certID)
		}
		return nil, fmt.Errorf("content: get gate_certificate: %w", err)
	}
	certRef := pgtype.Text{String: certID, Valid: true}
	runRows, err := s.qs.ListGateRunsByCertificate(ctx, certRef)
	if err != nil {
		return nil, fmt.Errorf("content: list gate_run: %w", err)
	}
	verdictRows, err := s.qs.ListGateVerdictsByCertificate(ctx, certRef)
	if err != nil {
		return nil, fmt.Errorf("content: list gate_verdict: %w", err)
	}

	verdictsByRun := make(map[string][]GateVerdictView, len(verdictRows))
	for _, v := range verdictRows {
		verdictsByRun[v.RunID] = append(verdictsByRun[v.RunID], GateVerdictView{
			VerdictID: v.VerdictID,
			RunID:     v.RunID,
			Detail:    json.RawMessage(v.Detail),
			CreatedAt: timePtr(v.CreatedAt),
		})
	}
	// 空集也输出 []（冻结 default_factory=list 语义；nil 会序列化成 null 偏离契约）.
	runs := make([]GateRunView, 0, len(runRows))
	for _, r := range runRows {
		confidence, err := numericText(r.Confidence)
		if err != nil {
			return nil, fmt.Errorf("content: gate_run %s: %w", r.RunID, err)
		}
		verdicts := verdictsByRun[r.RunID]
		if verdicts == nil {
			verdicts = []GateVerdictView{}
		}
		runs = append(runs, GateRunView{
			RunID:            r.RunID,
			CertificateID:    r.CertificateID.String,
			PolicyVersion:    r.PolicyVersion,
			ValidatorID:      r.ValidatorID,
			ValidatorVersion: r.ValidatorVersion,
			Verdict:          string(r.Verdict),
			Evidence:         json.RawMessage(r.Evidence),
			Confidence:       confidence,
			CostMs:           r.CostMs,
			CostTokens:       r.CostTokens,
			RunAt:            timePtr(r.RunAt),
			CreatedAt:        timePtr(r.CreatedAt),
			Verdicts:         verdicts,
		})
	}
	return &GateCertificateDetail{
		CertID:        certRow.CertID,
		ArtifactRef:   certRow.ArtifactRef,
		CertType:      certRow.CertType,
		PolicyVersion: certRow.PolicyVersion,
		IssuedBy:      certRow.IssuedBy,
		IssuedAt:      timePtr(certRow.IssuedAt),
		CreatedAt:     timePtr(certRow.CreatedAt),
		Runs:          runs,
	}, nil
}

// newItemVersionView 把版本行折叠成读取投影（GetItem 与 GetItemVersion 共用）.
func newItemVersionView(row dbgen.ItemVersion) *ItemVersionView {
	return &ItemVersionView{
		ItemVersionID:     row.ItemVersionID,
		ItemID:            row.ItemID,
		Status:            string(row.Status),
		Objective:         json.RawMessage(row.Objective),
		InteractionRef:    json.RawMessage(row.InteractionRef),
		Content:           json.RawMessage(row.Content),
		ScoringRef:        json.RawMessage(row.ScoringRef),
		ErrorBindings:     json.RawMessage(row.ErrorBindings),
		Lineage:           json.RawMessage(row.Lineage),
		RenderedSnapshot:  json.RawMessage(row.RenderedSnapshot),
		GateCertificateID: textPtr(row.GateCertificateID),
		PublishedAt:       timePtr(row.PublishedAt),
		RetiredAt:         timePtr(row.RetiredAt),
		CreatedAt:         timePtr(row.CreatedAt),
	}
}

// textPtr 收敛可空 text 列：NULL → nil（序列化 null），非空 → 值指针.
func textPtr(t pgtype.Text) *string {
	if !t.Valid {
		return nil
	}
	s := t.String
	return &s
}

// timePtr 收敛可空 timestamptz 列：NULL → nil（序列化 null），非空 → 时刻指针.
func timePtr(ts pgtype.Timestamptz) *time.Time {
	if !ts.Valid {
		return nil
	}
	t := ts.Time
	return &t
}

// numericText 收敛 NUMERIC 扫描值为十进制字符串（契约 confidence 的过线形态，
// 冻结 Decimal 原文语义）。confidence 是 NOT NULL 列，扫出 NULL 属账面/驱动
// 异常——fail-loud 返回错误，绝不编造 "0" 冒充有效值.
func numericText(n pgtype.Numeric) (string, error) {
	if !n.Valid {
		return "", errors.New("content: NUMERIC 列扫出 NULL（NOT NULL 约定被破坏，拒绝编造缺省值）")
	}
	v, err := n.Value()
	if err != nil {
		return "", fmt.Errorf("content: NUMERIC 文本化失败: %w", err)
	}
	s, ok := v.(string)
	if !ok {
		return "", fmt.Errorf("content: NUMERIC 文本化产物 %T 非 string", v)
	}
	return s, nil
}
