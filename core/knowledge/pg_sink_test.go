// pg_sink_test.go PGSink 的本地可验证语义（无 Docker/PG 的面，对齐
// cmd/ingest 的 fake 事务惯例）：以 fakeDBTX 承载 dbgen.DBTX，记录 Exec
// 语句参数面、按 SQL 形态返回预设 Query 行——验证三个 Existing* 读的
// 投影装配、三个 Insert* 写的参数映射（可空折叠/枚举/JSONB 归一），
// 以及 Load 全链在 PGSink 上的 added 统计。约束真值（唯一约束物理拦截、
// ON CONFLICT 行为）由 PG 在库端强制，不属于本套件伪造范围。
package knowledge

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// ── fake 执行面 ────────────────────────────────────────────────────────

// execCall 一次 Exec 的语句与参数快照.
type execCall struct {
	sql  string
	args []any
}

// fakeDBTX 记录全部 Exec（写面），Query 按 SQL 形态返回预设行（读面）。
type fakeDBTX struct {
	execLog []execCall
	// 预设读面：relation_type 行 / kp_node (code,node_id) 行 / kp_edge 三元组行
	relTypes []string
	nodes    [][2]string
	edges    [][3]string
	// 捕获最近一次 List 查询的过滤参数（验证 pack/dimension 下探）
	lastQueryArgs []any
}

func (f *fakeDBTX) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.execLog = append(f.execLog, execCall{sql: sql, args: args})
	return pgconn.CommandTag{}, nil
}

func (f *fakeDBTX) Query(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
	f.lastQueryArgs = args
	var rows [][]any
	switch {
	case strings.Contains(sql, "FROM relation_type"):
		for _, rt := range f.relTypes {
			rows = append(rows, []any{rt})
		}
	case strings.Contains(sql, "FROM kp_node"):
		for _, n := range f.nodes {
			rows = append(rows, []any{n[0], n[1]})
		}
	case strings.Contains(sql, "FROM kp_edge"):
		for _, e := range f.edges {
			rows = append(rows, []any{e[0], e[1], e[2]})
		}
	default:
		return nil, fmt.Errorf("fake: 未预设的 Query 语句: %s", sql)
	}
	return &fakeRows{rows: rows}, nil
}

func (f *fakeDBTX) QueryRow(context.Context, string, ...any) pgx.Row {
	return errRow{}
}

// fakeRows 最小 pgx.Rows：按预设行序推进，Scan 逐列拷入 *string.
type fakeRows struct {
	rows [][]any
	i    int
}

func (r *fakeRows) Next() bool {
	if r.i < len(r.rows) {
		r.i++
		return true
	}
	return false
}

func (r *fakeRows) Scan(dest ...any) error {
	row := r.rows[r.i-1]
	if len(dest) != len(row) {
		return fmt.Errorf("fake: scan 目标 %d 列 ≠ 桩 %d 列", len(dest), len(row))
	}
	for j, d := range dest {
		p, ok := d.(*string)
		if !ok {
			return fmt.Errorf("fake: 不支持的 scan 目标 %T", d)
		}
		*p = row[j].(string)
	}
	return nil
}

func (r *fakeRows) Close()                                       {}
func (r *fakeRows) Err() error                                   { return nil }
func (r *fakeRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *fakeRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *fakeRows) RawValues() [][]byte                          { return nil }
func (r *fakeRows) Values() ([]any, error)                       { return nil, errors.New("fake: 未用") }
func (r *fakeRows) Conn() *pgx.Conn                              { return nil }

// errRow 满足 pgx.Row 但必错（PGSink 不应走 :one 面）.
type errRow struct{}

func (errRow) Scan(...any) error { return errors.New("fake: PGSink 不使用 QueryRow") }

// ── 断言辅助 ───────────────────────────────────────────────────────────

