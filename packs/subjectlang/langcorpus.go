package subjectlang

import (
	"bufio"
	"fmt"
	"os"
	"strings"
	"unicode/utf8"
)

// langcorpus.go —— 语文轮新增确定性档语料装载（拼音字表 / 词义关系表；manifest
// 读取模式与 corpus.go/subjectenglish vocab.go 同构，独立实现——各档只消费自己的
// 文件引用列）。fail-closed：manifest 缺来源、缺 license、文件缺列/空表/脏数据
// 一律错误（不降级）。

// 语料来源 id（content/sources/corpus/manifest.yaml 登记）。
const (
	SourceDemoChars   = "demo-common-chars-v1"
	SourcePinyinChars = "lang-pinyin-chars-v1"
	SourceWordRel     = "lang-word-rel-v1"
)

// 词义关系白名单（wordrel 表第二列）。
const (
	RelSynonym = "synonym"
	RelAntonym = "antonym"
)

// 规模下限 = 许可留痕口径（manifest license_note 声明；低于下限=节选口径被破坏，
// 且四选项参数空间失去余量）。
const (
	minPinyinChars  = 300
	minWordRelPairs = 100
)

// langFileManifest 是 manifest.yaml 的最小程序面（语文确定性档各来源只消费自己的
// 文件引用列；license 语义由 content/sources/registry.yaml + check_sources 承载）。
type langFileManifest struct {
	Version string           `yaml:"version"`
	Corpora []langFileSource `yaml:"corpora"`
}

type langFileSource struct {
	SourceID    string `yaml:"source_id"`
	Title       string `yaml:"title"`
	License     string `yaml:"license"`
	LicenseNote string `yaml:"license_note"`
	PinyinFile  string `yaml:"pinyin_file"`
	WordRelFile string `yaml:"wordrel_file"`
}

// manifest 文件引用列 → yaml 字段名（readLangSource 按此取对应引用）。
const (
	fieldPinyin  = "pinyin_file"
	fieldWordRel = "wordrel_file"
)

// readLangSource 在 manifest 中定位来源并做许可/文件引用检查，返回语料文件路径
// （X12：许可未登记不得入管线；引用缺失即错误——fail-closed 落装载期）。
func readLangSource(manifestPath, sourceID, field string) (string, error) {
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		return "", err
	}
	var m langFileManifest
	if err := yamlUnmarshal(raw, &m); err != nil {
		return "", fmt.Errorf("manifest 解析失败: %w", err)
	}
	for i := range m.Corpora {
		if m.Corpora[i].SourceID != sourceID {
			continue
		}
		src := m.Corpora[i]
		if src.License == "" {
			return "", fmt.Errorf("来源 %q 缺 license（X12：许可未登记不得入管线）", sourceID)
		}
		name := src.PinyinFile
		if field == fieldWordRel {
			name = src.WordRelFile
		}
		if name == "" {
			return "", fmt.Errorf("来源 %q 缺 %s 引用", sourceID, field)
		}
		return joinPath(dirOf(manifestPath), name), nil
	}
	return "", fmt.Errorf("manifest %s 无来源 %q", manifestPath, sourceID)
}

// ── 拼音字表（拼音选字判定域）──

// PinyinEntry 单条「字→拼音」。
type PinyinEntry struct {
	Char   string
	Pinyin string
}

// PinyinChars 装载后的拼音字表（不可变查询面）。
type PinyinChars struct {
	SourceID string
	Entries  []PinyinEntry       // 稳定顺序（参数空间索引函数的基底——同序即可回放）
	ByChar   map[string]string   // char → pinyin（字全表唯一）
	Correct  map[string][]string // pinyin → 同音字集合（「不在正确集」干扰约束的判定域）
	Finals   map[string][]string // 韵母 → 字集合（同韵干扰的取材域，稳定序）
	FinalOf  map[string]string   // char → 韵母（校验器复核同韵声明用）
}

