// orchestrator.go 承载组卷编排层（审计卡 #147）：蓝图编译 → 候选装载 →
// 曝光过滤 → 求解 → 逐题渲染（core/render）→ 试卷制品的一条可执行链。
//
// 分层纪律：本文件是 assembly 包的「装配/编排面」，不是新的求解语义——
// 编译/过滤/求解全部复用既有纯函数核（CompileProfile / Assemble），渲染
// 复用 core/render（ItemToIR / RenderItem / trace_codes）。纯函数核签名
// 零改动；本面只做输入整形与产物组装。
//
// 题源（item_version 候选读取口）是 consumer-side 接口（PaperItemSource）：
// 本包不接 DB；DB 实现走 sqlc 只读 SELECT（db/queries/serving.sql，published
// 池按 学科包×学段 过滤），由装配层注入（api / cmd/papergen）。
//
// paper_id 是确定性内容寻址 id：复用本包既有摘要惯例（AssemblyProfile.Digest
// 同款 canonicalJSON + sha256Hex 口径），载荷为「蓝图摘要 + 入选题序 + 选题
// 指纹」——同蓝图同池内容必得同 paper_id，可回放可审计（D3）。生成时刻只进
// 卷元数据、不进 id（时钟不参与内容寻址）。
//
// trace 码现状（如实声明）：卷头 QR 位图依赖 render.GenerateQRSVG，Go 侧零
// 新依赖约束下尚未实现（render.ErrQRSVGNotImplemented，#152 落地前恒失败）。
// 编排对此 fail-loud：QR payload 恒可计算（纯函数），位图失败时把哨兵错误
// 原文如实写入制品的 QRSlot.Err 并透传到所有出口（HTTP 元数据/CLI stderr/
// 卷面 HTML 注释），绝不吞掉、绝不伪造占位图——#152 实现后同一调用路径
// 自动接通，编排面零改动。
package assembly

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/render"
)

// PaperItemSource 是编排层的题源端口（consumer-side 接口）：按 学科包×学段
// 读 published 候选题，返回 item_version dict 形态（与 DB JSONB 解码形状
// 一致，键面 = item_version_id/item_id/template_version_id/objective/
// interaction_ref/lineage/content）。DB 实现见 core/assembly/paperdb（sqlc
// 只读 SELECT）；测试注入 Memory fake。缺必要块的行由转换面 fail-loud
// （见 servingRowFromDict / CandidateFromServingRow），坏行不许静默混入池。
type PaperItemSource interface {
	LoadPublishedItemVersions(ctx context.Context, packID, gradeband string) ([]map[string]any, error)
}

// PaperBlueprint 组卷蓝图（编排层输入；HTTP body / CLI --blueprint 文件直接
// 按 JSON 解码）。字段面 = CompileInput 的 JSON 投影 + 编排定位字段
// （pack_id / seed / snapshot_ref）：蓝图编译走既有 Profile 面（CompileProfile），
// 本结构不携带任何新约束语义。
type PaperBlueprint struct {
	ProfileID      string `json:"profile_id"`
	ProfileVersion string `json:"profile_version"`
	// Purpose 用途（practice/diagnosis/measurement；CompileProfile 值域）.
	Purpose string `json:"purpose"`
	// Gradeband 学段（L/M/H；同时是题源过滤与候选筛选维度）.
	Gradeband string `json:"gradeband"`
	// PackID 学科包 id（题源过滤维度；核心域只字符串引用，不 import 包）.
	PackID string `json:"pack_id"`
	// KpCodes 本次组卷的知识点范围（Profile 的 KpQuotas 由此展开）.
	KpCodes []string `json:"kp_codes"`
	// Seed 确定性种子（R-Z-01 三要素之一；调用方显式给，缺省按 0 处理）.
	Seed int64 `json:"seed"`
	// SnapshotRef 内容快照引用（确定性留档；空串 = 未提供，如实入档）.
	SnapshotRef string `json:"snapshot_ref"`
	// 四维版本化配置（dict 形态直传 CompileProfile；SubjectOverlay 即学科包
	// assembly-overlays yaml 的解码值树）.
	Base               map[string]any `json:"base"`
	SubjectOverlay     map[string]any `json:"subject_overlay"`
	PurposeOverlay     map[string]any `json:"purpose_overlay"`
	GradebandOverlay   map[string]any `json:"gradeband_overlay"`
	MinItemsPerKp      *int           `json:"min_items_per_kp"`
	AllowItemCountSoft *bool          `json:"allow_item_count_soft"`
}

