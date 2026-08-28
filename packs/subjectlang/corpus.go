package subjectlang

import (
	"bufio"
	"fmt"
	"os"
	"strings"
)

// CorpusManifest 是 content/sources/corpus/manifest.yaml 的程序面（最小子集：
// 管线只消费 chars_file/words_file 引用与来源标识；license 语义由
// content/sources/registry.yaml + tools/ci/check_sources.py 承载）。
type CorpusManifest struct {
	Version string         `yaml:"version"`
	Corpora []CorpusSource `yaml:"corpora"`
}

// CorpusSource 单一语料来源（三源分离：source=public 的清单登记在案）。
type CorpusSource struct {
	SourceID    string `yaml:"source_id"`
	Title       string `yaml:"title"`
	License     string `yaml:"license"`
	LicenseNote string `yaml:"license_note"`
	CharsFile   string `yaml:"chars_file"`
	WordsFile   string `yaml:"words_file"`
}

// Corpus 是装载后的语料字/词表（不可变查询面）。
type Corpus struct {
	SourceID string
	Chars    map[string]bool // 单字集合（char_in_corpus 的判定域）
	Words    map[string]bool // 词条集合（word_in_vocab 的判定域）
	CharList []string        // 稳定顺序（生成器索引函数的基底——同序即可回放）
	WordList []string
}

// LoadCorpus 读 manifest 并装载第一个来源的字/词表。
// fail-closed：manifest 缺来源、文件缺列、空表、非法 UTF-8 行一律错误（不降级）。
func LoadCorpus(manifestPath, sourceID string) (*Corpus, error) {
	m, err := readManifest(manifestPath)
	if err != nil {
		return nil, err
	}
	var src *CorpusSource
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
	base := dirOf(manifestPath)
	chars, err := readLines(joinPath(base, src.CharsFile))
	if err != nil {
		return nil, fmt.Errorf("字表装载失败: %w", err)
	}
	words, err := readLines(joinPath(base, src.WordsFile))
	if err != nil {
		return nil, fmt.Errorf("词表装载失败: %w", err)
	}
	if len(chars) == 0 || len(words) == 0 {
		return nil, fmt.Errorf("来源 %q 字/词表为空（空语料=校验器判定域为零，fail-closed）", sourceID)
	}
	c := &Corpus{SourceID: src.SourceID, Chars: map[string]bool{}, Words: map[string]bool{}}
	for _, ch := range chars {
		c.Chars[ch] = true
		c.CharList = append(c.CharList, ch)
	}
	for _, w := range words {
		c.Words[w] = true
		c.WordList = append(c.WordList, w)
	}
	return c, nil
}

// readManifest 用 yaml.v3 解析（与 api/openapi 契约测试同一依赖，间接转直接）。
func readManifest(path string) (*CorpusManifest, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m CorpusManifest
	if err := yamlUnmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("manifest 解析失败: %w", err)
	}
	if len(m.Corpora) == 0 {
		return nil, fmt.Errorf("manifest %s 无任何来源", path)
	}
	return &m, nil
}

// readLines 读非空行集合（去空白与空行；UTF-8 由调用方语言栈保证合法）。
func readLines(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }() // 只读句柄：关闭失败无处理面（errcheck 显式化）
	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line != "" && !strings.HasPrefix(line, "#") {
			out = append(out, line)
		}
	}
	return out, sc.Err()
}

func dirOf(p string) string {
	if i := strings.LastIndexAny(p, "/\\"); i >= 0 {
		return p[:i]
	}
	return "."
}
