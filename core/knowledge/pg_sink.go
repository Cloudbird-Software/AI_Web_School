// pg_sink.go 承载 SeedSink 的 PG 生产实现（审计卡 #149/#159：知识图谱种子
// 批量装载入口 cmd/seedload 的写入面）。语句面全部来自 db/queries/knowledge.sql
// 的 sqlc 类型安全生成方法（SQL-2：不在 Go 拼 SQL）。
//
// 幂等双保险：seed_loader.Load 先经 Existing* 预查重跳过已存在行（skip 统计），
// 写入语句再以 INSERT ... ON CONFLICT DO NOTHING 兜底（并发重放时唯一约束
// uq_kp_node_pack_dim_code / uq_kp_edge_src_dst_rel / rel_type 主键在库端
// 物理拦截，不抛错只跳过）。账面只增不改（D1）：本实现没有任何 UPDATE/DELETE。
//
// 事务纪律（D11）：本类型不持有连接、不自 begin/commit——db 参数接受任何
// 满足 dbgen.DBTX 的执行面（pgx.Conn / pgx.Tx / pool），是否并入调用方事务
// 由装配方决定；种子装载是幂等批过程，cmd/seedload 直连池逐条提交即可，
// 单行失败由调用方按 stats/错误定位（导入中断不产生半行脏数据）。
package knowledge

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5/pgtype"
)

// PGSink 是 SeedSink 的 PG 生产实现.
type PGSink struct {
	ctx context.Context // SeedSink 接口无 ctx 参数（种子装载为批过程），构造期注入
	q   *dbgen.Queries
}

// 编译期锚定：PGSink 必须兑现 SeedSink 契约（cmd/seedload 装配直通的假设防线）.
var _ SeedSink = (*PGSink)(nil)

// NewPGSink 构造 PG 种子装载面。db 接受任何满足 dbgen.DBTX 的执行面.
func NewPGSink(ctx context.Context, db dbgen.DBTX) *PGSink {
	return &PGSink{ctx: ctx, q: dbgen.New(db)}
}

// ExistingRelationTypes 实现 SeedSink：已有关系类型集合.
func (p *PGSink) ExistingRelationTypes() (map[string]struct{}, error) {
	rows, err := p.q.ListRelationTypes(p.ctx)
	if err != nil {
		return nil, fmt.Errorf("knowledge/pg sink: 列关系类型失败: %w", err)
	}
	out := make(map[string]struct{}, len(rows))
	for _, rt := range rows {
		out[rt] = struct{}{}
	}
	return out, nil
}

// AddRelationType 实现 SeedSink：InsertRelationType（ON CONFLICT DO NOTHING）.
func (p *PGSink) AddRelationType(rt RelationType) error {
	if err := p.q.InsertRelationType(p.ctx, dbgen.InsertRelationTypeParams{
		RelType:     rt.RelType,
		Directed:    rt.Directed,
		Transitive:  rt.Transitive,
		Acyclic:     rt.Acyclic,
		Symmetric:   rt.Symmetric,
		Description: textNil(rt.Description),
	}); err != nil {
		return fmt.Errorf("knowledge/pg sink: 插入关系类型 %s 失败: %w", rt.RelType, err)
	}
	return nil
}

// ExistingNodeIDs 实现 SeedSink：本 pack+dimension 下 code → node_id.
func (p *PGSink) ExistingNodeIDs(packID, dimension string) (map[string]string, error) {
	rows, err := p.q.ListNodeIDsByPackDimension(p.ctx, dbgen.ListNodeIDsByPackDimensionParams{
		PackID:    packID,
		Dimension: dimension,
	})
	if err != nil {
		return nil, fmt.Errorf("knowledge/pg sink: 列节点（pack=%s dim=%s）失败: %w", packID, dimension, err)
	}
	out := make(map[string]string, len(rows))
	for _, r := range rows {
		out[r.Code] = r.NodeID
	}
	return out, nil
}

// AddNode 实现 SeedSink：InsertKpNode（ON CONFLICT DO NOTHING）.
// std_anchor/gradeband 缺省 NULL；status 由调用方显式传入（枚举白名单由
// kp_node_status_enum 在库端强制）.
func (p *PGSink) AddNode(n KpNode) error {
	if err := p.q.InsertKpNode(p.ctx, dbgen.InsertKpNodeParams{
		NodeID:    n.NodeID,
		PackID:    n.PackID,
		Dimension: n.Dimension,
		Code:      n.Code,
		Title:     n.Title,
		StdAnchor: textPtrNil(n.StdAnchor),
		Gradeband: textPtrNil(n.Gradeband),
		Status:    dbgen.KpNodeStatusEnum(n.Status),
	}); err != nil {
		return fmt.Errorf("knowledge/pg sink: 插入节点 %s 失败: %w", n.Code, err)
	}
	return nil
}

// ExistingEdges 实现 SeedSink：已有边 (src,dst,rel_type) 三元组集合.
func (p *PGSink) ExistingEdges() (map[[3]string]struct{}, error) {
	rows, err := p.q.ListKpEdges(p.ctx)
	if err != nil {
		return nil, fmt.Errorf("knowledge/pg sink: 列边失败: %w", err)
	}
	out := make(map[[3]string]struct{}, len(rows))
	for _, e := range rows {
		out[[3]string{e.SrcNodeID, e.DstNodeID, e.RelType}] = struct{}{}
	}
	return out, nil
}

// AddEdge 实现 SeedSink：InsertKpEdge（ON CONFLICT DO NOTHING）.
// attrs/provenance 以 JSONB 文本落列：nil/空 map 归一为 '{}'（与 0006 列默认
// '{}'::jsonb 语义一致，绝不落 JSON null）.
func (p *PGSink) AddEdge(e KpEdge) error {
	attrs, err := jsonbBytes(e.Attrs)
	if err != nil {
		return fmt.Errorf("knowledge/pg sink: 边 %s→%s(%s) attrs 序列化失败: %w", e.SrcNodeID, e.DstNodeID, e.RelType, err)
	}
	prov, err := jsonbBytes(e.Provenance)
	if err != nil {
		return fmt.Errorf("knowledge/pg sink: 边 %s→%s(%s) provenance 序列化失败: %w", e.SrcNodeID, e.DstNodeID, e.RelType, err)
	}
	if err := p.q.InsertKpEdge(p.ctx, dbgen.InsertKpEdgeParams{
		SrcNodeID:  e.SrcNodeID,
		DstNodeID:  e.DstNodeID,
		RelType:    e.RelType,
		Attrs:      attrs,
		Provenance: prov,
	}); err != nil {
		return fmt.Errorf("knowledge/pg sink: 插入边 %s→%s(%s) 失败: %w", e.SrcNodeID, e.DstNodeID, e.RelType, err)
	}
	return nil
}

// jsonbBytes 把 map 归一为 JSONB 文本字节：nil/空 map → '{}'（列默认同语义）.
func jsonbBytes(m map[string]any) ([]byte, error) {
	if len(m) == 0 {
		return []byte("{}"), nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	return b, nil
}

// textNil 把 nil 描述折叠为 NULL.
func textNil(s *string) pgtype.Text {
	if s == nil {
		return pgtype.Text{}
	}
	return pgtype.Text{String: *s, Valid: true}
}

// textPtrNil 与 textNil 同义（可空字符串指针 → pgtype.Text 的统一形）.
func textPtrNil(s *string) pgtype.Text {
	return textNil(s)
}
