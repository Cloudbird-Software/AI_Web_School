// Package knowledge 是知识域的 Go 移植（PyR 波；Python 冻结语义基准
// src/core/knowledge/closure.py + seed_loader.py）。
//
//   - closure.go：知识图谱传递闭包计算（kp_closure 语义）——对 transitive
//     关系类型（如 prerequisite/composes）递归展开多跳可达；对非 transitive
//     关系类型（如 confusable）仅 depth=1 直接边；path_count 承载同
//     (src, dst, rel_type, depth) 的不同路径数。
//   - seed_loader.go：知识图谱种子数据加载（读 content/seeds/*.yaml）——
//     按 (pack_id, dimension, code) 查重节点、按 (src_node_id, dst_node_id,
//     rel_type) 查重边，重复导入 = skip 不抛错（幂等约定）。
//
// 本包是纯逻辑层：不接 DB。图的边/关系类型/版本与闭包账经查询面端口注入
// （GraphView / ClosureStore / SeedSink，Memory 实现承载唯一约束兜底语义供
// 测试；PG 实现属装配层，热路径查询仍退化为 kp_closure 扁平单表过滤）。
//
// 宪法 A5/A7/X6：本包不 import 任何学科包/学段包（学科零特判）。
package knowledge
