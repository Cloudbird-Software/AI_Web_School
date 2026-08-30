// content_addressing_test.go 内容寻址移植的验收测试。
//
// 测试策略（契约 D3 + 冻结基准 src/core/models/content_addressing.py）：
//  1. 跨语言地面真值：用冻结 Python 实现（同仓库 src/，Python 3.11 实跑）
//     对共享 JSON 输入计算 digest，期望值硬编码于此（快照钉死）；
//     Go 端以 json.Decoder.UseNumber 解码同一 JSON 文本，保证数字字节
//     逐字一致（Python int/float 文本 = json.Number 字面量）。
//  2. 规范化文本逐字节比对：validators.CanonicalJSON 输出与 Python
//     json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":"))
//     输出完全一致。
//  3. 确定性：同输入跨次调用/并发调用必得同 id（-race 下运行）。
//  4. 敏感性：任一字段变化必改 id；corpus 链顺序变化必改 id。
//  5. fail-closed：非有限数、非法 UTF-8 拒绝计算。
package models

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
)

// ────────────────────────────────────────────────────────────────────
// 地面真值（冻结 Python 实跑产出，禁止手改期望值）
// ────────────────────────────────────────────────────────────────────

const goldenInstancePayload = `{
  "tvd": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "np": {
    "seed": 42,
    "ratio": 0.75,
    "count": 3,
    "label": "小数比较",
    "tags": ["基础", "进阶"],
    "meta": {"level": "apply", "hint": null}
  },
  "pd": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "ed": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "cd": [
    "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    "sha256:5555555555555555555555555555555555555555555555555555555555555555"
  ],
  "l": "zh-CN"
}`

// Python json.dumps(payload, sort_keys=True, ensure_ascii=False,
// separators=(",", ":")) 的逐字节输出。
const goldenCanonicalInstanceJSON = `{"cd":["sha256:4444444444444444444444444444444444444444444444444444444444444444","sha256:5555555555555555555555555555555555555555555555555555555555555555"],"ed":"sha256:3333333333333333333333333333333333333333333333333333333333333333","l":"zh-CN","np":{"count":3,"label":"小数比较","meta":{"hint":null,"level":"apply"},"ratio":0.75,"seed":42,"tags":["基础","进阶"]},"pd":"sha256:2222222222222222222222222222222222222222222222222222222222222222","tvd":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}`

// compute_instance_id(...) 冻结实现输出。
const goldenInstanceID = "sha256:a607b949d0f468d48e20f810dfa9919e9c59631b87b014b2591ed33b98948fdd"

const goldenContentPayload = `{
  "o": {
    "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
    "kp_set_mode": "single",
    "cognitive_level": "apply",
    "gradeband": "M",
    "graph_release": "rel-2024-01"
  },
  "ir": {"interaction_id": "single_choice", "interaction_params": {"shuffle": false}},
  "c": {"blocks": [{"type": "stem", "text": "比较 0.3 与 0.30 的大小"}]},
  "sr": {"scorer_id": "exact_match", "scorer_params": {"case_sensitive": false}},
  "eb": [
    {"option": "A", "error_type": "精度误解", "confidence_rule": "always"},
    {"option": "B", "error_type": "末零忽略", "confidence_rule": "sometimes"}
  ],
  "l": "zh-CN"
}`

const goldenCanonicalContentJSON = `{"c":{"blocks":[{"text":"比较 0.3 与 0.30 的大小","type":"stem"}]},"eb":[{"confidence_rule":"always","error_type":"精度误解","option":"A"},{"confidence_rule":"sometimes","error_type":"末零忽略","option":"B"}],"ir":{"interaction_id":"single_choice","interaction_params":{"shuffle":false}},"l":"zh-CN","o":{"cognitive_level":"apply","gradeband":"M","graph_release":"rel-2024-01","kp_set":[{"code":"math.nal.decimal.compare","dimension":"kp"}],"kp_set_mode":"single"},"sr":{"scorer_id":"exact_match","scorer_params":{"case_sensitive":false}}}`

// compute_canonical_item_version_id(...) 冻结实现输出。
const goldenContentID = "sha256:5c2aa68e70dbfcd85f0ecfb002063faa0675323d57956d82fa27161240f38ee8"

