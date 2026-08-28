// 发布服务（T-W5-003）：发布事务里「持证 + 内容寻址可证」双双 fail-loud。
//
// 冻结基准与修复点（Go 重锚定，语义不变、缺陷不留）：
//   - src/core/content/publication.py issue_item_version：签发闭环 = 状态前移
//     （draft/quarantined → published，无回边无重签）+ publication 签发账入账 +
//     item.current_version_id 指针前移。冻结版内部 commit——本服务按 D11 上移
//     到最外层调用方（事务面见 NewPublishService）。
//   - src/core/content/writer.py：published 必持 gate_certificate_id 的门强制
//     （GateEnforcementError）；A/B 级公式一缺参时退化为 UUID 的反模式——本服务
//     以哨兵错误终结：摘要不可证即拒绝发布，零静默降级（D3）。
//
// 判定序（全部前置到内容写入之前，任一失败即拒绝且零副作用落库）：
//  1. 无显式事务面 → ErrNoTransaction；
//  2. 请求契约违例 → ErrInvalidPublication（出进程前拦截，不烧事务语句）；
//  3. 版本行不存在 → ErrUnknownContentVersion；
//  4. 状态机：已 published → ErrAlreadyPublished（无重签）；已 retired →
//     ErrContentRetired（无回边）；
//  5. 内容寻址：按冻结公式一/二重算 ContentDigest 与版本 id 对表——不匹配 →
//     ErrContentDigestMismatch；公式一必填参数缺失 → ErrContentDigestUnverifiable；
//  6. 证书验真：复用 core/gate.CertificateVerifier（存在性 + publish 用途 +
//     artifact_ref 绑定本版本），哨兵错误原样放行不吞。
package content

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoTransaction 表示发布调用没有显式事务执行面。D11 fail-closed：发布
	// 的状态前移、签发账与指针前移必须同进同退，绝不在无事务面上「先发先得」.
	ErrNoTransaction = errors.New("content: 无显式事务执行面（D11 fail-closed：发布只接受外层已 begin 的事务）")

	// ErrInvalidPublication 表示发布请求违反契约，细分原因见 wrap 文本。契约
	// 违例在出 Go 进程前拦截，不烧事务语句、不给 PG 报错晚到.
	ErrInvalidPublication = errors.New("content: 发布请求违反契约")

	// ErrUnknownContentVersion 表示待发布的 item_version_id 无行——不存在的
	// 内容没有可发布的快照，也没有可验证的内容地址.
	ErrUnknownContentVersion = errors.New("content: 待发布内容版本不存在")

	// ErrAlreadyPublished 表示版本已处 published（契约 §4 状态机无重签；重发
	// 同一版本只会制造第二行签发账，审计面即假账）.
	ErrAlreadyPublished = errors.New("content: 内容版本已是 published（状态机无重签）")

	// ErrContentRetired 表示版本已处 retired（状态机无回边；退役是状态不是删除，
	// 复活路径不存在）.
	ErrContentRetired = errors.New("content: 内容版本已 retired（状态机无回边）")

	// ErrContentDigestUnverifiable 表示内容寻址不可证：冻结公式所需的必填参数
	// 缺失（或内容块不可解释）——终结 writer.py「缺参退化为 UUID」反模式的
	// fail-loud 面：证明不了的地址不发布，宁可拒绝也绝不编造.
	ErrContentDigestUnverifiable = errors.New("content: 内容寻址不可证（公式必填参数缺失，禁止退化路径）")

	// ErrContentDigestMismatch 表示重算摘要与版本 id 不一致：id 不是这份内容的
	// 内容寻址（伪造 id / 内容与 id 脱钩 / 口径漂移都会在此暴露）——D3 的
	// 发布侧物理验证，不一致即 fail，零静默放行.
	ErrContentDigestMismatch = errors.New("content: 内容寻址不一致（重算摘要 ≠ 版本 id）")
)

// ItemStatus 内容版本状态四值域（迁移 0002 item_version_status_enum 同值投影；
// 状态机 draft → quarantined → published → retired，无回边）.
type ItemStatus string

