package ai

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// 与冻结实现 pii_filter_test 语义对齐的剥离用例：确定性、可审计、启发式.

func TestRedactStripsAllKindsInOrder(t *testing.T) {
	// 组合片段之间用标点分隔。注意：汉字长连排会被地址规则整段回溯吞掉
	// （[\p{Han}]{2,}+关键字 的贪婪语义，冻结实现同构的已知启发式边界），
	// 姓名类用例单独在 TestRedactNamesNumberedByOccurrence 覆盖。
	in := "家住北京市海淀区中关村路，电话13812345678，邮箱sili@example.com，身份证11010119900307889X"
	got, kinds, err := RegexRedactor{}.Redact(in)
	if err != nil {
		t.Fatalf("Redact 意外失败: %v", err)
	}
	for _, bad := range []string{"13812345678", "11010119900307889X", "sili@example.com", "北京市海淀区"} {
		if strings.Contains(got, bad) {
			t.Fatalf("剥离后仍含敏感片段 %q: %s", bad, got)
		}
	}
	wantKinds := []string{PIIIDCard, PIIPhone, PIIEmail, PIIAddress}
	if !reflect.DeepEqual(kinds, wantKinds) {
		t.Fatalf("kinds = %v, want %v（长格式先剥防子串误切）", kinds, wantKinds)
	}
}

func TestRedactNamesNumberedByOccurrence(t *testing.T) {
	got, kinds, err := RegexRedactor{}.Redact("老师点名表扬了同学李四和家长王五")
	if err != nil {
		t.Fatal(err)
	}
	want := "老师点名表扬了学生A和学生B"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if !reflect.DeepEqual(kinds, []string{PIIName}) {
		t.Fatalf("kinds = %v", kinds)
	}
}

func TestRedactKeepsAliasAndCleanText(t *testing.T) {
	alias := "01J9Z0T4Q6MCW3K9S7XN2P8R5A" // ULID 形态 student_alias_id，D7 允许保留
	got, kinds, err := RegexRedactor{}.Redact("alias=" + alias + " 无个人信息")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(got, alias) {
		t.Fatalf("student_alias_id 被误剥: %s", got)
	}
	if len(kinds) != 0 {
		t.Fatalf("干净文本不应有剥离记录: %v", kinds)
	}

	clean, kinds2, _ := RegexRedactor{}.Redact("")
	if clean != "" || kinds2 != nil {
		t.Fatalf("空输入应原样通过: %q %v", clean, kinds2)
	}
}

func TestRedactPhoneNotSubsumedByIdCard(t *testing.T) {
	// 纯手机号文本只触发 phone；身份证先剥的次序保证 18 位串不被切成 phone 子串
	got, kinds, _ := RegexRedactor{}.Redact("联系 13912345678")
	if !reflect.DeepEqual(kinds, []string{PIIPhone}) || strings.Contains(got, "13912345678") {
		t.Fatalf("phone 未剥离: %q %v", got, kinds)
	}
}

// ── T-W5-013 姓名脱敏边界修复：强断言边界用例 ─────────────────────────────
// AC4：精确等值断言（禁止"包含"式弱断言掩盖误伤/漏脱敏）；X1：以上既有用例
// 一字未改。修复前本节用例红（漏脱敏/长数字串截断/失败无信号），修复后全绿。

// 漏脱敏修复：分隔符统一支持全部分支（修复前仅「姓名」分支支持 ：/: 与空白，
// "学生：张三"这类紧邻标点形态整体漏脱敏——fuzz 种子回归同源）.
func TestRedactNameAfterSeparatorVariants(t *testing.T) {
	cases := []struct{ in, want string }{
		{"学生：张三今天值日", "学生A今天值日"},
		{"姓名:赵六的学籍已归档", "学生A的学籍已归档"},
		{"学生 张三举手发言", "学生A举手发言"},
		{"同学，李四交了作业", "学生A交了作业"},
		{"我叫，王五来回答问题", "学生A来回答问题"},
		{"学生、张三值日", "学生A值日"},
	}
	for _, c := range cases {
		got, kinds, err := RegexRedactor{}.Redact(c.in)
		if err != nil {
			t.Fatalf("Redact(%q) 意外失败: %v", c.in, err)
		}
		if got != c.want {
			t.Fatalf("Redact(%q) = %q, want %q", c.in, got, c.want)
		}
		if !reflect.DeepEqual(kinds, []string{PIIName}) {
			t.Fatalf("Redact(%q) kinds = %v, want [name]", c.in, kinds)
		}
	}
}

// AC2：手机号不再截断长数字串中间片段（修复前 100138123456789 被切成 100[PHONE]9）.
func TestRedactPhoneInsideLongDigitRunPreserved(t *testing.T) {
	in := "流水100138123456789截断"
	got, kinds, err := RegexRedactor{}.Redact(in)
	if err != nil {
		t.Fatal(err)
	}
	if got != in {
		t.Fatalf("长数字串中间片段被截断: %q", got)
	}
	if len(kinds) != 0 {
		t.Fatalf("边界否决的候选不得计入 stripped（kinds 诚实性）: %v", kinds)
	}
}

