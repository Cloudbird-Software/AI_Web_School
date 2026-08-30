// ingest.go —— 内容入账链路内核（审计卡 #156）：mathgen JSONL → 摘要对表 →
// 校验门 → 同一显式事务入账（item/item_version/gate_certificate/gate_run →
// core/content PublishService）→ COMMIT 归最外层。
//
// 事务与留痕纪律（铁律 9 / D11）：
//   - 一次业务写入 = 一个事务：主事务内的全部 INSERT 与 Publish 同进同退，
//     COMMIT/ROLLBACK 只由本 cmd（最外层）执行；
//   - 门不过不入账：主事务回滚，失败走 gate.NewFailureTrail 独立事务留痕
//     （独立事务在主事务回滚后仍存活——失败也是账面事实）；
//   - 写入服务不绕行：发布必须经 core/content.NewPublishService(tx).Publish
//     （持证 + 内容寻址对表 + 状态前移 + 签发账 + 指针前移五段全走）。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/content"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate"
	"github.com/Cloudbird-Software/AI_Web_School/core/models"
	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// options 是一次 ingest 运行的装配参数（main.go flag 解析产物）。
type options struct {
	input         string // JSONL 文件或目录（目录取其中 *.jsonl）
	packID        string // item.pack_id（ground truth：subject-math overlay pack_id）
	packDigest    string // 公式一 pd（仓库无学科包摘要真源，显式 flag 传入）
	engineDigest  string // 公式一 ed（缺省 core/instantiation.EngineDigest）
	policyVersion string // 门策略版本（gate_certificate/gate_run/failure 共用语境）
	issuedBy      string // 门证书签发人 / 发布人 id
	operator      string // lineage.operator（谁执行的入账操作）
	registriesDir string // 注册表契约目录（interaction.yaml / scorer.yaml）
}

// Runner 是绑定好装配依赖的入账执行体（pool 抽象为 Connect 接口以便
// 无 DB 测试注入 fake；idGen/now 同理注入，测试断言账行 id 形态）。
type Runner struct {
	opts         options
	interactions contractIDs
	scorers      contractIDs
	begin        func(context.Context) (pgx.Tx, error)
	now          func() time.Time
	newID        func() (string, error)
}

// Connect 是 Runner 需要的最小连接面（pgxpool.Pool 天然满足）。
type Connect interface {
	Begin(ctx context.Context) (pgx.Tx, error)
}

// NewRunner 装配执行体；注册表 id 集由调用方在启动期加载（fail-fast）。
func NewRunner(opts options, db Connect, interactions, scorers contractIDs) *Runner {
	return &Runner{
		opts:         opts,
		interactions: interactions,
		scorers:      scorers,
		begin:        db.Begin,
		now:          func() time.Time { return time.Now().UTC() },
		newID:        newULID,
	}
}

// verdict is the per-record outcome label（summary 计数与原因分布的键面）。
const (
	outcomeAccepted = "accepted"
	outcomeRejected = "rejected"
	outcomeDecoded  = "decode-error"
)