// ErrInvalidBlueprint 是蓝图非法的编排层哨兵（必填字段缺失/结构性越界）：
// 调用方（HTTP 面）按此映射 422，不用字符串匹配.
var ErrInvalidBlueprint = errors.New("assembly: 组卷蓝图非法")

// ErrInvalidCandidateRow 是候选池行非法的哨兵（身份字段缺失）：池装载期
// fail-loud 的载体——坏行绝不允许静默混入候选池.
var ErrInvalidCandidateRow = errors.New("assembly: 候选池行非法")

// validate 蓝图必填面：编译入口（CompileProfile）会再做用途/学段值域裁决，
// 这里只拦「连编译都不该进」的结构性缺失（身份/定位字段）.
func (b *PaperBlueprint) validate() error {
	if b == nil {
		return fmt.Errorf("%w: 蓝图为空", ErrInvalidBlueprint)
	}
	if b.ProfileID == "" || b.ProfileVersion == "" {
		return fmt.Errorf("%w: profile_id / profile_version 必填", ErrInvalidBlueprint)
	}
	if b.PackID == "" {
		return fmt.Errorf("%w: pack_id 必填（题源过滤维度）", ErrInvalidBlueprint)
	}
	if len(b.KpCodes) == 0 {
		return fmt.Errorf("%w: kp_codes 不能为空（组卷知识点范围）", ErrInvalidBlueprint)
	}
	for _, k := range b.KpCodes {
		if k == "" {
			return fmt.Errorf("%w: kp_codes 含空码", ErrInvalidBlueprint)
		}
	}
	return nil
}

// compileInput 把蓝图整形为既有编译入口的关键字形（零语义新增）.
func (b *PaperBlueprint) compileInput() CompileInput {
	return CompileInput{
		ProfileID:          b.ProfileID,
		ProfileVersion:     b.ProfileVersion,
		Purpose:            b.Purpose,
		Gradeband:          b.Gradeband,
		KpCodes:            append([]string(nil), b.KpCodes...),
		Base:               b.Base,
		SubjectOverlay:     b.SubjectOverlay,
		PurposeOverlay:     b.PurposeOverlay,
		GradebandOverlay:   b.GradebandOverlay,
		MinItemsPerKp:      b.MinItemsPerKp,
		AllowItemCountSoft: b.AllowItemCountSoft,
	}
}

// BlueprintDigest 蓝图摘要（卷元数据留档面）：既有 AssemblyProfile.Digest
// 惯例的编排层复用——Profile 内容指纹 + 种子 + 快照引用一并入摘要，
// 规范化与摘要算法不另造（canonicalJSON + sha256Hex）.
func (b *PaperBlueprint) BlueprintDigest(profile *AssemblyProfile) string {
	return sha256Hex(canonicalJSON(map[string]any{
		"profile":      profile.Digest(),
		"seed":         b.Seed,
		"snapshot_ref": b.SnapshotRef,
	}))
}

// Orchestrator 编排器：题源与曝光账的注入点。Source 为 nil 时 Orchestrate
// 显式失败（绝不返回无题源的伪制品）；Exposure 为 nil 时按空排除集处理
// ——「无曝光账」的如实语义，而非冒充已互斥。
type Orchestrator struct {
	Source   PaperItemSource
	Exposure ExposureQueryStore
}

