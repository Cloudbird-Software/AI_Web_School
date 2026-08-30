// qr_svg_test.go 锁定 #152 QR SVG 生成与冻结 Python qrcode 实现的逐字节
// 等价性：golden 14 例（SvgPathImage 形态）全覆盖——数字/字母数字/字节
// （含 CJK UTF-8/多字节）、混合分段、v1-v10 与 v40、box/border 组合、
// 掩码自动选择与格式/版本信息 BCH.
package render

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const qrGoldenDir = "testdata/qrsvg"

// qrGoldenMeta manifest.json 单条目（mask 恒 null = 自动选掩码）.
type qrGoldenMeta struct {
	Modules int `json:"modules"`
	Version int `json:"version"`
}

// qrGoldenCases 黄金样例入参表——payload/box/border 从 golden 逆向解码，
// 并经 Python qrcode（冻结库本体）重生成逐字节比对确认.
func qrGoldenCases() []struct {
	name    string
	payload string
	box     int
	border  int
} {
	return []struct {
		name    string
		payload string
		box     int
		border  int
	}{
		{"alnum21_v1", strings.Repeat("a", 21), 4, 1},
		{"alnum22_v2", strings.Repeat("a", 22), 4, 1},
		{"audio_golden_url", "http://localhost:9000/audio-listening/2641629083fc4dbe8ab7e6a0355b1ba2.mp3?paper=paper-e2e&exp=1788134400&sig=73e97478d113ecf05295d8f2d258eddf", 4, 1},
		{"byte183_v10", strings.Repeat("~", 183), 4, 1},
		{"byte_2331_v40", strings.Repeat("~", 2331), 4, 1},
		{"cjk_box7_border3", "https://考卷.example.cn/卷-2026?spec=AB12CD&校验=7", 7, 3},
		{"cjk_utf8", "https://考卷.example.cn/卷-2026?spec=AB12CD&校验=7", 4, 1},
		{"iso_box1_border0", "01234567", 1, 0},
		{"iso_numeric8", "01234567", 4, 1},
		{"mixed_modes", "20260830123456789012345678ABCDEF0123456789012345XY王", 4, 1},
		{"numeric41", "12345678901234567890123456789012345678901", 4, 1},
		{"paper_alnum20", "PAPER-SPEC-2026-0001", 10, 4},
		{"spec_alnum9", "spec-0014", 4, 1},
		{"spec_box10_border4", "spec-0014", 10, 4},
	}
}

func TestGenerateQRSVGGolden(t *testing.T) {
	manifestRaw, err := os.ReadFile(filepath.Join(qrGoldenDir, "manifest.json"))
	if err != nil {
		t.Fatalf("读 manifest.json: %v", err)
	}
	var manifest map[string]qrGoldenMeta
	if err := json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatalf("解析 manifest.json: %v", err)
	}
	if len(manifest) != 14 {
		t.Fatalf("manifest 应含 14 例，得到 %d", len(manifest))
	}
	for _, tc := range qrGoldenCases() {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			meta, ok := manifest[tc.name]
			if !ok {
				t.Fatalf("manifest 缺条目 %s", tc.name)
			}
			golden, err := os.ReadFile(filepath.Join(qrGoldenDir, tc.name+".golden"))
			if err != nil {
				t.Fatalf("读黄金基准: %v", err)
			}
			got, err := GenerateQRSVG(tc.payload, tc.box, tc.border)
			if err != nil {
				t.Fatalf("生成失败: %v", err)
			}
			if got != string(golden) {
				gotB, wantB := []byte(got), []byte(golden)
				i := 0
				for i < len(gotB) && i < len(wantB) && gotB[i] == wantB[i] {
					i++
				}
				t.Fatalf("与黄金基准逐字节分歧（首分歧偏移 %d，got 长度 %d / want 长度 %d）:\ngot:  %.120s\nwant: %.120s",
					i, len(gotB), len(wantB), gotB[i:], wantB[i:])
			}
			// manifest 交叉校验：版本/模块数一致，且 SVG 网格尺寸吻合
			version := qrBestFit(optimalDataChunks([]byte(tc.payload)), 1)
			if version != meta.Version {
				t.Fatalf("版本分歧：got %d want %d", version, meta.Version)
			}
			if modules := version*4 + 17; modules != meta.Modules {
				t.Fatalf("模块数分歧：got %d want %d", modules, meta.Modules)
			}
		})
	}
}

func TestGenerateQRSVGFailLoud(t *testing.T) {
	// v40-M 字节容量 2334 → 2335 字节必超容（DataOverflowError 同语义）
	if _, err := GenerateQRSVG(strings.Repeat("a", 2335), 4, 1); !errors.Is(err, ErrQRSVGTooLarge) {
		t.Fatalf("超容量应锚定 ErrQRSVGTooLarge: %v", err)
	}
	if _, err := GenerateQRSVG("x", 0, 1); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("boxSize<1 应锚定 ErrInvalidCode: %v", err)
	}
	if _, err := GenerateQRSVG("x", 4, -1); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("border<0 应锚定 ErrInvalidCode: %v", err)
	}
}

func TestGenerateQRSVGDeterministic(t *testing.T) {
	a, err := GenerateQRSVG("paper-e2e1", 4, 1)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	b, err := GenerateQRSVG("paper-e2e1", 4, 1)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	if a != b {
		t.Fatal("同参两次生成必须逐字节一致（确定性）")
	}
}

func TestOptimalDataChunks(t *testing.T) {
	// 纯数字整串（≤20 锚定口径）
	segs := optimalDataChunks([]byte("01234567"))
	if len(segs) != 1 || segs[0].mode != qrModeNumber {
		t.Fatalf("纯数字应单数字段: %+v", segs)
	}
	// 混合：≥20 数字 + 短字母 + ≥20 字母数字 + 字节尾
	segs = optimalDataChunks([]byte("20260830123456789012345678ABCDEF0123456789012345XY王"))
	if len(segs) != 3 {
		t.Fatalf("混合串应三段: %+v", segs)
	}
	if segs[0].mode != qrModeNumber || string(segs[0].data) != "20260830123456789012345678" {
		t.Fatalf("第一段应为 26 位数字: %+v", segs[0])
	}
	if segs[1].mode != qrModeAlnum || string(segs[1].data) != "ABCDEF0123456789012345XY" {
		t.Fatalf("第二段应为 24 位字母数字: %+v", segs[1])
	}
	if segs[2].mode != qrModeByte || string(segs[2].data) != "王" {
		t.Fatalf("尾段应为字节模式: %+v", segs[2])
	}
	// 小写字母不在字母数字字符集 → 整串字节段
	segs = optimalDataChunks([]byte("spec-0014"))
	if len(segs) != 1 || segs[0].mode != qrModeByte {
		t.Fatalf("小写串应单字节段: %+v", segs)
	}
	// 空 payload：零段（版本选择回退 v1，与 Python 一致）
	if segs := optimalDataChunks(nil); len(segs) != 0 {
		t.Fatalf("空串应零段: %+v", segs)
	}
}