// ingestRecord 处理一行 JSONL，返回 (结果类别, 拒因, 硬错误)。
//   - 硬错误非 nil：运行期故障（驱动/留痕账写失败），调用方应中止整批——
//     继续跑会在故障基础设施上继续产出失败，账面反而失真；
//   - 硬错误为 nil：记录级结局（accepted/rejected/decode-error），继续下一条。
func (rn *Runner) ingestRecord(ctx context.Context, line []byte) (string, string, error) {
	rec, err := decodeRecord(line)
	if err != nil {
		return outcomeDecoded, "decode:" + err.Error(), nil
	}

	// 谱系补全（审计卡点名键 + 公式一证据链键），后续检查与入账同源。
	lineage := buildLineage(rec, rn.opts)

	// ── 校验门（先判后写：判定为纯函数，不烧事务语句）──────────────────
	checks := []checkResult{
		digestCheck(rec, lineage, rn.opts),
		registryCheck(rec, rn.interactions, rn.scorers),
	}
	var failed []checkResult
	for _, c := range checks {
		if c.Verdict != "pass" {
			failed = append(failed, c)
		}
	}

	// item_version_id：digest 验证器第③段的重算产物（公式一，内容寻址）。
	ivid := checks[0].ItemVersionID

	// ── 主事务：门过才写；不过则回滚 + 独立事务留痕 ────────────────────
	tx, err := rn.begin(ctx)
	if err != nil {
		return "", "", fmt.Errorf("ingest: begin 主事务失败: %w", err)
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback(ctx) // COMMIT 后的兜底 rollback 是 pgx 的 no-op
		}
	}()

	if len(failed) > 0 {
		if err := tx.Rollback(ctx); err != nil {
			return "", "", fmt.Errorf("ingest: 回滚门失败记录的主事务失败: %w", err)
		}
		if err := rn.recordFailures(ctx, ivid, rec.ContentDigest, failed); err != nil {
			return "", "", err
		}
		return outcomeRejected, rejectReason(failed), nil
	}

	if err := rn.writeLedger(ctx, tx, rec, lineage, ivid, checks); err != nil {
		if isDuplicate(err) {
			// 重放同批：内容寻址 id 已在账（PK 冲突），幂等屏障显形为
			// 拒绝计数——不是门失败，不走 failure trail。
			return outcomeRejected, "already-ingested", nil
		}
		return "", "", fmt.Errorf("ingest: 入账写入失败: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return "", "", fmt.Errorf("ingest: 提交主事务失败: %w", err)
	}
	committed = true
	return outcomeAccepted, "", nil
}

