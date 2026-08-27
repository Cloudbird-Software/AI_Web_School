package ai

import (
	"errors"
	"regexp"
	"strings"
	"testing"
	"unicode"
	"unicode/utf8"
)

// T-W5-013 Go 原生 fuzz：姓名脱敏无残留强断言（移植 tools/fuzz/fuzz_redaction.py
// 的不变式——BRIEF S3「fuzz_redaction.py 的不变式随 Go 移植为原生 fuzz」）。
//
// 运行纪律：`-fuzz` 只在本地手动跑（CI/gate 不执行 fuzz 靶，防时长失控）：
//
//	go test ./core/ai/ -run '^$' -fuzz FuzzRedactNoNameLeak -fuzztime 30s
//
// 普通 `go test` 会把 f.Add 种子与 testdata/fuzz 语料当固定用例回归，作为边界
// 防线常驻。发现的 crasher 落盘 testdata/fuzz/FuzzRedactNoNameLeak/ 后一并入库，
// 转为永久回归用例。
//
// 不变式（与冻结 fuzz 源逐条对应，仅依据 Redact 公开语义推导）：
//  1. 任意输入不 panic；非合法 UTF-8 → 必须返回 ErrRedactUncertain（AC5
//     fail-closed 信号，对应冻结源不变式 1「任意输入必须给出确定输出」）；
//  2. 已注册姓名（语料植入、紧跟指人关键字的 CJK 姓名）在输出中不得残留
//     （强断言：残留即 fail，对应冻结源不变式 3「CJK 姓名连续出现必须被脱敏」）；
//  3. 改写 ⇔ kinds 非空，且改写必含已知标记（占位符或「学生X」别名，对应
//     冻结源不变式 5「任何改写都来自脱敏替换」）；
//  4. 可用性下限（弱断言）：锚点「！」「？」必须保留——脱敏不得吞掉姓名区
//     以外的锚点内容（对应冻结源不变式 4 的不误伤方向，按设计约束弱化为
//     锚点保留而非全文等值：启发式对无关键字上下文本就按冻结语义不保证）。
//
// 语料谱系（设计约束）：中文姓名全谱（单字姓/复姓/少数民族·分隔）、姓名紧邻
// 标点/数字/英文、空串、超长串、非法 UTF-8 序列。

// fuzzSeps：关键字与姓名间的分隔变体（修复前仅「姓名」分支支持 → 漏脱敏主源）。
var fuzzSeps = []string{"", "：", ":", "，", " ", "、", "；", ";"}

// fuzzTail：harness 固定尾巴。姓名恰为其子串时强断言会误报（如「课后」），
// 与 ctx 含姓名同列为不可判定构型，跳过而非失败。
const fuzzTail = "？学生，课后作业！"

var reFuzzAliasMark = regexp.MustCompile(`学生[A-Z]`)

// contractualName 报告 name 是否落在启发式规则的契约覆盖形态内：
// 以 ≥2 个汉字开头、其余为汉字或 ·（少数民族姓名分隔符）。契约外形态
// （拉丁名、前导分隔符等）本就不在正则语义内，跳过强断言只保留不 panic 面。
func contractualName(name string) bool {
	rs := []rune(name)
	if len(rs) < 2 || len(rs) > 64 {
		return false
	}
	if !unicode.Is(unicode.Han, rs[0]) || !unicode.Is(unicode.Han, rs[1]) {
		return false
	}
	for _, r := range rs[2:] {
		if !unicode.Is(unicode.Han, r) && r != '·' {
			return false
		}
	}
	// 别名前缀恰为「学生」：任何脱敏输出（学生A…）都必然含该串，断言不可满足。
	if name == "学生" {
		return false
	}
	return true
}

func FuzzRedactNoNameLeak(f *testing.F) {
	seeds := []struct{ name, ctx string }{
		// 单字姓 / 常见双字名
		{"张三", "今天值日"},
		{"李四", "English mixed"},
		{"王五", "标点，、；："},
		// 复姓四字
		{"欧阳明日", "数学课代表"},
		{"司徒", "雷登来了"},
		// 少数民族姓名 · 分隔
		{"阿依古丽·买买提", "今天"},
		{"买买提·艾力", ""},
		{"张三丰", ""},
		// 姓名紧邻数字 / 邮箱 / 超长上下文
		{"张三", "13812345678"},
		{"赵六", "邮箱a@b.com的"},
		{"张三", strings.Repeat("课堂记录。", 2000)},
		// 空串 / 单字（契约外）/ 非法 UTF-8：只考察不 panic 与失败信号面
		{"", "空白姓名"},
		{"张", "单字不成名"},
		{"\xff\xfe", "binary"},
	}
	for _, s := range seeds {
		f.Add([]byte(s.name), []byte(s.ctx))
	}

	f.Fuzz(func(t *testing.T, nameB, ctxB []byte) {
		if len(nameB)+len(ctxB) > 1<<16 {
			t.Skip("超界语料只保不 panic 面")
		}
		name := string(nameB)
		ctx := string(ctxB)
		if !contractualName(name) || strings.Contains(ctx, name) || strings.Contains(fuzzTail, name) {
			t.Skip("契约外构型：只参与变异，不做强断言")
		}
		sep := fuzzSeps[0]
		if len(ctxB) > 0 {
			sep = fuzzSeps[int(ctxB[0])%len(fuzzSeps)]
		}
		// 「？」隔断上下文与植入段，防上下文尾部关键字被左最先匹配当作姓名
		// 前缀消费的 harness 构型伪影；锚点「！」「？」同时充当可用性下限探针。
		text := "！" + ctx + "？学生" + sep + name + "，课后作业？"

		got, kinds, err := RegexRedactor{}.Redact(text)

		// 不变式 1：确定输出 + 非法 UTF-8 必须给失败信号
		if !utf8.ValidString(text) {
			if !errors.Is(err, ErrRedactUncertain) {
				t.Fatalf("非合法 UTF-8 未给 fail-closed 信号: err=%v", err)
			}
			return
		}
		if err != nil {
			t.Fatalf("合法 UTF-8 输入不得失败: %v", err)
		}

		// 不变式 2（强断言）：已注册姓名零残留
		if strings.Contains(got, name) {
			t.Fatalf("漏脱敏：已注册姓名 %q 残留于输出 %q", name, got)
		}

		// 不变式 4（弱断言）：锚点保留
		if !strings.Contains(got, "！") || !strings.Contains(got, "？") {
			t.Fatalf("锚点被吞（可用性下限）: in=%q out=%q", text, got)
		}

		// 不变式 3：改写 ⇔ kinds 非空 ⇔ 已知标记
		changed := got != text
		if changed != (len(kinds) > 0) {
			t.Fatalf("kinds 与改写不一致: changed=%v kinds=%v", changed, kinds)
		}
		if changed &&
			!strings.Contains(got, "[ID_CARD]") && !strings.Contains(got, "[PHONE]") &&
			!strings.Contains(got, "[EMAIL]") && !strings.Contains(got, "[ADDRESS]") &&
			!strings.Contains(got, "[姓名]") && !reFuzzAliasMark.MatchString(got) {
			t.Fatalf("改写无已知标记（来源不明）: %q", got)
		}
	})
}
