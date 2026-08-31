// checks.go —— 数学 A 档确定性路径的门侧验证器（cmd/ingest 专用）。
//
// 冻结基线：src/core/gate/orchestrator.py 的 run_gate 对每 validator 产出
// verdict/evidence/confidence/cost 并落 gate_run。本包复刻该输出面（确定性
// 路径只有 pass/fail 两值：可证即 pass，不可证即 fail——不产 review，因为
// 编排器对 review 从不放行，确定性输入上引入第三值只会虚耗复核）。
//
// 两类验证器（审计卡 #156 点名的数学 A 档最小集）：
//   - digest   内容摘要重算对表：① packs 既有 ContentDigest 重算 content
//     摘要与 JSONL content_digest 对表（禁止私造第二套规范化）；
//     ② 母题版本号对表（pack 注册表 spec 规范化摘要）；
//     ③ 公式一重算 item_version_id（tvd/np/pd/ed/cd/l）。
//   - registry 注册核查：interaction_id / scorer_id 必须是平台注册表
//     （specs/contracts/registries/*.yaml）现役条目（铁律 3/D4）。
//
// 为什么不进 core/gate/validators：本卡禁改 core/**；且这两类判定依赖
// subjectmath 包的规范化函数与契约 YAML——core 禁 import 学科包（X6），
// 装配层的学科确定性检查只能住 cmd/。
package main

import (
	"fmt"
	"math/big"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
	"github.com/jackc/pgx/v5/pgtype"
)

// 验证器注册身份（validator_id/validator_version，落 gate_run 两列）。
// 数学 A 档确定性路径为最小实现，版本从 1.0.0 起步；演进只增不改。
const (
	digestValidatorID      = "digest"
	digestValidatorVersion = "1.0.0"
	registryValidatorID    = "registry"
	registryValidatorVer   = "1.0.0"
)

// checkResult 是一次验证器运行的落库投影（gate_run 一行的应用侧形态）。
type checkResult struct {
	ValidatorID      string
	ValidatorVersion string
	Verdict          string // pass / fail（gate_run_verdict_enum 值域子集）
	Evidence         map[string]any
	CostMs           int32
	// ItemVersionID 是 digest 验证器第③段算出的公式一 id（ingest 拿它作为
	// item_version/item/gate_certificate 的 artifact 身份）；其他验证器为空。
	ItemVersionID string
}

// fail 把判定翻转为 fail 并记录人读原因（机器细节进 evidence 键）。
func (r *checkResult) fail(reason string) {
	r.Verdict = "fail"
	r.Evidence["fail_reason"] = reason
}

// confidenceCertain 是确定性路径的置信度 NUMERIC(4,3)=1.000：
// 整数运算构造（1000 × 10^-3），不经 float 解析（数值精度在 pgtype.Numeric
// 的 Int/Exp 表示下无损）。
func confidenceCertain() pgtype.Numeric {
	return pgtype.Numeric{Int: big.NewInt(1000), Exp: -3, Valid: true}
}

