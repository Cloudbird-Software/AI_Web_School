package registry

import "testing"

// T-W5-031 验收 #5：双注册表类型落点 + 条目只增不改（重复注册被拒）。

func TestRegistryDuplicateRejected(t *testing.T) {
	reg := New[Entry]()
	if err := reg.Register("single_choice", Entry{ID: "single_choice", Version: "1.0.0"}); err != nil {
		t.Fatalf("首次注册不应失败: %v", err)
	}
	if err := reg.Register("single_choice", Entry{ID: "single_choice", Version: "1.0.1"}); err != ErrDuplicate {
		t.Fatalf("重复注册必须 ErrDuplicate，得到 %v", err)
	}
	if reg.Len() != 1 {
		t.Fatalf("条目数应为 1，得到 %d", reg.Len())
	}
	if got, ok := reg.Get("single_choice"); !ok || got.Version != "1.0.0" {
		t.Fatalf("已注册条目必须可查且保持原版本，得到 %+v ok=%v", got, ok)
	}
}

func TestRegistryGetMissing(t *testing.T) {
	reg := New[Entry]()
	if _, ok := reg.Get("nope"); ok {
		t.Fatal("未注册条目不可查")
	}
}