// LoadPinyinChars 读 manifest 并装载指定来源的拼音字表。
func LoadPinyinChars(manifestPath, sourceID string) (*PinyinChars, error) {
	path, err := readLangSource(manifestPath, sourceID, fieldPinyin)
	if err != nil {
		return nil, err
	}
	entries, err := readPinyinEntries(path)
	if err != nil {
		return nil, fmt.Errorf("拼音字表装载失败: %w", err)
	}
	if len(entries) < minPinyinChars {
		return nil, fmt.Errorf("来源 %q 字表 %d 条低于节选下限 %d（许可留痕口径被破坏）",
			sourceID, len(entries), minPinyinChars)
	}
	p := &PinyinChars{
		SourceID: sourceID,
		ByChar:   make(map[string]string, len(entries)),
		Correct:  map[string][]string{},
		Finals:   map[string][]string{},
		FinalOf:  map[string]string{},
	}
	for _, e := range entries {
		p.Entries = append(p.Entries, e)
		p.ByChar[e.Char] = e.Pinyin
		p.Correct[e.Pinyin] = append(p.Correct[e.Pinyin], e.Char)
		final := syllableFinal(e.Pinyin)
		p.Finals[final] = append(p.Finals[final], e.Char)
		p.FinalOf[e.Char] = final
	}
	return p, nil
}

// readPinyinEntries 逐行解析「汉字\t拼音」；注释行/空行跳过。
// 汉字必须单字且全表唯一、拼音须为小写声韵母+声调符号——任一破坏即错误。
func readPinyinEntries(path string) ([]PinyinEntry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }() // 只读句柄：关闭失败无处理面（errcheck 显式化）
	var out []PinyinEntry
	seen := map[string]bool{}
	sc := bufio.NewScanner(f)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, "\t")
		if len(parts) != 2 {
			return nil, fmt.Errorf("%s:%d 行格式非法（须为「汉字\\t拼音」两列）: %q", path, lineNo, line)
		}
		ch, py := parts[0], strings.TrimSpace(parts[1])
		if !isSingleRune(ch) {
			return nil, fmt.Errorf("%s:%d %q 非单字（判定域=单字）", path, lineNo, ch)
		}
		if !isPinyinSyllable(py) {
			return nil, fmt.Errorf("%s:%d 拼音 %q 非规范（小写声韵母+声调符号）", path, lineNo, py)
		}
		if seen[ch] {
			return nil, fmt.Errorf("%s:%d 汉字 %q 重复（判定域要求全表唯一）", path, lineNo, ch)
		}
		seen[ch] = true
		out = append(out, PinyinEntry{Char: ch, Pinyin: py})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// ── 词义关系表（近义/反义选词判定域）──

// WordRelEntry 单条「词→关系→目标词」。
type WordRelEntry struct {
	Word     string
	Relation string
	Target   string
}

// WordRel 装载后的词义关系表（不可变查询面）。
type WordRel struct {
	SourceID string
	Entries  []WordRelEntry      // 稳定顺序（参数空间索引函数的基底）
	WordSet  map[string]bool     // 全部词（词位∪目标位，干扰项取材域）
	WordList []string            // 稳定序全量词（首现顺序）
	Answer   map[string][]string // word\x00relation → 目标词集（判分地面真值）
}

// relKey 关系查询键（word\x00relation——tab 分隔符不会出现在规范词内）。
func relKey(word, relation string) string { return word + "\x00" + relation }

// AnswerOf 查询 (word, relation) 的目标词集（无关系即空）。
func (w *WordRel) AnswerOf(word, relation string) []string {
	return w.Answer[relKey(word, relation)]
}

// LoadWordRel 读 manifest 并装载指定来源的词义关系表。
func LoadWordRel(manifestPath, sourceID string) (*WordRel, error) {
	path, err := readLangSource(manifestPath, sourceID, fieldWordRel)
	if err != nil {
		return nil, err
	}
	entries, err := readWordRelEntries(path)
	if err != nil {
		return nil, fmt.Errorf("词义关系表装载失败: %w", err)
	}
	if len(entries) < minWordRelPairs {
		return nil, fmt.Errorf("来源 %q 关系对 %d 条低于节选下限 %d（许可留痕口径被破坏）",
			sourceID, len(entries), minWordRelPairs)
	}
	w := &WordRel{
		SourceID: sourceID,
		WordSet:  map[string]bool{},
		Answer:   map[string][]string{},
	}
	// 关系内角色唯一性（词位∪目标位）：杜绝 近义链/反义链——链上第三词会构成
	// 语义第二正确答案，四选题不可判定。
	roleSeen := map[string]map[string]bool{RelSynonym: {}, RelAntonym: {}}
	for _, e := range entries {
		w.Entries = append(w.Entries, e)
		k := relKey(e.Word, e.Relation)
		w.Answer[k] = append(w.Answer[k], e.Target)
		for _, word := range [2]string{e.Word, e.Target} {
			if roleSeen[e.Relation][word] {
				return nil, fmt.Errorf("来源 %q 关系 %s 内词 %q 重复出现（关系链破坏唯一正确性）",
					sourceID, e.Relation, word)
			}
			roleSeen[e.Relation][word] = true
			if !w.WordSet[word] {
				w.WordSet[word] = true
				w.WordList = append(w.WordList, word)
			}
		}
	}
	return w, nil
}

