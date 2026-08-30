package audio

import (
	"errors"
	"testing"
)

// point_read_test.go：点读验收（冻结实现 point_read.py 跨语言黄金互验）。
//   - 分词黄金（中文逐字/英文按空格/混合保持原序）；
//   - 时间轴语义：均分连续覆盖 [0, duration]、单调有序；
//   - word_timings 精确路径与越界回退；
//   - 编码面错误 fail-loud。

func TestSplitWordsMatchesFrozen(t *testing.T) {
	cases := []struct {
		name string
		text string
		want []string
	}{
		{"中文+英文混合", "苹果 banana", []string{"苹", "果", "banana"}},
		{"英文按空格", "Hello World", []string{"Hello", "World"}},
		{"CJK 夹数字逐字切", "第1题 ab12", []string{"第", "1", "题", "ab12"}},
		{"全角标点随非 CJK 缓冲", "Hi你好，go  go", []string{"Hi", "你", "好", "，go", "go"}},
		{"连续空白不产词", "  a   b  ", []string{"a", "b"}},
		{"纯 CJK 逐字", "苹果", []string{"苹", "果"}},
		{"空串", "", []string{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := SplitWords(tc.text)
			if len(got) != len(tc.want) {
				t.Fatalf("词数分歧：got=%q want=%q", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("第 %d 词分歧：got=%q want=%q", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestPointReadEvenSplitMatchesFrozen(t *testing.T) {
	// Python 现算：text="苹果 banana", duration_ms=1000 → 3 词均分 333ms.
	cases := []struct {
		index   int
		word    string
		startMS int
		endMS   int
	}{
		{0, "苹", 0, 333},
		{1, "果", 333, 666},
		{2, "banana", 666, 1000}, // 最后一词取到音频末尾（整除丢尾归末词）
	}
	for _, tc := range cases {
		r, err := PointRead("aid", tc.index, "苹果 banana", 1000, "http://x/0.mp3", nil)
		if err != nil {
			t.Fatalf("PointRead(%d): %v", tc.index, err)
		}
		if r.Word != tc.word || r.StartMS != tc.startMS || r.EndMS != tc.endMS {
			t.Fatalf("词 %d 分歧：got=(%s,%d,%d) want=(%s,%d,%d)",
				tc.index, r.Word, r.StartMS, r.EndMS, tc.word, tc.startMS, tc.endMS)
		}
		if r.Method != MethodEvenSplit {
			t.Fatalf("method=%s，期望 even_split", r.Method)
		}
		if r.AudioID != "aid" || r.AudioURL != "http://x/0.mp3" || r.WordIndex != tc.index {
			t.Fatalf("透传字段分歧： %+v", r)
		}
	}
}

func TestPointReadTimelineOrderedAndCoversDuration(t *testing.T) {
	// 时间轴排序验收：逐词点读结果必须有序、连续、覆盖 [0, duration].
	text := "低段点读 supports timeline coverage"
	duration := 4001 // 取质数放大整除丢尾
	words := SplitWords(text)
	prevEnd := 0
	for i := range words {
		r, err := PointRead("aid", i, text, duration, "u", nil)
		if err != nil {
			t.Fatalf("PointRead(%d): %v", i, err)
		}
		if r.StartMS != prevEnd {
			t.Fatalf("时间轴断裂：词 %d start=%d 前词 end=%d", i, r.StartMS, prevEnd)
		}
		if r.EndMS <= r.StartMS && i != len(words)-1 {
			t.Fatalf("词 %d 区间非法：[%d,%d)", i, r.StartMS, r.EndMS)
		}
		prevEnd = r.EndMS
	}
	if prevEnd != duration {
		t.Fatalf("时间轴必须覆盖到音频末尾：end=%d duration=%d", prevEnd, duration)
	}
}

func TestPointReadWordTimings(t *testing.T) {
	// Python 现算：index=1 → start=400 end=900 method=word_timings.
	timings := []WordTiming{{StartMS: 0, EndMS: 400}, {StartMS: 400, EndMS: 900}, {StartMS: 900, EndMS: 1000}}
	r, err := PointRead("aid", 1, "苹果 banana", 1000, "u", timings)
	if err != nil {
		t.Fatalf("PointRead: %v", err)
	}
	if r.Method != MethodWordTimings || r.StartMS != 400 || r.EndMS != 900 || r.Word != "果" {
		t.Fatalf("精确时间戳路径分歧： %+v", r)
	}

	// 回退语义（冻结实现同款）：越界检查先于 timings；timings 存在但不覆盖
	// 请求位（index < 词数 且 index >= len(timings)）→ 落估算路径.
	r, err = PointRead("aid", 2, "苹果 banana", 1000, "u", timings[:2])
	if err != nil {
		t.Fatalf("PointRead: %v", err)
	}
	if r.Method != MethodEvenSplit {
		t.Fatalf("timings 不覆盖请求位必须回退估算：got=%s", r.Method)
	}
}

func TestPointReadErrors(t *testing.T) {
	if _, err := PointRead("aid", 0, "", 100, "u", nil); !errors.Is(err, ErrEmptyText) {
		t.Fatalf("空文本必须报 ErrEmptyText：got=%v", err)
	}
	if _, err := PointRead("aid", 0, "   \n\t", 100, "u", nil); !errors.Is(err, ErrEmptyText) {
		t.Fatalf("全空白文本必须报 ErrEmptyText：got=%v", err)
	}
	if _, err := PointRead("aid", 0, "苹果", 0, "u", nil); !errors.Is(err, ErrInvalidDuration) {
		t.Fatalf("duration=0 必须报 ErrInvalidDuration：got=%v", err)
	}
	if _, err := PointRead("aid", 0, "苹果", -5, "u", nil); !errors.Is(err, ErrInvalidDuration) {
		t.Fatalf("负时长必须报 ErrInvalidDuration：got=%v", err)
	}
	if _, err := PointRead("aid", -1, "苹果", 100, "u", nil); !errors.Is(err, ErrWordIndexOutOfRange) {
		t.Fatalf("负索引必须报越界：got=%v", err)
	}
	if _, err := PointRead("aid", 2, "苹果", 100, "u", nil); !errors.Is(err, ErrWordIndexOutOfRange) {
		t.Fatalf("索引=词数必须报越界：got=%v", err)
	}
}