const goldenMaterialContentRef = "minio:materials/sha256:abcdef0123456789"

// compute_material_version_id(...) 冻结实现输出（公式三：直接对字符串字节取摘要）。
const goldenMaterialID = "sha256:dd94107c36b300ff922a6e688e20636866cf2bae1f16562b5328fcc721a721f6"

// 空串输入（SHA-256 空消息已知值，冻结实现实跑确认）。
const goldenEmptyMaterialID = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// decodeUseNumber 以 UseNumber 解码 JSON 对象（数字保持字面量，保证与
// Python json.dumps 的数字文本逐字节一致）。
func decodeUseNumber(t *testing.T, s string) map[string]any {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(s))
	dec.UseNumber()
	var m map[string]any
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("解码 golden payload 失败: %v", err)
	}
	return m
}

// goldenInstanceInputs 从共享 payload 提取公式一入参。
func goldenInstanceInputs(t *testing.T) (string, map[string]any, string, string, []string, string) {
	t.Helper()
	p := decodeUseNumber(t, goldenInstancePayload)
	cdRaw, ok := p["cd"].([]any)
	if !ok {
		t.Fatalf("golden payload cd 字段类型异常: %T", p["cd"])
	}
	cd := make([]string, len(cdRaw))
	for i, d := range cdRaw {
		s, ok := d.(string)
		if !ok {
			t.Fatalf("golden payload cd[%d] 类型异常: %T", i, d)
		}
		cd[i] = s
	}
	np, ok := p["np"].(map[string]any)
	if !ok {
		t.Fatalf("golden payload np 字段类型异常: %T", p["np"])
	}
	return p["tvd"].(string), np, p["pd"].(string), p["ed"].(string), cd, p["l"].(string)
}

// ────────────────────────────────────────────────────────────────────
// 公式一
// ────────────────────────────────────────────────────────────────────

func TestComputeInstanceIDGoldenAgainstFrozenPython(t *testing.T) {
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)
	got, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
	if err != nil {
		t.Fatalf("ComputeInstanceID: %v", err)
	}
	if got != goldenInstanceID {
		t.Fatalf("公式一与冻结 Python 实现不一致:\n got=%s\nwant=%s", got, goldenInstanceID)
	}
}

func TestCanonicalInstanceJSONByteExact(t *testing.T) {
	// 规范化文本逐字节一致是 digest 一致的根本（键序/空白/UTF-8 直出口径）
	p := decodeUseNumber(t, goldenInstancePayload)
	got, err := validators.CanonicalJSON(p)
	if err != nil {
		t.Fatalf("CanonicalJSON: %v", err)
	}
	if got != goldenCanonicalInstanceJSON {
		t.Fatalf("规范化 JSON 与冻结实现不一致:\n got=%s\nwant=%s", got, goldenCanonicalInstanceJSON)
	}
}

func TestComputeInstanceIDDeterministic(t *testing.T) {
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)
	want, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
	if err != nil {
		t.Fatalf("ComputeInstanceID: %v", err)
	}
	const n = 200
	for i := 0; i < n; i++ {
		got, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
		if err != nil {
			t.Fatalf("第 %d 次调用: %v", i, err)
		}
		if got != want {
			t.Fatalf("第 %d 次调用 id 漂移（D3 违反）: %s != %s", i, got, want)
		}
	}
}

func TestComputeInstanceIDConcurrentDeterministic(t *testing.T) {
	// 并发同输入计算：-race 下验证无数据竞争且结果恒一致
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)
	want := goldenInstanceID
	var mu sync.Mutex
	var firstErr error
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				got, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
				if err != nil {
					mu.Lock()
					if firstErr == nil {
						firstErr = fmt.Errorf("并发计算失败: %w", err)
					}
					mu.Unlock()
					return
				}
				if got != want {
					mu.Lock()
					if firstErr == nil {
						firstErr = fmt.Errorf("并发 id 漂移（D3 违反）: %s != %s", got, want)
					}
					mu.Unlock()
					return
				}
			}
		}()
	}
	wg.Wait()
	if firstErr != nil {
		t.Fatalf("%v", firstErr)
	}
}

