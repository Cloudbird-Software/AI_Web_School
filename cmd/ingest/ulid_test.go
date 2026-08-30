package main

import (
	"strings"
	"testing"
	"time"
)

// ulidTestRand 是确定性随机源（0xFF 填充等固定模式由测试直接构造字节，
// 这里提供 newULIDAt 可注入的最小 reader）。
type ulidTestRand struct{ b byte }

func (r ulidTestRand) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = r.b
	}
	return len(p), nil
}

// TestEncodeULIDKnownVectors 钉死 ULID 规范两端向量：
//   - 全零 → 26 个 '0'；
//   - 全 0xFF（时间与随机均最大）→ "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"（首字符只含
//     时间最高 3 位，48 位时间最大值在 50 位字段里恰为 7）。
func TestEncodeULIDKnownVectors(t *testing.T) {
	var zero [16]byte
	if got := encodeULID(zero); got != "00000000000000000000000000" {
		t.Fatalf("全零向量: got %q", got)
	}
	var maxed [16]byte
	for i := range maxed {
		maxed[i] = 0xFF
	}
	if got := encodeULID(maxed); got != "7ZZZZZZZZZZZZZZZZZZZZZZZZZ" {
		t.Fatalf("最大值向量: got %q", got)
	}
}

// TestNewULIDAt 形态与位布局：26 字符 Crockford 大写、时间字段（前 10 字符）
// 随毫秒递增且字典序与时间序一致（ULID 可排序的规范承诺）。
func TestNewULIDAt(t *testing.T) {
	base := time.UnixMilli(1_769_000_000_000)
	id1, err := newULIDAt(base, ulidTestRand{b: 0x11})
	if err != nil {
		t.Fatalf("newULIDAt: %v", err)
	}
	if len(id1) != 26 {
		t.Fatalf("长度 = %d, 期望 26", len(id1))
	}
	for i := 0; i < len(id1); i++ {
		if !strings.ContainsRune(crockford, rune(id1[i])) {
			t.Fatalf("字符 %c 不在 Crockford 字母表", id1[i])
		}
	}
	id2, err := newULIDAt(base.Add(time.Millisecond), ulidTestRand{b: 0x11})
	if err != nil {
		t.Fatalf("newULIDAt(+1ms): %v", err)
	}
	if id2[:10] <= id1[:10] {
		t.Fatalf("时间字段未随毫秒递增: %s vs %s", id1[:10], id2[:10])
	}
}

// TestNewULIDUnique 连发唯一性（crypto/rand 路径冒烟）。
func TestNewULIDUnique(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 100; i++ {
		id, err := newULID()
		if err != nil {
			t.Fatalf("newULID: %v", err)
		}
		if seen[id] {
			t.Fatalf("ULID 碰撞: %s", id)
		}
		seen[id] = true
	}
}
