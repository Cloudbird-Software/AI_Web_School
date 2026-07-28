"""核心域·AI 能力总线（T-W4-007 起）.

架构 v2 §4.8 横切层：统一收口 LLM（L0–L3 分级）、TTS、嵌入、ASR（预留）。
所有 AI 调用经总线，禁止各域直连供应商；PII 在总线前剥离（D7）；
每次调用记台账，按 item_revision 归集单题全生命周期 AI 成本。

子包：
- bus：核心路由（ai_call 入口、policy.yaml、任务分级模型）
- ledger：调用台账 + PII 剥离中间件（T-W4-008）
- adapter：LiteLLM/DeepSeek 适配器 + fallback（T-W4-009）
- cost：单题全生命周期成本归集（T-W4-010）
- tts：TTS 总线档 + 学段音色/语速配置（T-W4-011）

宪法 A5：本包禁止 import 任何学科包/学段包（X6 等价约束）。
宪法 D7：PII 在总线前剥离；学生直标识只在保险库 schema，本包只见 student_alias_id。
"""
