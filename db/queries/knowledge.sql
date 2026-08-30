-- 审计卡 #149/#159：知识图谱种子装载（cmd/seedload）写面 + 查重读面。
-- 列名以 db/migrations/0006_knowledge_graph.up.sql 为准（rel_type 主键、
-- uq_kp_node_pack_dim_code、uq_kp_edge_src_dst_rel 三条查重键）。
-- 幂等策略：INSERT ... ON CONFLICT DO NOTHING——与 seed_loader.Load 的
-- Existing* 预查重互为纵深（并发重放时唯一约束兜底，不抛错只跳过）；
-- 账面只增不改（D1）：本文件没有任何 UPDATE/DELETE 语句。

-- ── SeedSink 读面（core/knowledge.SeedSink 的 Existing* 预查重）─────────────

-- name: ListRelationTypes :many
-- 已有关系类型集合（relation_type.rel_type 主键投影）。
SELECT rel_type FROM relation_type;

-- name: ListNodeIDsByPackDimension :many
-- 本 pack+dimension 下现存 code → node_id（uq_kp_node_pack_dim_code 查重键）。
SELECT code, node_id FROM kp_node WHERE pack_id = $1 AND dimension = $2;

-- name: ListKpEdges :many
-- 已有边三元组集合（uq_kp_edge_src_dst_rel 查重键）。
SELECT src_node_id, dst_node_id, rel_type FROM kp_edge;

-- ── SeedSink 写面（幂等 upsert：冲突即 DO NOTHING）─────────────────────────

-- name: InsertRelationType :exec
-- 关系类型就位（rel_type 主键冲突 = 已存在，跳过不改既有行）。
-- description 缺省 NULL；created_at 留列默认 now()。
INSERT INTO relation_type (rel_type, directed, transitive, acyclic, "symmetric", description)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (rel_type) DO NOTHING;

-- name: InsertKpNode :exec
-- 知识点节点就位（pack+dimension+code 冲突 = 已存在，跳过不改既有行）。
-- status 由调用方显式传入（种子装载写 'active'），不用列默认 draft。
INSERT INTO kp_node (node_id, pack_id, dimension, code, title, std_anchor, gradeband, status)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (pack_id, dimension, code) DO NOTHING;

-- name: InsertKpEdge :exec
-- 知识点边就位（src+dst+rel_type 冲突 = 已存在，跳过不改既有行）。
-- attrs/provenance 传 JSONB 文本（缺省 '{}'::jsonb）；edge_id 为
-- GENERATED ALWAYS AS IDENTITY，禁止显式插入。
INSERT INTO kp_edge (src_node_id, dst_node_id, rel_type, attrs, provenance)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (src_node_id, dst_node_id, rel_type) DO NOTHING;