// writeLedger 在外层显式事务内完成一次完整入账（不做 COMMIT——提交归调用方）：
//
//	母题身份/版本行 → item（A 级自引用） → item_version(draft, 六块+谱系)
//	→ gate_certificate → gate_run×N → PublishService.Publish（状态前移+
//	签发账+指针前移，复用同一证书验真）。
func (rn *Runner) writeLedger(ctx context.Context, tx pgx.Tx, rec *subjectmath.Record, lineage map[string]any, ivid string, checks []checkResult) error {
	qs := dbgen.New(tx)
	now := rn.now()

	// 母题身份与版本行就位（item.template_version_id 的非延迟外键要求版本行
	// 先于实例行；spec 取自 pack 注册表——digest 验证器已对表过版本号）。
	g, ok := subjectmath.Get(rec.TemplateID)
	if !ok {
		return fmt.Errorf("母题 %q 不在 packs/subjectmath 注册表（writeLedger 前置门失效）", rec.TemplateID)
	}
	specJSON, err := jsonb("spec", g.Spec())
	if err != nil {
		return err
	}
	if err := qs.UpsertItemTemplate(ctx, dbgen.UpsertItemTemplateParams{
		TemplateID: rec.TemplateID,
		PackID:     rn.opts.packID,
	}); err != nil {
		return fmt.Errorf("upsert item_template: %w", err)
	}
	if err := qs.UpsertItemTemplateVersion(ctx, dbgen.UpsertItemTemplateVersionParams{
		TemplateVersionID: rec.TemplateVersionID,
		TemplateID:        rec.TemplateID,
		DslVersion:        "1",
		Spec:              specJSON,
		Status:            dbgen.ItemTemplateVersionStatusEnumDraft,
	}); err != nil {
		return fmt.Errorf("upsert item_template_version: %w", err)
	}

	// item：A/B 级 item_id = item_version_id（自引用，冻结 engine.py 地面真值）。
	if err := qs.InsertItem(ctx, dbgen.InsertItemParams{
		ItemID:            ivid,
		PackID:            rn.opts.packID,
		Tier:              dbgen.ItemTierEnumA,
		TemplateVersionID: pgtext(rec.TemplateVersionID),
	}); err != nil {
		return fmt.Errorf("insert item: %w", err)
	}

	// item_version：六块 JSONB + 补全谱系（draft 入账，published 由 Publish 前移）。
	objective, err := jsonb("objective", rec.Objective)
	if err != nil {
		return err
	}
	interactionRef, err := jsonb("interaction_ref", rec.InteractionRef)
	if err != nil {
		return err
	}
	contentJSON, err := jsonb("content", rec.Content)
	if err != nil {
		return err
	}
	scoringRef, err := jsonb("scoring_ref", rec.ScoringRef)
	if err != nil {
		return err
	}
	errorBindings, err := jsonb("error_bindings", rec.ErrorBindings)
	if err != nil {
		return err
	}
	lineageJSON, err := jsonb("lineage", lineage)
	if err != nil {
		return err
	}
	// 渲染快照（#170/#171 联动）：发布前提 ErrRenderedSnapshotMissing 的供给
	// 面。mathgen 各块的 rendered 文本即生成器侧渲染面，按块序确定性拼装；
	// 深度 ItemToIR 接线属渲染编排波次（#147/#152）。
	snapshot, err := jsonb("rendered_snapshot", renderedSnapshot(rec.Content))
	if err != nil {
		return err
	}
	if err := qs.InsertItemVersion(ctx, dbgen.InsertItemVersionParams{
		ItemVersionID:    ivid,
		ItemID:           ivid,
		Status:           dbgen.ItemVersionStatusEnumDraft,
		Objective:        objective,
		InteractionRef:   interactionRef,
		Content:          contentJSON,
		ScoringRef:       scoringRef,
		ErrorBindings:    errorBindings,
		Lineage:          lineageJSON,
		RenderedSnapshot: snapshot,
	}); err != nil {
		return fmt.Errorf("insert item_version: %w", err)
	}

	// 门证书 + 每 validator 一行 gate_run（cert 先于 run：fk_gr_certificate
	// 非延迟；cert_id = "cert_"+ULID，冻结 certifier 地面真值形态）。
	certULID, err := rn.newID()
	if err != nil {
		return fmt.Errorf("生成 cert_id: %w", err)
	}
	certID := "cert_" + certULID
	if err := qs.InsertGateCertificate(ctx, dbgen.InsertGateCertificateParams{
		CertID:        certID,
		ArtifactRef:   ivid,
		CertType:      "publish",
		PolicyVersion: rn.opts.policyVersion,
		IssuedBy:      rn.opts.issuedBy,
		IssuedAt:      pgts(now),
	}); err != nil {
		return fmt.Errorf("insert gate_certificate: %w", err)
	}
	for _, c := range checks {
		runULID, err := rn.newID()
		if err != nil {
			return fmt.Errorf("生成 run_id: %w", err)
		}
		evidence, err := jsonb("evidence", c.Evidence)
		if err != nil {
			return err
		}
		if err := qs.InsertGateRun(ctx, dbgen.InsertGateRunParams{
			RunID:            "run_" + runULID,
			CertificateID:    pgtext(certID),
			PolicyVersion:    rn.opts.policyVersion,
			ValidatorID:      c.ValidatorID,
			ValidatorVersion: c.ValidatorVersion,
			Verdict:          dbgen.GateRunVerdictEnum(c.Verdict),
			Evidence:         evidence,
			Confidence:       confidenceCertain(),
			CostMs:           c.CostMs,
			CostTokens:       0, // 确定性路径零 LLM 调用，无 token 成本
			RunAt:            pgts(now),
		}); err != nil {
			return fmt.Errorf("insert gate_run(%s): %w", c.ValidatorID, err)
		}
	}

	// 发布事务（状态前移 + 签发账 + 指针前移，同事务内证书验真与内容寻址
	// 对表二次把关）。publication_id = "pub_"+ULID，冻结 publication.py 真值。
	pubULID, err := rn.newID()
	if err != nil {
		return fmt.Errorf("生成 publication_id: %w", err)
	}
	if _, err := content.NewPublishService(tx).Publish(ctx, content.PublishRequest{
		PublicationID:     "pub_" + pubULID,
		ItemVersionID:     ivid,
		GateCertificateID: certID,
		PublishedBy:       rn.opts.issuedBy,
		Locale:            rec.Locale,
		PublishedAt:       now,
	}); err != nil {
		return fmt.Errorf("publish %s: %w", ivid, err)
	}
	return nil
}

