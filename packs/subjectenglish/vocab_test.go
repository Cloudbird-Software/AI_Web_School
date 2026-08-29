package subjectenglish

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testManifest = "../../content/sources/corpus/manifest.yaml"

func testVocab(t *testing.T) *EnglishVocab {
	t.Helper()
	v, err := LoadEnglishVocab(testManifest, "eng-basic-vocab-v1")
	if err != nil {
		t.Fatalf("英语词表装载: %v", err)
	}
	return v
}

// 语料装载：规模在许可留痕口径内、分域齐备、规则类全覆盖、基准条目在表。
func TestLoadEnglishVocab(t *testing.T) {
	v := testVocab(t)
	if len(v.EntryList) == 0 || len(v.EntryList) > maxVocabSize {
		t.Fatalf("词表规模 %d 违反节选口径 (0,%d]", len(v.EntryList), maxVocabSize)
	}
	if len(v.NounList) < 40 || len(v.VerbList) < 40 || len(v.AdjList) < 20 {
		t.Fatalf("词性分域不足：n=%d v=%d adj=%d", len(v.NounList), len(v.VerbList), len(v.AdjList))
	}
	if v.SourceID != "eng-basic-vocab-v1" {
		t.Fatalf("source_id=%q", v.SourceID)
	}
	for _, w := range []string{"apple", "book", "run", "happy", "box", "baby", "day", "teach"} {
		if _, ok := v.Entries[w]; !ok {
			t.Fatalf("基准词条 %q 缺失", w)
		}
	}
	// 释义互证判定域：释义↔词双向一致。
	if got := v.GlossToWord["苹果"]; got != "apple" {
		t.Fatalf("GlossToWord 反查失真: 苹果→%q", got)
	}
}

// 装载 fail-closed 面：许可缺失/来源缺失/坏行/重复/超规模/空表一律拒绝。
func TestLoadEnglishVocabFailClosed(t *testing.T) {
	writeTree := func(t *testing.T, manifest, words string) string {
		t.Helper()
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "manifest.yaml"), []byte(manifest), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "w.txt"), []byte(words), 0o644); err != nil {
			t.Fatal(err)
		}
		return filepath.Join(dir, "manifest.yaml")
	}
	const head = "version: \"1.0\"\ncorpora:\n"
	const licOK = "    license: CC0-1.0\n    words_file: w.txt\n"
	cases := []struct {
		name     string
		manifest string
		words    string
		wantSub  string
	}{
		{"来源缺失", head + "  - source_id: other\n" + licOK, "apple\tn\t苹果\n", "无来源"},
		{"许可缺失", head + "  - source_id: s1\n    words_file: w.txt\n", "apple\tn\t苹果\n", "缺 license"},
		{"words_file 缺失", head + "  - source_id: s1\n    license: CC0-1.0\n", "apple\tn\t苹果\n", "缺 words_file"},
		{"坏行两列", head + "  - source_id: s1\n" + licOK, "apple\tn\n", "格式非法"},
		{"词性白名单外", head + "  - source_id: s1\n" + licOK, "apple\tadv\t苹果\n", "白名单"},
		{"大写词拒绝", head + "  - source_id: s1\n" + licOK, "Apple\tn\t苹果\n", "非规范"},
		{"词重复", head + "  - source_id: s1\n" + licOK, "apple\tn\t苹果\napple\tn\t苹果\n", "重复"},
		{"释义重复", head + "  - source_id: s1\n" + licOK, "apple\tn\t苹果\npear\tn\t苹果\n", "重复"},
		{"空表", head + "  - source_id: s1\n" + licOK, "# 只有注释\n", "为空"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := writeTree(t, tc.manifest, tc.words)
			if _, err := LoadEnglishVocab(path, "s1"); err == nil {
				t.Fatalf("%s: 应装载失败", tc.name)
			} else if !strings.Contains(err.Error(), tc.wantSub) {
				t.Fatalf("%s: 错误未指向预期类别: %v", tc.name, err)
			}
		})
	}
	// 超规模（121 条 > 上限 120）：词与释义都唯一，确保先不触发重复哨兵。
	var sb strings.Builder
	for i := 0; i < maxVocabSize+1; i++ {
		sb.WriteString("w" + string(rune('a'+i%26)) + string(rune('a'+i/26)) + "\tn\t释义" + strings.Repeat("字", i+1) + "\n")
	}
	path := writeTree(t, head+"  - source_id: s1\n"+licOK, sb.String())
	if _, err := LoadEnglishVocab(path, "s1"); err == nil || !strings.Contains(err.Error(), "超节选上限") {
		t.Fatalf("超规模应拒绝: %v", err)
	}
}
