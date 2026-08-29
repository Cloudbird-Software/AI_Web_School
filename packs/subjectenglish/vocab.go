package subjectenglish

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

// vocab.go —— 英语词表装载（subjectlang 的 manifest 读取模式同构移植，独立实现）。
// fail-closed：manifest 缺来源、缺 license、文件缺列/空表/超规模/非法词条一律
// 错误（不降级）——判定域为零或含脏数据时宁可停线也不带病出题。

// VocabManifest 是 content/sources/corpus/manifest.yaml 的程序面（最小子集：
// 英语轮只消费 words_file 引用与来源标识；license 语义由 content/sources/
// registry.yaml + tools/ci/check_sources.py 承载）。
type VocabManifest struct {
	Version string        `yaml:"version"`
	Corpora []VocabSource `yaml:"corpora"`
}

// VocabSource 单一语料来源（三源分离：source=public 的清单登记在案）。
type VocabSource struct {
	SourceID    string `yaml:"source_id"`
	Title       string `yaml:"title"`
	License     string `yaml:"license"`
	LicenseNote string `yaml:"license_note"`
	WordsFile   string `yaml:"words_file"`
}

// 词性白名单（词表第三列；白名单外即装载失败）。
const (
	PosNoun = "n"
	PosVerb = "v"
	PosAdj  = "adj"
)

// maxVocabSize 是许可留痕口径的节选规模硬上限（manifest license_note 声明 ≤120）。
const maxVocabSize = 120

// VocabEntry 单词条。
type VocabEntry struct {
	Word  string
	Pos   string
	Gloss string
}

// EnglishVocab 装载后的英语词表（不可变查询面）。
type EnglishVocab struct {
	SourceID string
	Entries  map[string]VocabEntry // word → entry（拼写答案在表内的判定域）
	// GlossToWord 释义 → 词（释义全表唯一，拼写题「释义↔答案」互证的判定域）。
	GlossToWord map[string]string
	EntryList   []VocabEntry // 稳定顺序（参数空间索引函数的基底——同序即可回放）
	NounList    []string     // 词性分域（语法母题按词性取材）
	VerbList    []string
	AdjList     []string
}

// LoadEnglishVocab 读 manifest 并装载指定来源的英语词表。
func LoadEnglishVocab(manifestPath, sourceID string) (*EnglishVocab, error) {
	m, err := readVocabManifest(manifestPath)
	if err != nil {
		return nil, err
	}
	var src *VocabSource
	for i := range m.Corpora {
		if m.Corpora[i].SourceID == sourceID {
			src = &m.Corpora[i]
			break
		}
	}
	if src == nil {
		return nil, fmt.Errorf("manifest %s 无来源 %q", manifestPath, sourceID)
	}
	if src.License == "" {
		// 许可缺失即拒绝装载（X12：许可未登记时不得放行）。
		return nil, fmt.Errorf("来源 %q 缺 license（X12：许可未登记不得入管线）", sourceID)
	}
	if src.WordsFile == "" {
		return nil, fmt.Errorf("来源 %q 缺 words_file 引用", sourceID)
	}
	entries, err := readVocabEntries(joinPath(dirOf(manifestPath), src.WordsFile))
	if err != nil {
		return nil, fmt.Errorf("词表装载失败: %w", err)
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("来源 %q 词表为空（空语料=校验器判定域为零，fail-closed）", sourceID)
	}
	if len(entries) > maxVocabSize {
		return nil, fmt.Errorf("来源 %q 词表 %d 条超节选上限 %d（许可留痕口径被破坏）", sourceID, len(entries), maxVocabSize)
	}
	v := &EnglishVocab{
		SourceID:    src.SourceID,
		Entries:     make(map[string]VocabEntry, len(entries)),
		GlossToWord: make(map[string]string, len(entries)),
	}
	for _, e := range entries {
		v.Entries[e.Word] = e
		v.GlossToWord[e.Gloss] = e.Word
		v.EntryList = append(v.EntryList, e)
		switch e.Pos {
		case PosNoun:
			v.NounList = append(v.NounList, e.Word)
		case PosVerb:
			v.VerbList = append(v.VerbList, e.Word)
		case PosAdj:
			v.AdjList = append(v.AdjList, e.Word)
		}
	}
	return v, nil
}

// readVocabManifest 解析 manifest（零来源即错误）。
func readVocabManifest(path string) (*VocabManifest, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m VocabManifest
	if err := yamlUnmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("manifest 解析失败: %w", err)
	}
	if len(m.Corpora) == 0 {
		return nil, fmt.Errorf("manifest %s 无任何来源", path)
	}
	return &m, nil
}

// readVocabEntries 逐行解析「英语词\t词性\t中文释义」；注释行/空行跳过。
// 词 ^[a-z]+$、词性白名单、释义非空、词/释义全表唯一——任一破坏即错误。
func readVocabEntries(path string) ([]VocabEntry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }() // 只读句柄：关闭失败无处理面（errcheck 显式化）
	var out []VocabEntry
	seenWord := map[string]bool{}
	seenGloss := map[string]bool{}
	sc := bufio.NewScanner(f)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, "\t")
		if len(parts) != 3 {
			return nil, fmt.Errorf("%s:%d 行格式非法（须为「词\\t词性\\t释义」三列）: %q", path, lineNo, line)
		}
		word, pos, gloss := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]), strings.TrimSpace(parts[2])
		if !isLowerAlpha(word) {
			return nil, fmt.Errorf("%s:%d 词 %q 非规范（须 ^[a-z]+$）", path, lineNo, word)
		}
		switch pos {
		case PosNoun, PosVerb, PosAdj:
		default:
			return nil, fmt.Errorf("%s:%d 词性 %q 不在白名单（n/v/adj）", path, lineNo, pos)
		}
		if gloss == "" {
			return nil, fmt.Errorf("%s:%d 释义为空", path, lineNo)
		}
		if seenWord[word] {
			return nil, fmt.Errorf("%s:%d 词 %q 重复（拼写答案判定域要求全表唯一）", path, lineNo, word)
		}
		if seenGloss[gloss] {
			return nil, fmt.Errorf("%s:%d 释义 %q 重复（释义↔词互证要求全表唯一）", path, lineNo, gloss)
		}
		seenWord[word] = true
		seenGloss[gloss] = true
		out = append(out, VocabEntry{Word: word, Pos: pos, Gloss: gloss})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// isLowerAlpha 判定非空纯小写 ASCII 字母串。
func isLowerAlpha(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 'a' || s[i] > 'z' {
			return false
		}
	}
	return true
}

// dirOf 取目录部分（与 subjectlang 同语义，独立实现）。
func dirOf(p string) string {
	if i := strings.LastIndexAny(p, "/\\"); i >= 0 {
		return p[:i]
	}
	return "."
}