// 状态四值（与 DB enum 同域，DB 物理约束兜底）.
const (
	StatusDraft       ItemStatus = "draft"
	StatusQuarantined ItemStatus = "quarantined"
	StatusPublished   ItemStatus = "published"
	StatusRetired     ItemStatus = "retired"
)

// Executor 是发布事务所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX 同构
// （与本仓 core/events、core/gate 的同名接口同形）。
//
// 为什么不复用他域接口而本地重声明：领域端口按需各自声明最小依赖面，六边形
// 核心域之间不为一个三方法接口建立编译耦合；两者方法集一致，pgx.Tx 与连接池
// 事务面天然同时满足。全部语句文本只住在 db/queries/content.sql（SQL-2：不在
// Go 拼 SQL），经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方——UPDATE
// 只触及契约 §4 允许的状态机字段与指针表列，内容快照无更新面可写.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// 编译期锚定一：pgx.Tx 必须满足 Executor（W6 装配直通的假设防线）.
var _ Executor = (pgx.Tx)(nil)

// 编译期锚定二：Executor 必须满足生成层执行面 dbgen.DBTX——NewPublishService
// 内部用 dbgen.New(tx) 构造类型安全查询器；sqlc 升级改形状时在此第一时间红.
var _ dbgen.DBTX = Executor(nil)

// PublishRequest 是一次发布事务的声明：发布哪个版本、持哪张证、谁在何时发布。
// 可空语义对齐 DB：必填字段一律显式声明，零值即契约违例（前置拒绝，不猜默认）.
type PublishRequest struct {
	// PublicationID 签发账行唯一 id，应用层生成（与 events.EventID、
	// gate.FailureInput.FailureID 同惯例：账行身份由调用方定型，重试同 id
	// 撞 PK 即天然幂等屏障）.
	PublicationID string
	// ItemVersionID 待发布内容版本（内容寻址 id，D3）；发布主体，必填.
	ItemVersionID string
	// GateCertificateID 门证书 id；published 必持证（D2，冻结 writer
	// GateEnforcementError 的字面语义），空即拒.
	GateCertificateID string
	// PublishedBy 发布人 id（签发账「谁」的审计锚），必填.
	PublishedBy string
	// Locale 内容寻址所用语言/地区（冻结公式一/二的 l 输入；行内不落 locale
	// 列，发布侧重算摘要必须与写入侧同 locale——显式声明，缺省即拒）.
	Locale string
	// PublishedAt 发布时刻 UTC（零值即契约必填项违例，前置拒绝）.
	PublishedAt time.Time
}

// validate 前置拦截请求契约违例：空引用与零时刻在本进程内失败，不发 SQL.
func (r PublishRequest) validate() error {
	if r.PublicationID == "" {
		return fmt.Errorf("%w: publication_id 不能为空（签发账行身份由调用方定型）", ErrInvalidPublication)
	}
	if r.ItemVersionID == "" {
		return fmt.Errorf("%w: item_version_id 不能为空", ErrInvalidPublication)
	}
	if r.GateCertificateID == "" {
		return fmt.Errorf("%w: gate_certificate_id 不能为空（D2：published 必持门证书）", ErrInvalidPublication)
	}
	if r.PublishedBy == "" {
		return fmt.Errorf("%w: published_by 不能为空（签发账必须记谁）", ErrInvalidPublication)
	}
	if r.Locale == "" {
		return fmt.Errorf("%w: locale 不能为空（内容寻址重算必须与写入侧同 locale）", ErrInvalidPublication)
	}
	if r.PublishedAt.IsZero() {
		return fmt.Errorf("%w: published_at 必填（何时为零值即契约违例）", ErrInvalidPublication)
	}
	return nil
}

// Publication 是发布成功的签发账投影（发布方据此回执；publication 行本身已
// 在外层事务内入账，COMMIT 由最外层调用方持有）.
type Publication struct {
	PublicationID     string
	ItemID            string
	ItemVersionID     string
	GateCertificateID string
	PublishedAt       time.Time
}