// recordFailures 把门失败写进门失败留痕账（独立事务：主事务已回滚，失败事实
// 必须跨其存活——铁律 9 审计副作用纪律）。任一条留痕失败即硬错误（留痕账
// 缺口的入账批次不可信，宁可中止整批也不静默丢单条失败证据）。
func (rn *Runner) recordFailures(ctx context.Context, ivid, contentDigest string, failed []checkResult) error {
	artifactRef := ivid
	if artifactRef == "" {
		// digest 验证器未能算出公式一 id（如 locale 缺失）：退锚内容摘要——
		// 留痕必须有稳定引用，content_digest 是该记录的确定性身份。
		artifactRef = contentDigest
	}
	ftx, err := rn.begin(ctx)
	if err != nil {
		return fmt.Errorf("ingest: begin 失败留痕事务失败: %w", err)
	}
	committed := false
	defer func() {
		if !committed {
			_ = ftx.Rollback(ctx)
		}
	}()
	trail := gate.NewFailureTrail(ftx)
	now := rn.now()
	for _, c := range failed {
		fid, err := rn.newID()
		if err != nil {
			return fmt.Errorf("生成 failure_id: %w", err)
		}
		if _, err := trail.Record(ctx, gate.FailureInput{
			FailureID:        "fail_" + fid,
			ArtifactType:     gate.ArtifactItem,
			ArtifactRef:      artifactRef,
			ValidatorID:      c.ValidatorID,
			ValidatorVersion: c.ValidatorVersion,
			PolicyVersion:    rn.opts.policyVersion,
			Reason:           failReason(c),
			Evidence:         c.Evidence,
			FailedAt:         now,
		}); err != nil {
			return fmt.Errorf("ingest: 门失败留痕失败（validator=%s）: %w", c.ValidatorID, err)
		}
	}
	if err := ftx.Commit(ctx); err != nil {
		return fmt.Errorf("ingest: 提交失败留痕事务失败: %w", err)
	}
	committed = true
	return nil
}

// decodeRecord 以 UseNumber 解码一行 JSONL（json.Number 保数字原文——摘要
// 与公式一都按原文进哈希，float64 重解析即口径漂移）。
func decodeRecord(line []byte) (*subjectmath.Record, error) {
	dec := json.NewDecoder(bytes.NewReader(line))
	dec.UseNumber()
	rec := &subjectmath.Record{}
	if err := dec.Decode(rec); err != nil {
		return nil, err
	}
	if rec.Instance == nil || rec.TemplateID == "" || rec.ContentDigest == "" {
		return nil, errors.New("行缺少 Instance/TemplateID/ContentDigest 必填字段")
	}
	if rec.Locale == "" {
		return nil, errors.New("行缺 locale（公式一 l 输入必填）")
	}
	return rec, nil
}

// buildLineage 在实例谱系上补全审计卡点名键与公式一证据链键，返回谱系 map
// （原地补全：记录属本进程私有，发布侧重算公式一只读这几个键）。
//   - template_id / source / operator / pack_id / corpus_version_id：审计卡点名；
//   - pack_digest / engine_digest：公式一 pd/ed 证据链（publish 侧重算必读）；
//   - content_digest：H-W6-1 结构互异审计的判定对象（库内无独立摘要列，
//     谱系即其账面落点——dup 验证器 W6 DB 适配的消费键）。
func buildLineage(rec *subjectmath.Record, opts options) map[string]any {
	lineage := rec.Lineage
	lineage["template_id"] = rec.TemplateID
	lineage["source"] = pipelineID(lineage)
	lineage["operator"] = opts.operator
	lineage["pack_id"] = opts.packID
	lineage["corpus_version_id"] = firstCorpusVersionID(lineage)
	lineage["pack_digest"] = opts.packDigest
	lineage["engine_digest"] = opts.engineDigest
	lineage["content_digest"] = rec.ContentDigest
	return lineage
}

// computeInstanceVersionID 重算公式一（core/models.ComputeInstanceID，冻结
// compute_instance_id 的 Go 唯一实现）：np 取谱系 params.normalized，cd 取
// corpus_refs[].digest 按引用顺序——与 core/content 发布侧重算同源同缺省。
func computeInstanceVersionID(rec *subjectmath.Record, lineage map[string]any, opts options) (string, error) {
	corpus, err := corpusDigests(lineage)
	if err != nil {
		return "", err
	}
	return models.ComputeInstanceID(
		rec.TemplateVersionID, normalizedParams(lineage),
		opts.packDigest, opts.engineDigest, corpus, rec.Locale)
}

// normalizedParams 取公式一 np（lineage.params.normalized；缺省空对象，
// 与 core/content.normalizedParams 同缺省——两侧重算才不误伤）。
func normalizedParams(lineage map[string]any) map[string]any {
	params, ok := lineage["params"].(map[string]any)
	if !ok {
		return map[string]any{}
	}
	if np, ok := params["normalized"].(map[string]any); ok {
		return np
	}
	return map[string]any{}
}