// OrchestrateOptions 编排运行参（曝光账查询键与生成时刻；均不参与 paper_id）.
type OrchestrateOptions struct {
	// Channel / WeekLabel 静态曝光轨查询键（两者非空且 Exposure 注入时才查）.
	Channel   string
	WeekLabel string
	// Now 生成时刻（卷元数据 GeneratedAt；零值取运行时 UTC）.
	Now time.Time
}

// QRSlot 卷头 QR 槽位（trace 码现状的如实载体，见文件头注释）：
// Payload 恒可计算（GenerateQRPayload 纯函数）；SVG 为 "" 表示位图缺位
// （绝不伪造占位图）；Err 非 "" 时为 render 包哨兵错误的原文——该字段随
// 制品透传到 HTTP 元数据、CLI stderr 与卷面 HTML 注释（fail-loud，不吞错）.
type QRSlot struct {
	Payload string `json:"payload"`
	SVG     string `json:"svg"`
	Err     string `json:"qr_error"`
}

// PaperMetadata 卷元数据（paper 制品的寻址与审计面）.
type PaperMetadata struct {
	PaperID string `json:"paper_id"`
	// BlueprintDigest 蓝图摘要（Profile 指纹+种子+快照引用）.
	BlueprintDigest string `json:"blueprint_digest"`
	// ItemVersionIDs 入选题 id 列表（卷内题序，与 HTML 题号一一对应）.
	ItemVersionIDs []string `json:"item_version_ids"`
	ItemCount      int      `json:"item_count"`
	Purpose        string   `json:"purpose"`
	Gradeband      string   `json:"gradeband"`
	PackID         string   `json:"pack_id"`
	Seed           int64    `json:"seed"`
	SnapshotRef    string   `json:"snapshot_ref"`
	// SelectionDigest 求解器选题指纹（既有 AssemblyResult 字段，透传留档）.
	SelectionDigest string `json:"selection_digest"`
	GeneratedAt     string `json:"generated_at"`
}

// PaperArtifact 试卷制品：卷元数据 + 卷头 QR 槽位 + HTML 字节.
type PaperArtifact struct {
	Metadata PaperMetadata `json:"metadata"`
	QR       QRSlot        `json:"qr"`
	HTML     []byte        `json:"html"`
}