// PublishService 是绑定显式事务的发布服务：证书验真（复用 core/gate 的
// CertificateVerifier）+ 内容寻址重算对表 + 状态前移 + 签发账入账 + 指针前移，
// 全部运行在调用方已 begin 的同一事务里.
//
// 事务纪律（S4/D11）：不持有连接、不自 begin、永不 Commit/Rollback——状态
// 前移、签发账与指针前移三写同进同退；冻结 publication.py 的内部 commit 是
// 边界缺陷，本服务予以终结（提交/回滚归最外层调用方）.
type PublishService struct {
	tx Executor // 外层已 begin 的执行面；nil 即非事务上下文（fail-closed 拒绝）
	qs *dbgen.Queries
}

// NewPublishService 把调用方已 begin 的显式事务执行面绑定为发布服务。tx 允许
// nil——构造不报错，但所有 Publish 调用立即返回 ErrNoTransaction：fail-closed
// 落在发布路径而非构造路径（与 core/events.WithTx 同惯例）.
func NewPublishService(tx Executor) *PublishService {
	return &PublishService{tx: tx, qs: dbgen.New(tx)}
}

// Publish 执行一次发布事务的领域面（不含 COMMIT）：
//
//	取证 → 状态机 → 内容寻址对表 → 证书验真 → 状态前移 + 签发账 + 指针前移
//
// 任何失败返回 nil 投影与非 nil 错误；已发出的语句随外层回滚消失（调用方
// Rollback 即零残留）。成功返回签发账投影.
func (s *PublishService) Publish(ctx context.Context, req PublishRequest) (*Publication, error) {
	if s == nil || s.tx == nil {
		return nil, ErrNoTransaction
	}
	if err := req.validate(); err != nil {
		return nil, err
	}

	// 1. 取证：待发布版本必须存在（不存在的版本无快照亦无地址可验）.
	row, err := s.qs.GetItemVersion(ctx, req.ItemVersionID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: item_version_id=%q 在 item_version 无行", ErrUnknownContentVersion, req.ItemVersionID)
		}
		return nil, fmt.Errorf("content: get item_version: %w", err)
	}

	// 2. 状态机：draft/quarantined 可前移；published 无重签；retired 无回边.
	switch ItemStatus(row.Status) {
	case StatusDraft, StatusQuarantined:
		// 合法前移起点.
	case StatusPublished:
		return nil, fmt.Errorf("%w: item_version_id=%q", ErrAlreadyPublished, req.ItemVersionID)
	case StatusRetired:
		return nil, fmt.Errorf("%w: item_version_id=%q", ErrContentRetired, req.ItemVersionID)
	default:
		return nil, fmt.Errorf("%w: status %q 不在 draft/quarantined/published/retired 四值域内",
			ErrInvalidPublication, string(row.Status))
	}

	// 3. 内容寻址对表（D3 发布侧物理验证）：重算摘要 ≠ 版本 id 即拒——
	// 伪造 id、内容与 id 脱钩、公式缺参在此全部 fail-loud.
	if err := verifyContentAddress(row, req.Locale); err != nil {
		return nil, err
	}

	// 4. 证书验真（D2）：复用门域 CertificateVerifier——存在性 + publish 用途
	// + artifact_ref 绑定本版本；gate 哨兵已带诊断 wrap，原样放行不吞不裹
	// （「假证拦发布」与「驱动故障走运维」两类处置不许混报）.
	if _, err := gate.NewCertificateVerifier(s.tx).Verify(ctx, req.GateCertificateID, gate.Requirement{
		ArtifactRef: req.ItemVersionID,
		CertType:    gate.CertPublish,
	}); err != nil {
		return nil, err
	}

	// 5. 三写同事务：状态前移 → 签发账 → 指针前移（FK 均 DEFERRABLE，语句
	// 先后序自由，一致性在 COMMIT 边界统一验证）.
	if err := s.qs.UpdateItemVersionPublished(ctx, dbgen.UpdateItemVersionPublishedParams{
		ItemVersionID:     req.ItemVersionID,
		GateCertificateID: pgtype.Text{String: req.GateCertificateID, Valid: true},
		PublishedAt:       pgtype.Timestamptz{Time: req.PublishedAt, Valid: true},
	}); err != nil {
		return nil, fmt.Errorf("content: update item_version published: %w", err)
	}
	if err := s.qs.InsertPublication(ctx, dbgen.InsertPublicationParams{
		PublicationID:     req.PublicationID,
		ItemID:            row.ItemID,
		ItemVersionID:     req.ItemVersionID,
		GateCertificateID: pgtype.Text{String: req.GateCertificateID, Valid: true},
		PublishedBy:       req.PublishedBy,
		PublishedAt:       pgtype.Timestamptz{Time: req.PublishedAt, Valid: true},
	}); err != nil {
		return nil, fmt.Errorf("content: insert publication: %w", err)
	}
	if err := s.qs.ForwardItemCurrentVersion(ctx, dbgen.ForwardItemCurrentVersionParams{
		ItemID:           row.ItemID,
		CurrentVersionID: pgtype.Text{String: req.ItemVersionID, Valid: true},
	}); err != nil {
		return nil, fmt.Errorf("content: forward item.current_version_id: %w", err)
	}

	return &Publication{
		PublicationID:     req.PublicationID,
		ItemID:            row.ItemID,
		ItemVersionID:     req.ItemVersionID,
		GateCertificateID: req.GateCertificateID,
		PublishedAt:       req.PublishedAt,
	}, nil
}