func TestComputeInstanceIDSensitivity(t *testing.T) {
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)
	base, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
	if err != nil {
		t.Fatalf("baseline: %v", err)
	}

	// 每个用例从基准输入的独立副本出发，只改一个字段
	cases := []struct {
		name string
		mut  func(tvd *string, np *map[string]any, pd *string, ed *string, cd *[]string, locale *string)
	}{
		{"tvd 变化", func(tvd *string, _ *map[string]any, _ *string, _ *string, _ *[]string, _ *string) {
			*tvd = "sha256:" + strings.Repeat("a", 64)
		}},
		{"np 变化", func(_ *string, np *map[string]any, _ *string, _ *string, _ *[]string, _ *string) {
			*np = map[string]any{"seed": json.Number("43")}
		}},
		{"pd 变化", func(_ *string, _ *map[string]any, pd *string, _ *string, _ *[]string, _ *string) {
			*pd = "sha256:" + strings.Repeat("b", 64)
		}},
		{"ed 变化", func(_ *string, _ *map[string]any, _ *string, ed *string, _ *[]string, _ *string) {
			*ed = "sha256:" + strings.Repeat("c", 64)
		}},
		{"locale 变化", func(_ *string, _ *map[string]any, _ *string, _ *string, _ *[]string, locale *string) {
			*locale = "en-US"
		}},
		{"corpus 链增项", func(_ *string, _ *map[string]any, _ *string, _ *string, cd *[]string, _ *string) {
			*cd = append(append([]string{}, *cd...), "sha256:"+strings.Repeat("d", 64))
		}},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			tvd2, np2, pd2, ed2, cd2, locale2 := tvd, np, pd, ed, append([]string{}, cd...), locale
			tc.mut(&tvd2, &np2, &pd2, &ed2, &cd2, &locale2)
			got, err := ComputeInstanceID(tvd2, np2, pd2, ed2, cd2, locale2)
			if err != nil {
				t.Fatalf("ComputeInstanceID: %v", err)
			}
			if got == base {
				t.Fatalf("字段变化未导致 id 变化（D3 违反）: %s", got)
			}
		})
	}
}

func TestComputeInstanceIDCorpusOrderMatters(t *testing.T) {
	// corpus 版本链顺序是谱系的一部分：交换顺序必改 id
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)
	a, err := ComputeInstanceID(tvd, np, pd, ed, cd, locale)
	if err != nil {
		t.Fatalf("ComputeInstanceID: %v", err)
	}
	swapped := []string{cd[1], cd[0]}
	b, err := ComputeInstanceID(tvd, np, pd, ed, swapped, locale)
	if err != nil {
		t.Fatalf("ComputeInstanceID(swapped): %v", err)
	}
	if a == b {
		t.Fatalf("corpus 链顺序变化未导致 id 变化（D3 违反）: %s", a)
	}
}

// ────────────────────────────────────────────────────────────────────
// 公式二
// ────────────────────────────────────────────────────────────────────

func TestComputeCanonicalItemVersionIDGoldenAgainstFrozenPython(t *testing.T) {
	p := decodeUseNumber(t, goldenContentPayload)
	o := p["o"].(map[string]any)
	ir := p["ir"].(map[string]any)
	c := p["c"].(map[string]any)
	sr := p["sr"].(map[string]any)
	eb := p["eb"].([]any)
	locale := p["l"].(string)

	got, err := ComputeCanonicalItemVersionID(o, ir, c, sr, eb, locale)
	if err != nil {
		t.Fatalf("ComputeCanonicalItemVersionID: %v", err)
	}
	if got != goldenContentID {
		t.Fatalf("公式二与冻结 Python 实现不一致:\n got=%s\nwant=%s", got, goldenContentID)
	}

	// 规范化文本逐字节一致
	canon, err := validators.CanonicalJSON(p)
	if err != nil {
		t.Fatalf("CanonicalJSON: %v", err)
	}
	if canon != goldenCanonicalContentJSON {
		t.Fatalf("规范化 JSON 与冻结实现不一致:\n got=%s\nwant=%s", canon, goldenCanonicalContentJSON)
	}
}

