// Package subjectlang 是语文学科包（W6 语文轮第一阶：确定性档）。
//
// 惯例照 subjectmath（W6 数学轮）：生成器/验证器独立（判分逻辑不复用生成
// 内部状态）、纯索引函数 + 种子可回放、内容摘要两两互异断言。
// 语文三档结构（issue #34 §六）的第一档——确定性校验（char_in_corpus /
// word_in_vocab）纯代码承载；半确定/开放档（LLM 操作员+评价者）归后续波次。
//
// 语文三档结构（issue #34 §六）：第一档确定性校验（char_in_corpus /
// word_in_vocab）纯代码承载；第二档半确定（LLM 操作员挖空 + 代码验可解性）
// 见 sentence_reorg.go——LLM 产出永远是 draft；评价者与开放档归后续波次。
//
// 语料来源：content/sources/corpus/manifest.yaml（三源分离 source=public；
// 演示样表 CC0-1.0，真实全量入库须 owner 逐源许可审查——issue #34 §五）。
package subjectlang
