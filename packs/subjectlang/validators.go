package subjectlang

import (
	"fmt"
	"strings"
)

// CharInCorpus 判定：候选单字是否在语料字表内（issue #34 §六「确定性档：
// 拼音→汉字、字词辨认——纯代码 validator char_in_corpus 模式移植」）。
// fail-closed：语料未装载/空表 → 判定失败（绝不静默 pass）。
type CharInCorpus struct{ c *Corpus }

// NewCharInCorpus 构造；语料为 nil 即错误（fail-closed 落构造期）。
func NewCharInCorpus(c *Corpus) (*CharInCorpus, error) {
	if c == nil || len(c.Chars) == 0 {
		return nil, fmt.Errorf("语料未装载或字表为空（char_in_corpus 判定域为零）")
	}
	return &CharInCorpus{c: c}, nil
}

// InCorpus 判定单字是否在字表；多字/空串均为 false（判定域是单字）。
func (v *CharInCorpus) InCorpus(ch string) bool {
	return v.c.Chars[ch]
}

// WordInVocab 判定：候选词是否在语料词表内。fail-closed 同上。
type WordInVocab struct{ c *Corpus }

// NewWordInVocab 构造；词表为空即错误。
func NewWordInVocab(c *Corpus) (*WordInVocab, error) {
	if c == nil || len(c.Words) == 0 {
		return nil, fmt.Errorf("语料未装载或词表为空（word_in_vocab 判定域为零）")
	}
	return &WordInVocab{c: c}, nil
}

// InVocab 判定词条是否在词表；空白归一（首尾 trim）后判定。
func (v *WordInVocab) InVocab(word string) bool {
	return v.c.Words[strings.TrimSpace(word)]
}