// Orchestrate 组卷编排主链（#147）：编译 → 候选装载 → 曝光过滤 → 求解 →
// 渲染 → 制品。任何环节失败返回 nil 制品 + 原样包装的错误（fail-loud：
// 编译冲突/不可行报告/坏行/渲染失败都不降级、不静默）；唯一例外是卷头 QR
// 位图（render.ErrQRSVGNotImplemented，#152 前恒失败）——按文件头注释如实
// 记入制品 QRSlot.Err 后继续，其余 QR 错误一律硬失败.
func (o *Orchestrator) Orchestrate(ctx context.Context, bp PaperBlueprint, opts OrchestrateOptions) (*PaperArtifact, error) {
	if o == nil || o.Source == nil {
		return nil, fmt.Errorf("assembly: 题源未注入（PaperItemSource 为 nil），拒绝编排")
	}
	if err := bp.validate(); err != nil {
		return nil, err
	}

	// ── 蓝图编译（既有 Profile 面）──
	profile, err := CompileProfile(bp.compileInput())
	if err != nil {
		return nil, fmt.Errorf("assembly: 蓝图编译失败: %w", err)
	}
	digest := bp.BlueprintDigest(profile)

	// ── 候选装载（published 池；坏行 fail-loud）──
	dicts, err := o.Source.LoadPublishedItemVersions(ctx, bp.PackID, bp.Gradeband)
	if err != nil {
		return nil, fmt.Errorf("assembly: 候选池装载失败: %w", err)
	}
	byID := make(map[string]map[string]any, len(dicts))
	candidates := make([]CandidateItem, 0, len(dicts))
	for i, d := range dicts {
		row, err := servingRowFromDict(d)
		if err != nil {
			return nil, fmt.Errorf("assembly: 候选池第 %d 行非法: %w", i, err)
		}
		cand, err := CandidateFromServingRow(row)
		if err != nil {
			return nil, fmt.Errorf("assembly: 候选池第 %d 行（%s）无法规范化: %w", i, row.ItemVersionID, err)
		}
		byID[cand.ItemVersionID] = d
		candidates = append(candidates, cand)
	}

	// ── 曝光过滤（既有 ExposureQueryStore 端口复用；nil = 无账，空排除集）──
	excludedItems, excludedTemplates := IDSet{}, IDSet{}
	if o.Exposure != nil && opts.Channel != "" && opts.WeekLabel != "" {
		excludedItems, err = o.Exposure.QueueExposedItemVersionIDs(ctx, opts.Channel, bp.PackID, opts.WeekLabel)
		if err != nil {
			return nil, fmt.Errorf("assembly: 曝光账查询失败（题轨）: %w", err)
		}
		excludedTemplates, err = o.Exposure.QueueExposedTemplateVersionIDs(ctx, opts.Channel, bp.PackID, opts.WeekLabel)
		if err != nil {
			return nil, fmt.Errorf("assembly: 曝光账查询失败（母题轨）: %w", err)
		}
	}

	// ── 求解（既有 Assemble；不可行按结构化报告原样上抛）──
	result, err := Assemble(profile, candidates, AssembleOptions{
		Seed:                       bp.Seed,
		SnapshotRef:                bp.SnapshotRef,
		ExcludedItemVersionIDs:     excludedItems,
		ExcludedTemplateVersionIDs: excludedTemplates,
	})
	if err != nil {
		return nil, err
	}

	// ── 逐题渲染（core/render：ItemToIR → RenderItem；题号/位置标识由编排分配）──
	itemHTML := make([]string, 0, len(result.Items))
	ids := make([]string, 0, len(result.Items))
	for i := range result.Items {
		cand := &result.Items[i]
		d, ok := byID[cand.ItemVersionID]
		if !ok {
			return nil, fmt.Errorf("assembly: 入选题 %s 在题源中缺内容块（渲染不可继续）", cand.ItemVersionID)
		}
		ir, err := render.ItemToIR(d, render.ItemToIRInput{
			ItemNumber:     strconv.Itoa(i + 1),
			PlacementToken: fmt.Sprintf("q%d", i+1),
		})
		if err != nil {
			return nil, fmt.Errorf("assembly: 题 %s IR 转换失败: %w", cand.ItemVersionID, err)
		}
		html, err := render.RenderItem(ir)
		if err != nil {
			return nil, fmt.Errorf("assembly: 题 %s HTML 渲染失败: %w", cand.ItemVersionID, err)
		}
		itemHTML = append(itemHTML, html)
		ids = append(ids, cand.ItemVersionID)
	}

	// ── paper_id：确定性内容寻址（本包摘要惯例，见文件头注释）──
	paperID := sha256Hex(canonicalJSON(map[string]any{
		"blueprint_digest": digest,
		"items":            idsToAny(ids),
		"selection":        result.SelectionDigest,
	}))

	// ── 卷头 QR（trace 码；现状 fail-loud，见文件头注释）──
	payload, err := render.GenerateQRPayload(paperID)
	if err != nil {
		return nil, fmt.Errorf("assembly: 卷头 QR payload 计算失败: %w", err)
	}
	qr := QRSlot{Payload: payload}
	svg, err := render.GenerateQRSVG(payload, 4, 2)
	switch {
	case err == nil:
		qr.SVG = svg
	case errors.Is(err, render.ErrQRSVGNotImplemented):
		// #152 前的既定现状：哨兵原文如实入档（不吞不伪造），HTML 卷头留
		// 注释槽位；#152 实现后本路径自动产出位图，编排面零改动.
		qr.Err = err.Error()
	default:
		return nil, fmt.Errorf("assembly: 卷头 QR 位图生成失败: %w", err)
	}

	now := opts.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}

	return &PaperArtifact{
		Metadata: PaperMetadata{
			PaperID:         paperID,
			BlueprintDigest: digest,
			ItemVersionIDs:  ids,
			ItemCount:       len(ids),
			Purpose:         profile.Purpose,
			Gradeband:       profile.Gradeband,
			PackID:          bp.PackID,
			Seed:            bp.Seed,
			SnapshotRef:     bp.SnapshotRef,
			SelectionDigest: result.SelectionDigest,
			GeneratedAt:     now.UTC().Format(time.RFC3339),
		},
		QR:   qr,
		HTML: pageHTML(&bp, profile, paperID, qr, itemHTML),
	}, nil
}

