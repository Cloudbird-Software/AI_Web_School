// Package ai 承载 AI 总线核心域：一切生成式调用经总线、落台账
// （模型+版本+prompt 版本+成本+产物 id），PII 先剥离、剥离失败 fail-closed
// （宪法 A6/D9/D10；T-W5-014/015）。
//
// prompt 层使用 BAML（ADR-0004 D-C）；本包只做总线与台账，不含 prompt 定义。
// 出站执行面以 Caller 接口注入（生产装配方包装 baml_client 函数，本包不直接
// import baml_client——BAML 函数签名演进被隔离在装配层）。
//
// 本包落地面（T-W5-014，D10/X12 的 Go 重锚定）：
//   - bus.go     总线唯一入口 Call：目标 allowlist → PII 剥离门 → 预算门 →
//     出站 → 同步落账；三条失败路径全部拒绝且各自留账；
//   - redact.go  正则剥离器（D7），与冻结实现 src/core/ai/ledger/pii_filter.py
//     语义逐条对齐；
//   - cost.go    模型单价表 / prompt 哈希 / 兜底 token 计数（冻结实现
//     ledger.py compute_cost_cny 口径一致）；
//   - budget.go  累计预算硬顶接口（W6 成本核算的前置骨架）;
//   - ledger.go / ledger_pg.go 台账接口 + 内存与 PG 双实现（0026 ai_call_ledger，
//     append-only 由 DB 触发器物理强制）。
//   - tts/      TTS 合成统一服务面（T-W5-015）：TTSRequest 经 Bus.Call
//     （ModalityTTS，PII 剥离 fail-closed 在总线内）、完整内容寻址产物 id +
//     定容 LRU 缓存、台账 payload 加性键对齐。
package ai
