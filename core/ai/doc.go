// Package ai 承载 AI 总线核心域：一切生成式调用经总线、落台账
// （模型+版本+prompt 版本+成本+产物 id），PII 先剥离、剥离失败 fail-closed
// （宪法 A6/D9/D10；T-W5-014/015）。
//
// prompt 层使用 BAML（ADR-0004 D-C）；本包只做总线与台账，不含 prompt 定义。
package ai