// readWordRelEntries 逐行解析「词\t关系\t目标词」；关系白名单、词非空、
// (词,关系) 唯一、词≠目标词——任一破坏即错误。
func readWordRelEntries(path string) ([]WordRelEntry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }() // 只读句柄：关闭失败无处理面（errcheck 显式化）
	var out []WordRelEntry
	seen := map[string]bool{}
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
			return nil, fmt.Errorf("%s:%d 行格式非法（须为「词\\t关系\\t目标词」三列）: %q", path, lineNo, line)
		}
		word, rel, target := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]), strings.TrimSpace(parts[2])
		switch rel {
		case RelSynonym, RelAntonym:
		default:
			return nil, fmt.Errorf("%s:%d 关系 %q 不在白名单（synonym/antonym）", path, lineNo, rel)
		}
		if word == "" || target == "" {
			return nil, fmt.Errorf("%s:%d 词/目标词为空", path, lineNo)
		}
		if strings.ContainsAny(word, " \t") || strings.ContainsAny(target, " \t") {
			return nil, fmt.Errorf("%s:%d 词含空白（判定域=规范词条）", path, lineNo)
		}
		if word == target {
			return nil, fmt.Errorf("%s:%d 词与目标词相同（%q）", path, lineNo, word)
		}
		k := relKey(word, rel)
		if seen[k] {
			return nil, fmt.Errorf("%s:%d (词,关系) %q 重复", path, lineNo, k)
		}
		seen[k] = true
		out = append(out, WordRelEntry{Word: word, Relation: rel, Target: target})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// ── 共用形态工具 ──

// isSingleRune 判定恰为一个 Unicode 字符。
func isSingleRune(s string) bool {
	return utf8.RuneCountInString(s) == 1
}

// isPinyinSyllable 判定拼音形态：小写字母 + 带调元音（āáǎà ōóǒò ēéěè īíǐì ūúǔù）+ ü/ǖǘǚǜ。
func isPinyinSyllable(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z':
		case r == 'ā' || r == 'á' || r == 'ǎ' || r == 'à':
		case r == 'ō' || r == 'ó' || r == 'ǒ' || r == 'ò':
		case r == 'ē' || r == 'é' || r == 'ě' || r == 'è':
		case r == 'ī' || r == 'í' || r == 'ǐ' || r == 'ì':
		case r == 'ū' || r == 'ú' || r == 'ǔ' || r == 'ù':
		case r == 'ü' || r == 'ǖ' || r == 'ǘ' || r == 'ǚ' || r == 'ǜ':
		default:
			return false
		}
	}
	return true
}

// stripTone 去声调（ā→a …；ü/ǖǘǚǜ→v），供韵母归组。
func stripTone(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch r {
		case 'ā', 'á', 'ǎ', 'à':
			b.WriteByte('a')
		case 'ō', 'ó', 'ǒ', 'ò':
			b.WriteByte('o')
		case 'ē', 'é', 'ě', 'è':
			b.WriteByte('e')
		case 'ī', 'í', 'ǐ', 'ì':
			b.WriteByte('i')
		case 'ū', 'ú', 'ǔ', 'ù':
			b.WriteByte('u')
		case 'ü', 'ǖ', 'ǘ', 'ǚ', 'ǜ':
			b.WriteByte('v')
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// syllableFinal 提取韵母（教学归组口径）：y/w 还原为 i/u 声介后取主韵母。
// 例：tiān→ian、yuè→ve、shuǐ→uei、ōu→ou。归组仅用于干扰项取材（近音启发式），
// 不承担判分语义（判分语义只看全字拼音相等）。
func syllableFinal(py string) string {
	base := stripTone(py)
	switch {
	case strings.HasPrefix(base, "yu"):
		base = "v" + base[2:]
	case strings.HasPrefix(base, "y"):
		base = "i" + base[1:]
	case strings.HasPrefix(base, "w"):
		base = "u" + base[1:]
	}
	for _, ini := range []string{"zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n",
		"l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s"} {
		if strings.HasPrefix(base, ini) && len(base) > len(ini) {
			return base[len(ini):]
		}
	}
	return base
}