// AC2：身份证同理——18 位候选嵌在更长数字串中时整体保留，不切中间片段.
func TestRedactIDCardInsideLongDigitRunPreserved(t *testing.T) {
	in := "编号0011010119900307889X99"
	got, kinds, _ := RegexRedactor{}.Redact(in)
	if got != in || len(kinds) != 0 {
		t.Fatalf("ID 候选被从长数字串中截切: %q %v", got, kinds)
	}
}

// 保守方向：字母紧邻的号码仍剥离（边界只豁免数字相邻这一种歧义形态）.
func TestRedactPhoneLetterAdjacentStillRedacted(t *testing.T) {
	got, kinds, _ := RegexRedactor{}.Redact("QQ13812345678联系")
	if want := "QQ[PHONE]联系"; got != want || !reflect.DeepEqual(kinds, []string{PIIPhone}) {
		t.Fatalf("got %q %v, want %q [phone]", got, kinds, want)
	}
}

func TestRedactIDCardAtStringStart(t *testing.T) {
	got, kinds, _ := RegexRedactor{}.Redact("11010119900307889X已归档")
	if want := "[ID_CARD]已归档"; got != want || !reflect.DeepEqual(kinds, []string{PIIIDCard}) {
		t.Fatalf("got %q %v, want %q [id_card]", got, kinds, want)
	}
}

// 手工拼接不消费分隔符：连续号码各自成边界，第二个不得因前次替换而漏剥.
func TestRedactConsecutivePhonesBothRedacted(t *testing.T) {
	got, kinds, _ := RegexRedactor{}.Redact("13812345678，13912345678都换了")
	if want := "[PHONE]，[PHONE]都换了"; got != want {
		t.Fatalf("got %q, want %q（连续号码不得漏剥第二个）", got, want)
	}
	if !reflect.DeepEqual(kinds, []string{PIIPhone}) {
		t.Fatalf("kinds = %v", kinds)
	}
}

// 可用性下限：姓名区以外的非姓名片段必须保留（修复不引入整段打码）。
// 「张三丰」仅前两字被启发式覆盖（冻结实现同构的已知局限，剩余「丰」
// 不构成完整姓名残留；全量词典属任务 non_goal）.
func TestRedactNamePartialRedactionKeepsSurroundings(t *testing.T) {
	in := "学生张三丰今天在课上回答了老师提出的问题"
	got, _, err := RegexRedactor{}.Redact(in)
	if err != nil {
		t.Fatal(err)
	}
	if want := "学生A丰今天在课上回答了老师提出的问题"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if !strings.Contains(got, "今天在课上") {
		t.Fatalf("非姓名片段被整段吞掉: %q", got)
	}
}

// 少数民族姓名（·分隔）：指人关键字后的首段必须脱敏（完整姓名覆盖依赖
// 注册名单，启发式按 non_goal 只保证首段消残——至少破坏完整姓名连续性）.
func TestRedactEthnicDotNameLeadingSegment(t *testing.T) {
	in := "学生阿依古丽·买买提问了好问题"
	got, kinds, _ := RegexRedactor{}.Redact(in)
	if want := "学生A古丽·买买提问了好问题"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if !reflect.DeepEqual(kinds, []string{PIIName}) {
		t.Fatalf("kinds = %v", kinds)
	}
	if strings.Contains(got, "阿依古丽") {
		t.Fatalf("完整姓名前段未被破坏: %q", got)
	}
}

// 姓名紧邻数字：号码位被数字占据时姓名规则照常、地址规则不被「号」误触发.
func TestRedactNameAdjacentToDigits(t *testing.T) {
	in := "学生张三3号生日"
	got, _, _ := RegexRedactor{}.Redact(in)
	if want := "学生A3号生日"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

// 姓名与号码混排：kinds 按剥离次序（phone 先于 name）完整留痕.
func TestRedactMixedNameAndPhone(t *testing.T) {
	got, kinds, _ := RegexRedactor{}.Redact("学生张三13812345678联系")
	if want := "学生A[PHONE]联系"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if !reflect.DeepEqual(kinds, []string{PIIPhone, PIIName}) {
		t.Fatalf("kinds = %v, want [phone name]", kinds)
	}
}

// AC5：非合法 UTF-8 输入边界不可判定 → 返回可识别失败信号（fail-closed），
// 原文零改写、零剥离记录；总线侧对非 nil err 一律拒绝.
func TestRedactInvalidUTF8FailsClosed(t *testing.T) {
	in := "学生张三\xff\xfe"
	got, kinds, err := RegexRedactor{}.Redact(in)
	if !errors.Is(err, ErrRedactUncertain) {
		t.Fatalf("err = %v, want ErrRedactUncertain", err)
	}
	if got != in || kinds != nil {
		t.Fatalf("失败信号下不得部分改写: %q %v", got, kinds)
	}
}

// 超长文本：确定性完成剥离，不 panic、无残留（性能面：RE2 线性）.
func TestRedactLongTextNoResidue(t *testing.T) {
	in := strings.Repeat("学生张三，", 2000)
	got, kinds, err := RegexRedactor{}.Redact(in)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(got, "张三") || !reflect.DeepEqual(kinds, []string{PIIName}) {
		t.Fatalf("超长文本剥离异常: kinds=%v 残留=%v", kinds, strings.Contains(got, "张三"))
	}
}