// pageHTML 组装卷页 HTML（编排层的最小页面包装——页面级模板装配是 render
// 域的既定非目标，编排面只做制品级拼装）：卷头（paper_id + 蓝图摘要 + QR
// 槽位）+ 题序片段。编排侧动态值均为包内生成的摘要/计数（非用户自由文本），
// 仍经 render.EscapeText 转义以与 render 域保持同一安全口径.
func pageHTML(bp *PaperBlueprint, profile *AssemblyProfile, paperID string, qr QRSlot, items []string) []byte {
	var b strings.Builder
	b.WriteString("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n<title>")
	b.WriteString(render.EscapeText(bp.ProfileID))
	b.WriteString("</title>\n</head>\n<body>\n")
	b.WriteString("<header class=\"paper-header\" data-paper-id=\"")
	b.WriteString(render.EscapeText(paperID))
	b.WriteString("\" data-blueprint-digest=\"")
	b.WriteString(render.EscapeText(bp.BlueprintDigest(profile)))
	b.WriteString("\">\n<div class=\"paper-code\">")
	b.WriteString(render.EscapeText(paperID))
	b.WriteString("</div>\n")
	if qr.SVG != "" {
		b.WriteString("<div class=\"paper-qr\">")
		b.WriteString(qr.SVG)
		b.WriteString("</div>\n")
	} else {
		// QR 缺位：错误原文如实注释入卷（人类可读的缺位声明），不伪造位图.
		b.WriteString("<!-- paper-qr unavailable: ")
		b.WriteString(render.EscapeText(qr.Err))
		b.WriteString(" -->\n")
	}
	b.WriteString("<div class=\"paper-meta\">purpose=")
	b.WriteString(render.EscapeText(profile.Purpose))
	b.WriteString(" gradeband=")
	b.WriteString(render.EscapeText(profile.Gradeband))
	b.WriteString(" seed=")
	b.WriteString(strconv.FormatInt(bp.Seed, 10))
	b.WriteString(" snapshot_ref=")
	b.WriteString(render.EscapeText(bp.SnapshotRef))
	b.WriteString("</div>\n</header>\n<main class=\"paper-items\">\n")
	for _, h := range items {
		b.WriteString(h)
		b.WriteByte('\n')
	}
	b.WriteString("</main>\n</body>\n</html>\n")
	return []byte(b.String())
}

func idsToAny(ids []string) []any {
	out := make([]any, 0, len(ids))
	for _, id := range ids {
		out = append(out, id)
	}
	return out
}

// servingRowFromDict 把 item_version dict 整形为既有 ServingRow（候选规范化
// 的入口形状）：必填身份字段缺失即错误——坏行在池装载期 fail-loud.
func servingRowFromDict(d map[string]any) (ServingRow, error) {
	ivID, _ := d["item_version_id"].(string)
	itemID, _ := d["item_id"].(string)
	if ivID == "" || itemID == "" {
		return ServingRow{}, fmt.Errorf("%w: 缺 item_version_id / item_id", ErrInvalidCandidateRow)
	}
	tvID, _ := d["template_version_id"].(string)
	obj, _ := d["objective"].(map[string]any)
	ref, _ := d["interaction_ref"].(map[string]any)
	lin, _ := d["lineage"].(map[string]any)
	return ServingRow{
		ItemVersionID:     ivID,
		ItemID:            itemID,
		TemplateVersionID: tvID,
		Objective:         obj,
		InteractionRef:    ref,
		Lineage:           lin,
	}, nil
}