// verifyContentAddress 重算内容摘要并与版本 id 对表（冻结 writer
// _compute_item_version_id 判定树的 fail-loud 重写，UUID 退化路径不复存在）：
//   - tier ∈ {A,B} 且 lineage.template_version_id 非空 → 公式一
//     H(tvd, np, pd, ed, cd, l)：pack_digest / engine_digest 必填，缺失即
//     ErrContentDigestUnverifiable（冻结版在此退化为随机 UUID——D3 破坏点）；
//   - 其余（C/D 级，及冻结语义下落公式二的缺 template 变体）→ 公式二
//     H(canonical(o, ir, c, sr, eb), l)：五块内容全部在行内，必可重算。
//
// 摘要口径唯一源 = validators.ContentDigest（T-W5-020 锚定），本包不另造规范化.
func verifyContentAddress(row dbgen.ItemVersion, locale string) error {
	lineage := decodeLineage(row.Lineage)

	tier, _ := lineageString(lineage, "tier")
	templateRef, _ := lineageString(lineage, "template_version_id")

	var addressed map[string]any
	if (tier == "A" || tier == "B") && templateRef != "" {
		// 公式一：谱系即证据链，缺一项都无法证明这个 id 是这份内容的.
		packDigest, ok := lineageString(lineage, "pack_digest")
		if !ok {
			return fmt.Errorf("%w: 公式一缺 pack_digest（lineage 未携带，冻结版此处退化为随机 UUID）",
				ErrContentDigestUnverifiable)
		}
		engineDigest, ok := lineageString(lineage, "engine_digest")
		if !ok {
			return fmt.Errorf("%w: 公式一缺 engine_digest（lineage 未携带，冻结版此处退化为随机 UUID）",
				ErrContentDigestUnverifiable)
		}
		addressed = map[string]any{
			"tvd": templateRef,
			"np":  normalizedParams(lineage),
			"pd":  packDigest,
			"ed":  engineDigest,
			"l":   locale,
		}
		corpusRefs, err := corpusDigests(lineage)
		if err != nil {
			return err
		}
		addressed["cd"] = corpusRefs
	} else {
		// 公式二：五块内容即证据链（行内 NOT NULL jsonb，不可解释即账面残缺）.
		objective, err := decodeJSONB("objective", row.Objective)
		if err != nil {
			return err
		}
		interactionRef, err := decodeJSONB("interaction_ref", row.InteractionRef)
		if err != nil {
			return err
		}
		blocks, err := decodeJSONB("content", row.Content)
		if err != nil {
			return err
		}
		scoringRef, err := decodeJSONB("scoring_ref", row.ScoringRef)
		if err != nil {
			return err
		}
		errorBindings, err := decodeJSONB("error_bindings", row.ErrorBindings)
		if err != nil {
			return err
		}
		addressed = map[string]any{
			"o":  objective,
			"ir": interactionRef,
			"c":  blocks,
			"sr": scoringRef,
			"eb": errorBindings,
			"l":  locale,
		}
	}

	recomputed, err := validators.ContentDigest(addressed)
	if err != nil {
		return fmt.Errorf("%w: 内容规范化失败: %w", ErrContentDigestUnverifiable, err)
	}
	if recomputed != row.ItemVersionID {
		return fmt.Errorf("%w: item_version_id=%q 重算摘要=%q（内容与地址脱钩，拒绝发布）",
			ErrContentDigestMismatch, row.ItemVersionID, recomputed)
	}
	return nil
}

