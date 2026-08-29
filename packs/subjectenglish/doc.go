// Package subjectenglish 是 W6 英语轮确定性档的学科包（issue #34 §六第一档）：
// 词汇拼写（tpl-se-vocab-spell）+ 语法单选（tpl-se-gram-sc）。
//
// 与 subjectlang/subjectmath 同构但互相独立（学科包零横向 import）：同一
// Generator/Instance 管线契约、同一 fail-closed 语料装载纪律、同一「生成器 ×
// 独立校验器」双实现互证模式。语料来源登记在 content/sources/corpus/manifest.yaml。
package subjectenglish