// digestCheck 执行内容摘要重算对表（三段全部可证才 pass；第一段失败不
// 短路后两段——evidence 一次带全三段实况，审账不必重放就知道断在哪）。
//
// rec 必须是 UseNumber 解码的 JSONL 行（json.Number 保数字原文，浮点重解析
// 即口径漂移）；lineage 是 buildLineage 补全谱系键后的实例谱系；binding 是
// 按模板 id 前缀解析的学科包绑定（P0-2：摘要口径与模板注册表按包分派）。
func digestCheck(rec *subjectmath.Record, lineage map[string]any, opts options, binding *packBinding) checkResult {
	start := time.Now()
	r := checkResult{
		ValidatorID:      digestValidatorID,
		ValidatorVersion: digestValidatorVersion,
		Verdict:          "pass",
		Evidence:         map[string]any{},
	}

	// ① content 摘要对表：packs 唯一口径（学科包既有 InstanceDigest——数学轮
	//    content-only、语英轮 {template_id, content, scoring_ref} 三字段，同轴
	//    管线 issue #34 §二），重算 ≠ JSONL 声明即内容被篡改/口径漂移，拒绝入账。
	recomputed, err := binding.Digest(rec)
	if err != nil {
		r.fail("内容摘要不可计算: " + err.Error())
		r.evidenceClose(start)
		return r
	}
	r.Evidence["content_digest_declared"] = rec.ContentDigest
	r.Evidence["content_digest_recomputed"] = recomputed
	if recomputed != rec.ContentDigest {
		r.fail("content 摘要与 JSONL 声明不一致（重算 ≠ content_digest）")
	}

	// ② 母题版本号对表：instance.template_version_id 必须等于包注册表
	//    spec 的规范化摘要（mustTemplateVersionID 同输入同函数同口径）；
	//    模板未注册即生成侧私造，拒绝。
	g, ok := binding.Templates[rec.TemplateID]
	if !ok {
		r.fail(fmt.Sprintf("母题 %q 不在学科包 %s 注册表（私造模板禁止入账）", rec.TemplateID, binding.PackID))
		r.evidenceClose(start)
		return r
	}
	tvdRecomputed, err := subjectmath.DigestAny(map[string]any{"dsl_version": "1", "spec": g.Spec()})
	if err != nil {
		r.fail("母题 spec 摘要不可计算: " + err.Error())
		r.evidenceClose(start)
		return r
	}
	r.Evidence["template_version_id_declared"] = rec.TemplateVersionID
	r.Evidence["template_version_id_recomputed"] = tvdRecomputed
	if tvdRecomputed != rec.TemplateVersionID {
		r.fail("母题版本号与 spec 摘要不一致（template_version_id 脱钩）")
	}

	// ③ 公式一重算：item_version_id = H(tvd, np, pd, ed, cd, l)。与发布事务
	//    （core/content verifyContentAddress）同一公式同一证据链，缺参即拒。
	ivid, err := computeInstanceVersionID(rec, lineage, opts)
	if err != nil {
		r.fail("公式一 item_version_id 计算失败: " + err.Error())
	} else {
		r.Evidence["item_version_id"] = ivid
		r.ItemVersionID = ivid
	}
	r.evidenceClose(start)
	return r
}

// registryCheck 执行注册核查：interaction_ref.interaction_id 与
// scoring_ref.scorer_id 必须分别是交互/评分注册表的现役条目。
func registryCheck(rec *subjectmath.Record, interactions, scorers contractIDs) checkResult {
	start := time.Now()
	r := checkResult{
		ValidatorID:      registryValidatorID,
		ValidatorVersion: registryValidatorVer,
		Verdict:          "pass",
		Evidence:         map[string]any{},
	}

	interactionID, _ := rec.InteractionRef["interaction_id"].(string)
	scorerID, _ := rec.ScoringRef["scorer_id"].(string)
	r.Evidence["interaction_id"] = interactionID
	r.Evidence["scorer_id"] = scorerID
	if interactionID == "" {
		r.fail("interaction_ref 缺 interaction_id（注册核查无从谈起）")
	} else if !interactions.isActive(interactionID) {
		r.fail(fmt.Sprintf("interaction_id %q 不是交互注册表现役条目", interactionID))
	}
	if scorerID == "" {
		r.fail("scoring_ref 缺 scorer_id（注册核查无从谈起）")
	} else if !scorers.isActive(scorerID) {
		r.fail(fmt.Sprintf("scorer_id %q 不是评分注册表现役条目", scorerID))
	}
	r.evidenceClose(start)
	return r
}

// evidenceClose 收口计时（cost_ms 落 gate_run；整数毫秒，不足 1ms 记 0）。
func (r *checkResult) evidenceClose(start time.Time) {
	r.CostMs = int32(time.Since(start).Milliseconds())
}
