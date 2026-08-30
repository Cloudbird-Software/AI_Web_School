package subjectlang

import (
	"testing"
)

// 组装面：四类确定性母题就位、每母题配独立校验器、参数空间 ≥100、
// langgen 批量口径（n=50）全实例过校验且摘要全局唯一、同 index 可回放。
func TestBuildDeterministicSuite(t *testing.T) {
	suite, err := BuildDeterministicSuite("../../content/sources/corpus/manifest.yaml")
	if err != nil {
		t.Fatalf("组装失败: %v", err)
	}
	want := map[string]bool{
		TplCharRecognize: false,
		TplPinyinChar:    false,
		TplWordRel:       false,
		TplRadical:       false,
	}
	if len(suite.Generators) != len(want) {
		t.Fatalf("母题数 %d ≠ %d", len(suite.Generators), len(want))
	}
	seenDigest := map[string]bool{}
	for _, g := range suite.Generators {
		id := g.Entry().ID
		if _, ok := want[id]; !ok {
			t.Fatalf("意外母题 %q", id)
		}
		want[id] = true
		v := suite.Validators[id]
		if v == nil {
			t.Fatalf("母题 %s 缺独立校验器", id)
		}
		if g.Size() < 100 {
			t.Fatalf("母题 %s 参数空间 %d < 100", id, g.Size())
		}
		// 批量口径：同 index 回放一致（同 seed 同输出）+ 校验器全过 + 摘要全局唯一。
		for i := 0; i < 50; i++ {
			a, err := g.Instance(i)
			if err != nil {
				t.Fatalf("%s Instance(%d): %v", id, i, err)
			}
			b, err := g.Instance(i)
			if err != nil {
				t.Fatal(err)
			}
			if string(mustJSON(a)) != string(mustJSON(b)) {
				t.Fatalf("%s Instance(%d) 回放不一致", id, i)
			}
			if err := v(a); err != nil {
				t.Fatalf("%s Instance(%d) 校验器拒绝: %v", id, i, err)
			}
			digest, err := InstanceDigest(a)
			if err != nil {
				t.Fatal(err)
			}
			if seenDigest[digest] {
				t.Fatalf("%s Instance(%d) 摘要全局重复", id, i)
			}
			seenDigest[digest] = true
		}
	}
	for id, ok := range want {
		if !ok {
			t.Fatalf("母题 %s 未组装", id)
		}
	}
}