// execByName 取第 n 个名字匹配的 Exec 调用（按生成语句常量含的表/动作片段）.
func execByName(t *testing.T, f *fakeDBTX, fragment string, nth int) execCall {
	t.Helper()
	seen := 0
	for _, c := range f.execLog {
		if strings.Contains(c.sql, fragment) {
			if seen == nth {
				return c
			}
			seen++
		}
	}
	t.Fatalf("Exec 日志中没找到第 %d 个含 %q 的语句（共 %d 条 Exec）", nth, fragment, len(f.execLog))
	return execCall{}
}

// writeSeed 落一个最小种子文件（默认 gradeband 缺省链路与可空字段覆盖）.
func writeSeed(t *testing.T) string {
	t.Helper()
	const yaml = `version: "1.0"
pack_id: subject-math
graph_release_id: "2026.1.test-pg-sink"
relation_types:
  - rel_type: prerequisite
    directed: true
    transitive: true
    acyclic: true
    symmetric: false
    description: 先修关系
nodes:
  - {code: math.t.a, title: 节点甲, std_anchor: "课标2022.nal.1-2.1", gradeband: L}
  - {code: math.t.b, title: 节点乙}
edges:
  - {src: math.t.a, dst: math.t.b, rel_type: prerequisite}
`
	path := filepath.Join(t.TempDir(), "seed_test.yaml")
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatalf("写种子文件: %v", err)
	}
	return path
}

// ── 读面：Existing* 投影装配 ────────────────────────────────────────────

func TestPGSinkExistingReads(t *testing.T) {
	f := &fakeDBTX{
		relTypes: []string{"prerequisite", "confusable"},
		nodes:    [][2]string{{"math.t.a", "kp_aaa"}, {"math.t.b", "kp_bbb"}},
		edges:    [][3]string{{"kp_aaa", "kp_bbb", "prerequisite"}},
	}
	sink := NewPGSink(context.Background(), f)

	rels, err := sink.ExistingRelationTypes()
	if err != nil {
		t.Fatalf("ExistingRelationTypes: %v", err)
	}
	if len(rels) != 2 {
		t.Fatalf("关系类型集合应含 2 项，实际 %v", rels)
	}
	if _, ok := rels["prerequisite"]; !ok {
		t.Fatalf("关系类型集合缺 prerequisite: %v", rels)
	}

	ids, err := sink.ExistingNodeIDs("subject-math", "kp")
	if err != nil {
		t.Fatalf("ExistingNodeIDs: %v", err)
	}
	if ids["math.t.a"] != "kp_aaa" || ids["math.t.b"] != "kp_bbb" {
		t.Fatalf("code→node_id 投影不符: %v", ids)
	}
	// 过滤参数必须原样下探（pack+dimension 查重键）
	if len(f.lastQueryArgs) != 2 || f.lastQueryArgs[0] != "subject-math" || f.lastQueryArgs[1] != "kp" {
		t.Fatalf("ListNodeIDsByPackDimension 过滤参数不符: %v", f.lastQueryArgs)
	}

	edges, err := sink.ExistingEdges()
	if err != nil {
		t.Fatalf("ExistingEdges: %v", err)
	}
	if _, ok := edges[[3]string{"kp_aaa", "kp_bbb", "prerequisite"}]; !ok {
		t.Fatalf("边三元组集合缺 kp_aaa→kp_bbb(prerequisite): %v", edges)
	}
}

// ── 写面：Load 全链参数映射 ─────────────────────────────────────────────

