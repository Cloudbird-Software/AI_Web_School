package audio

import (
	"strings"
	"testing"
)

// content_addressing_test.go：音频内容寻址验收。
//   - 冻结实现跨语言地面真值互验（golden 三案例 id 采样 + 字节哈希，Python
//     src/core/audio/content_addressing.py 现算采样值）；
//   - D3 确定性：同参同 id、任何参数变更产生新 id。

// golden 冻结实现采样值（Python 现算：compute_audio_content_id /
// compute_content_hash）.
const (
	goldenIDApple = "2641629083fc4dbe8ab7e6a0355b1ba2" // 苹果|female_standard|140|mock
	goldenIDHello = "da16b5b03b9ce37d69d5fbefc48aa9a6" // Hello World|male_standard|160|mock
	goldenIDText  = "e73b7afdfc8412e23bb456f30c415ff5" // 学生朗读课文|female_gentle|120|mock
	goldenHash    = "sha256:340e5005cb420cc8da7bd475052fe9b5897c4232d8c7a0c64b6118ecc771bbf7"
)

func TestComputeAudioContentIDMatchesFrozen(t *testing.T) {
	cases := []struct {
		name         string
		text         string
		voiceProfile string
		speed        int
		engine       string
		want         string
	}{
		{"中文短文本", "苹果", "female_standard", 140, "mock", goldenIDApple},
		{"英文文本", "Hello World", "male_standard", 160, "mock", goldenIDHello},
		{"学段默认音色组合", "学生朗读课文", "female_gentle", 120, "mock", goldenIDText},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ComputeAudioContentID(tc.text, tc.voiceProfile, tc.speed, tc.engine)
			if got != tc.want {
				t.Fatalf("与冻结实现分歧：got=%s want=%s", got, tc.want)
			}
			if len(got) != 32 {
				t.Fatalf("id 必须为 32 位 hex，得到 %d", len(got))
			}
		})
	}
}

func TestComputeAudioContentIDDeterministic(t *testing.T) {
	a := ComputeAudioContentID("苹果", "female_standard", 140, "mock")
	b := ComputeAudioContentID("苹果", "female_standard", 140, "mock")
	if a != b {
		t.Fatalf("同参必须同 id（D3）：%s != %s", a, b)
	}
}

func TestComputeAudioContentIDParameterChangeProducesNewID(t *testing.T) {
	base := ComputeAudioContentID("苹果", "female_standard", 140, "mock")
	changed := []struct {
		name string
		id   string
	}{
		{"文本变更", ComputeAudioContentID("苹果汁", "female_standard", 140, "mock")},
		{"音色变更", ComputeAudioContentID("苹果", "female_gentle", 140, "mock")},
		{"语速变更", ComputeAudioContentID("苹果", "female_standard", 120, "mock")},
		{"引擎变更", ComputeAudioContentID("苹果", "female_standard", 140, "azure")},
	}
	seen := map[string]bool{base: true}
	for _, c := range changed {
		if c.id == base {
			t.Fatalf("%s 不得产生原 id（D3：任何参数变更产生新 id）", c.name)
		}
		if seen[c.id] {
			t.Fatalf("%s 与其他参数组合碰撞", c.name)
		}
		seen[c.id] = true
	}
}

func TestComputeContentHashMatchesFrozen(t *testing.T) {
	got := ComputeContentHash([]byte("audio:voice-female-standard:140:abc"))
	if got != goldenHash {
		t.Fatalf("与冻结实现分歧：got=%s want=%s", got, goldenHash)
	}
	if !strings.HasPrefix(got, "sha256:") {
		t.Fatal("content_hash 必须带 sha256: 前缀")
	}
	// 同字节同哈希；异字节异哈希.
	if ComputeContentHash([]byte("x")) == ComputeContentHash([]byte("y")) {
		t.Fatal("异字节哈希碰撞（不可能）")
	}
	if ComputeContentHash(nil) != "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatal("空字节哈希与 sha256 空串摘要不符")
	}
}
