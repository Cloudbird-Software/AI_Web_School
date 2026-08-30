// Package production 是生产线域的 Go 移植（PyR 波；Python 冻结语义基准
// src/core/production/ 全量，逐函数对齐）。
//
// 架构 v2 §4.1 生产线四条线统一 ItemVersion、共用同一入库服务/校验门/
// 证据链（宪法 A7）：
//   - A 线：母题 DSL + 实例化引擎（Go 面另行波次，经 Generator 端口注入）；
//   - B 线：框架模板 + 语料库填充 → ItemVersion draft（b_assembler.go，
//     公式二内容寻址）；
//   - C 线：人工创作（另行波次）；
//   - D 线：命题蓝图 → 开放式题 → 量规评分（blueprint.go / rubric_template.go /
//     d_pipeline.go）。
//
// 本包是纯逻辑层：不接 DB。蓝图注册表是 Memory 面（BlueprintRegistry，
// 供测试与早期集成注入；蓝图 DB 持久化由教研后台波次接线）；生成与入库经
// 端口注入（Generator / ItemSink）——A 线实例化引擎与 content writer 的
// Go 接线属后续波次，阶段间产物显式传递、任一环节失败 fail-loud。
//
// D3：B 线产物走公式二（compute_canonical_item_version_id），同
// (template, corpus_refs, params, locale) 必得同一 item_version_id——
// canon.go 按 Python json.dumps(sort_keys=True, ensure_ascii=False,
// separators=(",",":")) 规则规范化，跨实现指纹逐字节可比。
//
// 宪法 A5/A7/X6：本包不 import 任何学科包/学段包（tools/go-lint/
// import-boundary 强制）；学科/模板差异经端口注入，核心域零学科特判。
package production