func TestComputeCanonicalItemVersionIDDedupHint(t *testing.T) {
	// D3：同内容（六块完全一致 + 同 locale）必得同 id——重复命题作去重提示
	p := decodeUseNumber(t, goldenContentPayload)
	args := func(t *testing.T) (map[string]any, map[string]any, map[string]any, map[string]any, []any, string) {
		return p["o"].(map[string]any), p["ir"].(map[string]any), p["c"].(map[string]any),
			p["sr"].(map[string]any), p["eb"].([]any), p["l"].(string)
	}
	a, err := ComputeCanonicalItemVersionID(args(t))
	if err != nil {
		t.Fatalf("第一次: %v", err)
	}
	b, err := ComputeCanonicalItemVersionID(args(t))
	if err != nil {
		t.Fatalf("第二次: %v", err)
	}
	if a != b {
		t.Fatalf("同内容两次计算 id 不同（去重提示失效）: %s != %s", a, b)
	}
}

// ────────────────────────────────────────────────────────────────────
// 公式三
// ────────────────────────────────────────────────────────────────────

func TestComputeMaterialVersionIDGoldenAgainstFrozenPython(t *testing.T) {
	got, err := ComputeMaterialVersionID(goldenMaterialContentRef)
	if err != nil {
		t.Fatalf("ComputeMaterialVersionID: %v", err)
	}
	if got != goldenMaterialID {
		t.Fatalf("公式三与冻结 Python 实现不一致:\n got=%s\nwant=%s", got, goldenMaterialID)
	}

	empty, err := ComputeMaterialVersionID("")
	if err != nil {
		t.Fatalf("ComputeMaterialVersionID(\"\"): %v", err)
	}
	if empty != goldenEmptyMaterialID {
		t.Fatalf("空串输入与冻结实现不一致:\n got=%s\nwant=%s", empty, goldenEmptyMaterialID)
	}
}

func TestComputeMaterialVersionIDSensitivity(t *testing.T) {
	a, err := ComputeMaterialVersionID("minio:materials/sha256:aaa")
	if err != nil {
		t.Fatalf("ComputeMaterialVersionID: %v", err)
	}
	b, err := ComputeMaterialVersionID("minio:materials/sha256:bbb")
	if err != nil {
		t.Fatalf("ComputeMaterialVersionID: %v", err)
	}
	if a == b {
		t.Fatalf("不同 content_ref 产生同 id（D3 违反）: %s", a)
	}
	if !strings.HasPrefix(a, "sha256:") || !strings.HasPrefix(b, "sha256:") {
		t.Fatalf("公式三返回值必须是 sha256: 前缀口径: %s, %s", a, b)
	}
}

// ────────────────────────────────────────────────────────────────────
// fail-closed
// ────────────────────────────────────────────────────────────────────

func TestComputeInstanceIDFailClosed(t *testing.T) {
	tvd, np, pd, ed, cd, locale := goldenInstanceInputs(t)

	// 非有限浮点拒绝（NaN 无唯一规范文本，落入哈希即成判重盲区）
	bad := map[string]any{"ratio": math.NaN()}
	if _, err := ComputeInstanceID(tvd, bad, pd, ed, cd, locale); err == nil {
		t.Fatal("NaN 参数未拒绝（fail-closed 失效）")
	}
	inf := map[string]any{"ratio": math.Inf(1)}
	if _, err := ComputeInstanceID(tvd, inf, pd, ed, cd, locale); err == nil {
		t.Fatal("Inf 参数未拒绝（fail-closed 失效）")
	}
	// 非法 UTF-8 拒绝（避免 U+FFFD 替换折叠不同字节序列为同一哈希）
	if _, err := ComputeInstanceID(tvd, np, pd, ed, cd, "zh-\xff-CN"); err == nil {
		t.Fatal("非法 UTF-8 locale 未拒绝（fail-closed 失效）")
	}
	// 公式三同样拒绝非法 UTF-8
	if _, err := ComputeMaterialVersionID("minio:\xff"); err == nil {
		t.Fatal("公式三非法 UTF-8 输入未拒绝（fail-closed 失效）")
	}
}