// corpusDigests 取公式一 cd：corpus_refs[].digest 按引用顺序（顺序是谱系的
// 一部分）；引用缺 digest 即不可证（发布侧同语义，拒绝入账）。
func corpusDigests(lineage map[string]any) ([]string, error) {
	refs, ok := lineage["corpus_refs"].([]any)
	if !ok {
		return nil, nil
	}
	digests := make([]string, 0, len(refs))
	for i, ref := range refs {
		m, ok := ref.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("corpus_refs[%d] 不是对象", i)
		}
		d, _ := m["digest"].(string)
		if d == "" {
			return nil, fmt.Errorf("corpus_refs[%d] 缺 digest", i)
		}
		digests = append(digests, d)
	}
	return digests, nil
}

// firstCorpusVersionID 取首个语料引用 id（审计卡点名键）；无数料引用为 nil
// （JSON null：数学轮确定性路径不引用语料，null 是事实不是缺账）。
func firstCorpusVersionID(lineage map[string]any) any {
	refs, ok := lineage["corpus_refs"].([]any)
	if !ok || len(refs) == 0 {
		return nil
	}
	if m, ok := refs[0].(map[string]any); ok {
		if id, ok := m["corpus_version_id"].(string); ok && id != "" {
			return id
		}
	}
	return nil
}

// pipelineID 取谱系声明生产线 id（lineage.source 的取值：来源=生产线本身）。
func pipelineID(lineage map[string]any) string {
	if p, ok := lineage["pipeline"].(map[string]any); ok {
		if id, ok := p["id"].(string); ok && id != "" {
			return id
		}
	}
	return "subjectmath-mathgen"
}

// rejectReason 汇总门失败为稳定拒因键（"digest:fail/registry:fail" 形态，
// 汇总分布按此计数）。
func rejectReason(failed []checkResult) string {
	parts := make([]string, 0, len(failed))
	for _, c := range failed {
		parts = append(parts, c.ValidatorID+":fail")
	}
	return strings.Join(parts, ",")
}

// failReason 取单条门失败的人读主因（落 gate_failure.reason 必填面）。
func failReason(c checkResult) string {
	if r, ok := c.Evidence["fail_reason"].(string); ok && r != "" {
		return r
	}
	return "verdict " + c.Verdict
}

// isDuplicate 判定驱动错误是否唯一约束冲突（重放同批的幂等屏障信号）。
func isDuplicate(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}

// jsonb 序列化 JSONB 字段：SetEscapeHTML(false) 与 Python ensure_ascii=False
// 同向（Unicode 按原文落库，审账直读）；UseNumber 解出的 json.Number 按原文
// 进 JSON——与摘要口径（数字原文进哈希）一致，零精度漂移。
func jsonb(field string, v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, fmt.Errorf("ingest: %s JSON 序列化失败: %w", field, err)
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// pgts/pgtext 收敛 pgtype 可空列构造（本文件内语句参数的统一形态）。
func pgts(t time.Time) pgtype.Timestamptz { return pgtype.Timestamptz{Time: t, Valid: true} }
func pgtext(s string) pgtype.Text         { return pgtype.Text{String: s, Valid: s != ""} }

// renderedSnapshot 从 content.blocks 的 rendered 文本拼装渲染快照（确定性：
// 块序即文档序；文本 HTML 转义防注入——快照是发布时的渲染面存档）.
func renderedSnapshot(content map[string]any) map[string]any {
	var sb strings.Builder
	sb.WriteString(`<div class="item-rendered">`)
	if blocks, ok := content["blocks"].([]any); ok {
		for _, b := range blocks {
			blk, ok := b.(map[string]any)
			if !ok {
				continue
			}
			rendered, _ := blk["rendered"].(string)
			if rendered == "" {
				text, _ := blk["text"].(string)
				rendered = text
			}
			if rendered == "" {
				continue
			}
			sb.WriteString("<p>")
			sb.WriteString(html.EscapeString(rendered))
			sb.WriteString("</p>")
		}
	}
	sb.WriteString("</div>")
	return map[string]any{"html": sb.String()}
}
