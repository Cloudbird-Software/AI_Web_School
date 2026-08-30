// dryRunSink：SeedSink 装饰器——读面照常穿透（查重统计与真实重跑一致），
// 写面抑制并计数。cmd/seedload -dry-run 用它兑现「演练不动账」。
package main

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/knowledge"
)

type dryRunSink struct {
	inner knowledge.SeedSink
	// suppressed 记录被抑制的写入数（added 计数由 Load 统计面承担，
	// 这里交叉验证「演练期间底层零写入」——两个数应相等）
	relTypes, nodes, edges int
}

// 编译期锚定：dryRunSink 必须兑现 SeedSink.
var _ knowledge.SeedSink = (*dryRunSink)(nil)

func newDryRunSink(inner knowledge.SeedSink) *dryRunSink {
	return &dryRunSink{inner: inner}
}

func (d *dryRunSink) ExistingRelationTypes() (map[string]struct{}, error) {
	return d.inner.ExistingRelationTypes()
}

func (d *dryRunSink) AddRelationType(rt knowledge.RelationType) error {
	d.relTypes++
	return nil
}

func (d *dryRunSink) ExistingNodeIDs(packID, dimension string) (map[string]string, error) {
	return d.inner.ExistingNodeIDs(packID, dimension)
}

func (d *dryRunSink) AddNode(n knowledge.KpNode) error {
	d.nodes++
	return nil
}

func (d *dryRunSink) ExistingEdges() (map[[3]string]struct{}, error) {
	return d.inner.ExistingEdges()
}

func (d *dryRunSink) AddEdge(e knowledge.KpEdge) error {
	d.edges++
	return nil
}

// verifyAndReset 交叉验证：Load 统计的 added 数 = 被抑制的写入数（否则
// 演练面漏记写入）；验证后清零计数器（按文件逐次校验）.
func (d *dryRunSink) verifyAndReset(s *knowledge.SeedLoadStats) error {
	defer func() { d.relTypes, d.nodes, d.edges = 0, 0, 0 }()
	if d.relTypes != s.RelationTypesAdded || d.nodes != s.NodesAdded || d.edges != s.EdgesAdded {
		return fmt.Errorf("dry-run 记账不一致：抑制写入 (rt=%d node=%d edge=%d) ≠ added 统计 (rt=%d node=%d edge=%d)",
			d.relTypes, d.nodes, d.edges, s.RelationTypesAdded, s.NodesAdded, s.EdgesAdded)
	}
	return nil
}
