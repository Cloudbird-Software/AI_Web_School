// calibrate 纯函数面单测：params JSONB 形状（discrimination nil → JSON null，
// 不伪造 0——与冻结实现同形）与场景越域 fail-closed。
package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/datastat"
)

func openDevNull() (*os.File, error) {
	return os.OpenFile(os.DevNull, os.O_RDWR, 0)
}

func TestCttParams(t *testing.T) {
	d := 0.75
	b, err := cttParams(datastat.ItemCttStats{
		ItemVersionID:  "iv-1",
		SampleSize:     40,
		Difficulty:     0.75,
		Discrimination: &d,
	})
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	if m["difficulty"] != 0.75 {
		t.Fatalf("difficulty = %v, want 0.75", m["difficulty"])
	}
	if m["discrimination"] != 0.75 {
		t.Fatalf("discrimination = %v, want 0.75", m["discrimination"])
	}

	// 区分度不可计算（n<2 零方差）：JSON null，绝不伪造 0
	b2, err := cttParams(datastat.ItemCttStats{ItemVersionID: "iv-2", SampleSize: 1, Difficulty: 0.5})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b2), `"discrimination":null`) {
		t.Fatalf("discrimination 应为 null（不伪造 0）: %s", b2)
	}
}

func TestRunValidatesInputs(t *testing.T) {
	devNull, err := openDevNull()
	if err != nil {
		t.Skip("无 /dev/null 环境")
	}
	defer func() { _ = devNull.Close() }() // GO-2 显式弃错
	if err := run([]string{"-scene", "bogus"}, devNull, devNull); err == nil {
		t.Fatal("越域 scene 应 fail-closed 报错")
	}
	if err := run([]string{"-scene", "practice", "-min-sample", "0"}, devNull, devNull); err == nil {
		t.Fatal("min-sample<1 应报错")
	}
	// 合法 scene 但无 DSN：fail-closed（无账本不标定）
	t.Setenv("SCHOOL_DATABASE_URL", "")
	if err := run([]string{"-scene", "practice"}, devNull, devNull); err == nil ||
		!strings.Contains(err.Error(), "SCHOOL_DATABASE_URL") {
		t.Fatalf("缺 DSN 应 fail-closed 报错，得到: %v", err)
	}
}