// decodeLineage 收敛 lineage 列：不可解释时回 nil——冻结 writer 对缺 lineage
// 缺省 tier=C，本函数保持同一缺省语义（公式二路径不依赖 lineage，不受影响）.
func decodeLineage(raw []byte) map[string]any {
	m, err := decodeObject("lineage", raw)
	if err != nil {
		return nil
	}
	return m
}

// lineageString 取谱系里的非空字符串字段（缺失/空串/类型不符统一报缺）.
func lineageString(lineage map[string]any, key string) (string, bool) {
	v, ok := lineage[key]
	if !ok {
		return "", false
	}
	s, ok := v.(string)
	if !ok || s == "" {
		return "", false
	}
	return s, true
}

// normalizedParams 取公式一的 np：lineage.params.normalized（§2.2.2 证据位），
// 缺省空对象——冻结 writer 对缺参同样记 {}（语义对齐，重算才不误伤）.
func normalizedParams(lineage map[string]any) any {
	params, ok := lineage["params"].(map[string]any)
	if !ok {
		return map[string]any{}
	}
	if np, ok := params["normalized"]; ok {
		return np
	}
	return map[string]any{}
}

// corpusDigests 取公式一的 cd：lineage.corpus_refs[].digest 按引用顺序（顺序
// 是谱系的一部分，进摘要）；缺省空数组（冻结 writer 同缺省），语料引用缺
// digest 即不可证.
func corpusDigests(lineage map[string]any) (any, error) {
	refs, ok := lineage["corpus_refs"].([]any)
	if !ok {
		return []any{}, nil
	}
	digests := make([]any, 0, len(refs))
	for i, ref := range refs {
		m, ok := ref.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: 公式一 corpus_refs[%d] 不是对象", ErrContentDigestUnverifiable, i)
		}
		digest, ok := lineageString(m, "digest")
		if !ok {
			return nil, fmt.Errorf("%w: 公式一 corpus_refs[%d] 缺 digest", ErrContentDigestUnverifiable, i)
		}
		digests = append(digests, digest)
	}
	return digests, nil
}

// decodeJSONB 解码内容块（json.Number 保数字原文——规范化序列化按原文落哈希，
// 浮点重解析即口径漂移）。块不可解释即「地址不可证」：证明不了的地址不发布.
func decodeJSONB(field string, raw []byte) (any, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("%w: %s 为空（账面残缺，无法重算内容地址）", ErrContentDigestUnverifiable, field)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("%w: %s JSON 解码失败: %w", ErrContentDigestUnverifiable, field, err)
	}
	return v, nil
}

// decodeObject 解码必为 JSON object 的列.
func decodeObject(field string, raw []byte) (map[string]any, error) {
	v, err := decodeJSONB(field, raw)
	if err != nil {
		return nil, err
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%w: %s 不是 JSON object", ErrContentDigestUnverifiable, field)
	}
	return m, nil
}
