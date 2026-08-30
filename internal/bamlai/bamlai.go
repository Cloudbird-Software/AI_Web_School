// Package bamlai 是装配层的 BAML 出站适配器：把 baml_client 函数包装成
// core/ai.Caller（T-W5-014 接缝的 W6 接线，审计 #157）。
//
// 分层纪律：core/ai 只认 Caller 接口、永不 import baml_client（BAML 函数
// 签名演进被隔离在本包）；本包也不接触数据库与台账——出站一律经调用方注入
// 的 Caller（生产=core/ai.BusCaller，调用自动落台账）。
//
// 入参信封：packs 生成器的 draftPrompt 产出「结构化入参行」（task/source_word/
// gradeband，见 packs/subjectlang/sentence_reorg.go 的注释契约），本包严格
// 解包进 BAML 类型化调用——prompt 层唯一事实源是 baml_src/generators/*.baml
// （BAML-1），适配器不做任何 prompt 拼接或改写。
package bamlai

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/baml_client"
	"github.com/Cloudbird-Software/AI_Web_School/baml_client/types"
	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// TaskSentenceReorg 是句子重组出站信封的 task 行取值（与 packs 侧
// draftPrompt 固定首行一致；不一致即契约漂移，fail-closed 拒绝）.
const TaskSentenceReorg = "lang_sentence_reorg"

// SentenceReorgFunc 是 BAML 函数的函数指针形态（测试注入 fake，不触网）.
type SentenceReorgFunc func(ctx context.Context, sourceWord, gradeband string, opts ...baml_client.CallOptionFunc) (types.SentenceReorg, error)

// SentenceReorgCaller 把 baml_client.GenerateSentenceReorg 适配成 ai.Caller.
//
// usage 口径：每次调用挂独立 Collector（D10 台账 token 计量的真实来源）；
// Collector 创建/读取失败不阻断出站（TokenIn/TokenOut 留零，总线 TokenCounter
// 兜底计数）——计量降级不改变调用语义与失败语义.
type SentenceReorgCaller struct {
	Fn SentenceReorgFunc
	// collectorName 为空取缺省（ NewCollector 命名仅用于观测辨识）.
	collectorName string
}

// NewSentenceReorgCaller 构造生产适配器（绑定生成面真函数）.
func NewSentenceReorgCaller() SentenceReorgCaller {
	return SentenceReorgCaller{Fn: baml_client.GenerateSentenceReorg}
}

// NewSentenceReorgCallerWithFunc 构造测试适配器（注入 fake Fn，不触网）.
func NewSentenceReorgCallerWithFunc(fn SentenceReorgFunc) SentenceReorgCaller {
	return SentenceReorgCaller{Fn: fn}
}

// SetCollectorName 显式声明 usage collector 观测名.
func (c *SentenceReorgCaller) SetCollectorName(name string) { c.collectorName = name }

// Call 实现 ai.Caller：严格解包结构化入参 → 类型化 BAML 调用 → draft JSON。
// 出站内容即 BAML 输出契约的 JSON 序列化（字段与
// packs/subjectlang.SentenceReorgDraft 一一对应，超纲字段不存在）.
func (c SentenceReorgCaller) Call(ctx context.Context, req ai.OutboundRequest) (ai.OutboundResult, error) {
	sw, gb, err := ParseSentenceReorgRequest(req.Prompt)
	if err != nil {
		return ai.OutboundResult{}, err
	}
	if c.Fn == nil {
		return ai.OutboundResult{}, fmt.Errorf("bamlai: SentenceReorgCaller.Fn 未注入")
	}
	var opts []baml_client.CallOptionFunc
	col, colErr := baml_client.NewCollector(c.collectorName)
	if colErr == nil {
		opts = append(opts, baml_client.WithCollector(col))
	}
	draft, err := c.Fn(ctx, sw, gb, opts...)
	if err != nil {
		// 错误文本不得包含 prompt 原文（X3/D7）；BAML 错误已由其运行时脱敏
		// 到错误类，这里只加定位前缀.
		return ai.OutboundResult{}, fmt.Errorf("bamlai: GenerateSentenceReorg 出站失败: %w", err)
	}
	b, err := json.Marshal(draft)
	if err != nil {
		return ai.OutboundResult{}, fmt.Errorf("bamlai: draft 序列化失败: %w", err)
	}
	out := ai.OutboundResult{Content: string(b)}
	if colErr == nil {
		if u, uerr := col.Usage(); uerr == nil {
			if n, nerr := u.InputTokens(); nerr == nil {
				out.TokenIn = int(n)
			}
			if n, nerr := u.OutputTokens(); nerr == nil {
				out.TokenOut = int(n)
			}
		}
	}
	return out, nil
}

// ParseSentenceReorgRequest 严格解析句子重组出站信封：逐行 key: value，
// 三键齐备且无未知键、值非空、task 行精确匹配。任何漂移都是 pack 侧契约
// 变更——在此 fail-closed，绝不猜默认值继续出站.
func ParseSentenceReorgRequest(prompt string) (sourceWord, gradeband string, err error) {
	const (
		keyTask        = "task"
		keySourceWord  = "source_word"
		keyGradeband   = "gradeband"
		expectedKeyNum = 3
	)
	seen := map[string]string{}
	for i, line := range strings.Split(prompt, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			// 行内容不回显（X3/D7）：错误文本不得携带 prompt 原文片段.
			return "", "", fmt.Errorf("bamlai: 信封第 %d 行格式非法（缺少 key: value 冒号，内容不回显）", i+1)
		}
		k, v = strings.TrimSpace(k), strings.TrimSpace(v)
		switch k {
		case keyTask, keySourceWord, keyGradeband:
			if _, dup := seen[k]; dup {
				return "", "", fmt.Errorf("bamlai: 信封键重复: %s", k)
			}
			if v == "" {
				return "", "", fmt.Errorf("bamlai: 信封键值为空: %s", k)
			}
			seen[k] = v
		default:
			return "", "", fmt.Errorf("bamlai: 信封未知键: %s", k)
		}
	}
	if len(seen) != expectedKeyNum {
		return "", "", fmt.Errorf("bamlai: 信封键不齐（%d/%d）", len(seen), expectedKeyNum)
	}
	if seen[keyTask] != TaskSentenceReorg {
		return "", "", fmt.Errorf("bamlai: task 不符: %s", seen[keyTask])
	}
	return seen[keySourceWord], seen[keyGradeband], nil
}