func TestPGSinkLoadRoundTripParams(t *testing.T) {
	f := &fakeDBTX{} // 空库：全部 added、零 skip
	sink := NewPGSink(context.Background(), f)

	stats, err := Load(writeSeed(t), sink, "kp")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if stats.RelationTypesAdded != 1 || stats.NodesAdded != 2 || stats.EdgesAdded != 1 {
		t.Fatalf("added 统计不符: %+v", stats)
	}
	if stats.RelationTypesSkipped != 0 || stats.NodesSkipped != 0 || stats.EdgesSkipped != 0 || stats.EdgesMissingNode != 0 {
		t.Fatalf("skip/missing 统计应为零: %+v", stats)
	}

	// relation_type：布尔五元组 + description 非空
	rtc := execByName(t, f, "INSERT INTO relation_type", 0)
	if rtc.args[0] != "prerequisite" {
		t.Fatalf("rel_type 参数不符: %v", rtc.args[0])
	}
	for i, want := range []bool{true, true, true, false} { // directed/transitive/acyclic/symmetric
		if rtc.args[1+i] != want {
			t.Fatalf("relation_type 布尔参数[%d] 应为 %v: %v", i, want, rtc.args[1+i])
		}
	}
	desc, ok := rtc.args[5].(pgtype.Text)
	if !ok || !desc.Valid || desc.String != "先修关系" {
		t.Fatalf("description 参数不符: %#v", rtc.args[5])
	}

	// kp_node 首行：显式 std_anchor/gradeband，status=active，node_id 由 Load 生成
	nc := execByName(t, f, "INSERT INTO kp_node", 0)
	if nc.args[1] != "subject-math" || nc.args[2] != "kp" || nc.args[3] != "math.t.a" {
		t.Fatalf("节点身份四元组不符: %v", nc.args[1:4])
	}
	nodeID, _ := nc.args[0].(string)
	if !strings.HasPrefix(nodeID, "kp_") {
		t.Fatalf("node_id 应由 NewNodeID 生成（kp_ 前缀）: %q", nodeID)
	}
	anchor, _ := nc.args[5].(pgtype.Text)
	if !anchor.Valid || anchor.String != "课标2022.nal.1-2.1" {
		t.Fatalf("std_anchor 参数不符: %#v", nc.args[5])
	}
	band, _ := nc.args[6].(pgtype.Text)
	if !band.Valid || band.String != "L" {
		t.Fatalf("gradeband 参数不符: %#v", nc.args[6])
	}
	if nc.args[7] != dbgen.KpNodeStatusEnumActive {
		t.Fatalf("status 应为 active: %#v", nc.args[7])
	}

	// kp_node 次行：缺省链路——std_anchor NULL、gradeband 走加载器默认 M
	nc2 := execByName(t, f, "INSERT INTO kp_node", 1)
	if nc2.args[3] != "math.t.b" {
		t.Fatalf("第二节点 code 不符: %v", nc2.args[3])
	}
	if a := nc2.args[5].(pgtype.Text); a.Valid {
		t.Fatalf("缺省 std_anchor 应落 NULL: %#v", a)
	}
	if b := nc2.args[6].(pgtype.Text); !b.Valid || b.String != "M" {
		t.Fatalf("缺省 gradeband 应为 M: %#v", nc2.args[6])
	}

	// kp_edge：node_id 引用同批生成的 id，attrs/provenance 归一为 '{}'
	ec := execByName(t, f, "INSERT INTO kp_edge", 0)
	if ec.args[0] != nc.args[0] || ec.args[1] != nc2.args[0] {
		t.Fatalf("边端点应引用本批生成的 node_id: %v", ec.args[:2])
	}
	if ec.args[2] != "prerequisite" {
		t.Fatalf("边 rel_type 不符: %v", ec.args[2])
	}
	if string(ec.args[3].([]byte)) != "{}" || string(ec.args[4].([]byte)) != "{}" {
		t.Fatalf("attrs/provenance 应归一为 '{}': %q %q", ec.args[3], ec.args[4])
	}
}

// ── jsonbBytes 归一 ─────────────────────────────────────────────────────

func TestJsonbBytesNormalization(t *testing.T) {
	for _, m := range []map[string]any{nil, {}} {
		b, err := jsonbBytes(m)
		if err != nil {
			t.Fatalf("jsonbBytes(%v): %v", m, err)
		}
		if string(b) != "{}" {
			t.Fatalf("nil/空 map 应归一为 '{{}}'，实际 %q", b)
		}
	}
	b, err := jsonbBytes(map[string]any{"why": "先修"})
	if err != nil {
		t.Fatalf("jsonbBytes: %v", err)
	}
	if !strings.Contains(string(b), `"why"`) {
		t.Fatalf("非空 map 应序列化键值: %q", b)
	}
}
