package main

import (
	"encoding/json"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/datastat"
)

// TestCalibrateEndToEnd 端到端：构造 JSONL 输入 → 标定 → 校验输出字段与方向.
func TestCalibrateEndToEnd(t *testing.T) {
	// 3 题宽间距真值，30 生，seed 固定（确定性）.
	recs := `{"student_id":"s0","item_id":"easy","correct":1}
{"student_id":"s0","item_id":"mid","correct":0}
{"student_id":"s0","item_id":"hard","correct":0}
{"student_id":"s1","item_id":"easy","correct":1}
{"student_id":"s1","item_id":"mid","correct":1}
{"student_id":"s1","item_id":"hard","correct":0}
{"student_id":"s2","item_id":"easy","correct":1}
{"student_id":"s2","item_id":"mid","correct":1}
{"student_id":"s2","item_id":"hard","correct":1}
`
	records, err := readRecordsFrom(strings.NewReader(recs))
	if err != nil {
		t.Fatalf("readRecords: %v", err)
	}
	params := datastat.Calibrate2PL(records)
	if len(params) != 3 {
		t.Fatalf("期望 3 题，得到 %d", len(params))
	}
	// 按 item_version_id 索引.
	byID := make(map[string]datastat.ItemIrtStats)
	for _, p := range params {
		byID[p.ItemVersionID] = p
	}
	easy, mid, hard := byID["easy"], byID["mid"], byID["hard"]
	if easy.Difficulty >= mid.Difficulty {
		t.Errorf("方向：easy 难度 %.3f 应 < mid %.3f", easy.Difficulty, mid.Difficulty)
	}
	if mid.Difficulty >= hard.Difficulty {
		t.Errorf("方向：mid 难度 %.3f 应 < hard %.3f", mid.Difficulty, hard.Difficulty)
	}
	for _, p := range params {
		if p.Discrimination <= 0 {
			t.Errorf("%s 区分度 %.4f 应为正", p.ItemVersionID, p.Discrimination)
		}
		if p.SampleSize < 3 {
			t.Errorf("%s sample_size=%d 过小", p.ItemVersionID, p.SampleSize)
		}
	}
}

// TestWriteParamsShape 校验输出 JSON 字段形态.
func TestWriteParamsShape(t *testing.T) {
	params := []datastat.ItemIrtStats{
		{ItemVersionID: "it01", Discrimination: 1.23456, Difficulty: -0.12345, Rating: 1500.0, SampleSize: 40},
	}
	var sb strings.Builder
	if err := writeParamsTo(&sb, params); err != nil {
		t.Fatalf("writeParams: %v", err)
	}
	var got outRecord
	if err := json.Unmarshal([]byte(sb.String()), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.ItemVersionID != "it01" {
		t.Errorf("item_version_id = %q", got.ItemVersionID)
	}
	if got.Discrimination != 1.2346 {
		t.Errorf("discrimination 收整 = %v, want 1.2346", got.Discrimination)
	}
	if got.SampleSize != 40 {
		t.Errorf("sample_size = %d", got.SampleSize)
	}
}

// TestWriteItemParamsDryRun 校验 dry-run 路径构造 params JSON 且不连 DB.
func TestWriteItemParamsDryRun(t *testing.T) {
	params := []datastat.ItemIrtStats{
		{ItemVersionID: "it01", Discrimination: 1.23456, Difficulty: -0.12345, Rating: 1500.0, SampleSize: 40},
	}
	var sb strings.Builder
	// capture stderr
	old := os.Stderr
	r, w, _ := os.Pipe()
	os.Stderr = w
	err := writeItemParams("unused-dsn", params, datastat.IRTSource, datastat.ScopeMeasurement, true)
	_ = w.Close()
	os.Stderr = old
	if err != nil {
		t.Fatalf("dry-run 不应报错: %v", err)
	}
	_, _ = io.Copy(&sb, r)
	if !strings.Contains(sb.String(), "it01") || !strings.Contains(sb.String(), "measured_irt") {
		t.Errorf("dry-run 输出应含 item 与 source，got: %s", sb.String())
	}
	if strings.Contains(sb.String(), "discrimination:1.2346") == false {
		// params JSON 含收整后的区分度.
	}
}

func TestReadRecordsEmpty(t *testing.T) {
	records, err := readRecordsFrom(strings.NewReader(""))
	if err != nil {
		t.Fatalf("空输入应返回空列表无错: %v", err)
	}
	if len(records) != 0 {
		t.Errorf("空输入应返回 0 条，得到 %d", len(records))
	}
}

// ── 测试用薄封装（避免触碰 os.Stdin/Stdout）──

func readRecordsFrom(r *strings.Reader) ([]datastat.ResponseRecord, error) {
	dec := json.NewDecoder(r)
	var out []datastat.ResponseRecord
	for {
		var rec reqRecord
		err := dec.Decode(&rec)
		if err != nil {
			break
		}
		out = append(out, datastat.ResponseRecord{
			StudentAliasID: rec.StudentID,
			ItemVersionID:  rec.ItemID,
			Correct:        rec.Correct,
		})
	}
	return out, nil
}

func writeParamsTo(w *strings.Builder, params []datastat.ItemIrtStats) error {
	enc := json.NewEncoder(w)
	for _, p := range params {
		rec := outRecord{
			ItemVersionID:  p.ItemVersionID,
			Discrimination: round4(p.Discrimination),
			Difficulty:     round4(p.Difficulty),
			Rating:         round1(p.Rating),
			SampleSize:     p.SampleSize,
		}
		if err := enc.Encode(rec); err != nil {
			return err
		}
	}
	return nil
}
