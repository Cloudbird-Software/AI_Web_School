package paperdb

// paperdb_test.go：题源 DB 适配（#148 交付 4）的单元测试。零 DB——pgx.Rows
// 的查询面属集成测试范畴（core/review 查询服务同一取舍），本文件覆盖：
//   - 行→dict 转换的纯函数面（身份三键直通 / JSONB 四块解码 / NULL 模板键
//     缺省 / 坏 JSON fail-loud / null 块放行）；
//   - 未装配 fail-closed（nil db → ErrNoExecutor）；
//   - 编译期锚定（ItemSource 满足 assembly.PaperItemSource，见包内 var _）。

import (
	"context"
	"errors"
	"strings"
	"testing"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5/pgtype"
)

func testRow() dbgen.ListServingItemVersionsByPackGradebandRow {
	return dbgen.ListServingItemVersionsByPackGradebandRow{
		Objective:      []byte(`{}`),
		InteractionRef: []byte(`{}`),
		Lineage:        []byte(`{}`),
		Content:        []byte(`{}`),
	}
}

func TestRowToDict_DecodesAllBlocks(t *testing.T) {
	r := testRow()
	r.ItemVersionID = "iv-1"
	r.ItemID = "item-1"
	r.TemplateVersionID = pgtype.Text{String: "tpl-1", Valid: true}
	r.Objective = []byte(`{"gradeband":"M","kp_set":[{"code":"KP1"}]}`)
	r.InteractionRef = []byte(`{"interaction_id":"single_choice"}`)
	r.Lineage = []byte(`{"params":{"seed":7}}`)
	r.Content = []byte(`{"blocks":[{"type":"text","value":"3 + 5 = ?"}]}`)

	d, err := rowToDict(r)
	if err != nil {
		t.Fatalf("合法行解码失败: %v", err)
	}
	if d["item_version_id"] != "iv-1" || d["item_id"] != "item-1" || d["template_version_id"] != "tpl-1" {
		t.Fatalf("身份键面漂移: %v", d)
	}
	obj, _ := d["objective"].(map[string]any)
	if obj == nil || obj["gradeband"] != "M" {
		t.Fatalf("objective 块未解码为值树: %v", d["objective"])
	}
	content, _ := d["content"].(map[string]any)
	blocks, _ := content["blocks"].([]any)
	if len(blocks) != 1 {
		t.Fatalf("content.blocks 未解码（渲染必要输入）: %v", content)
	}
}

func TestRowToDict_NullTemplateOmitsKey(t *testing.T) {
	r := testRow()
	r.ItemVersionID = "iv-2"
	r.ItemID = "item-2"
	// TemplateVersionID 零值 = pgtype.Text{Valid:false} = SQL NULL
	d, err := rowToDict(r)
	if err != nil {
		t.Fatalf("NULL 模板行解码失败: %v", err)
	}
	if _, present := d["template_version_id"]; present {
		t.Fatalf("SQL NULL 模板不得以空串值混入（缺键 = 无引用的编排层约定）")
	}
}

func TestRowToDict_BadJSONFailsLoud(t *testing.T) {
	cases := map[string][]byte{
		"objective":       []byte(`{broken`),
		"interaction_ref": []byte(`[1,2`), // 顶型非对象同为坏行
		"lineage":         []byte(`null{`),
		"content":         []byte(`"str"`), // 顶型非对象（content 必须是块容器）
	}
	for name, bad := range cases {
		r := testRow()
		r.ItemVersionID = "iv-bad"
		r.ItemID = "item-bad"
		switch name {
		case "objective":
			r.Objective = bad
		case "interaction_ref":
			r.InteractionRef = bad
		case "lineage":
			r.Lineage = bad
		case "content":
			r.Content = bad
		}
		_, err := rowToDict(r)
		if err == nil {
			t.Fatalf("%s 块坏 JSON 必须整体失败（坏行不混入候选池）", name)
		}
		if !strings.Contains(err.Error(), "iv-bad") {
			t.Fatalf("%s 错误未锚定行身份（排障面）: %v", name, err)
		}
	}
}

func TestRowToDict_NullBlockYieldsNilMap(t *testing.T) {
	r := testRow()
	r.ItemVersionID = "iv-3"
	r.ItemID = "item-3"
	r.Content = nil // JSON null / 空列：解码面放行 nil，业务校验归下游
	d, err := rowToDict(r)
	if err != nil {
		t.Fatalf("null 块解码失败: %v", err)
	}
	raw, present := d["content"]
	if !present {
		t.Fatalf("null content 应保留键位（编排层形态约定）")
	}
	m, _ := raw.(map[string]any)
	if m != nil {
		t.Fatalf("null content 应解码为 nil map（typed-nil 语义如实保留）: %v", raw)
	}
}

func TestItemSource_NotWiredFailsClosed(t *testing.T) {
	var s *ItemSource
	if _, err := s.LoadPublishedItemVersions(context.Background(), "pack-1", "M"); !errors.Is(err, ErrNoExecutor) {
		t.Fatalf("nil 接收器应 ErrNoExecutor, got %v", err)
	}
	if _, err := NewItemSource(nil).LoadPublishedItemVersions(context.Background(), "pack-1", "M"); !errors.Is(err, ErrNoExecutor) {
		t.Fatalf("nil db 应 ErrNoExecutor, got %v", err)
	}
}
