package auth

// T-W5-007 凭证治理测试：登记面 fail-closed 两级、Secret 零结构化导出、
// 统一 mask 层矩阵与并发安全。原则同 config_test.go：告警/错误文案可提及
// 变量名与登记名，但任何断言路径都不得出现凭证原值。

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
)

// 测试用凭证值：值本体刻意避开 X3 扫描形态（敏感键名紧邻 =/: 接 16+ 位
// [A-Za-z0-9_-] 串），值串内混入点号进一步断开可匹配游程。短值刻意取长值
// 的子串：值打码若不按长值优先，短值先替换会留下混合残片，矩阵断言可证伪。
const (
	testKeyLong  = "llm.raw.0123456789abcdef0000000001"
	testKeyShort = "raw.0123"
	testAuthKey  = "auth.raw.0123456789abcdef01"
)

// newTestRegistry 构造已加载双凭证的登记面：长值包含短值核心段（子串
// 形态），用于验证值打码的长值优先序不残留混合残片。
func newTestRegistry(t *testing.T) *CredentialRegistry {
	t.Helper()
	r := NewCredentialRegistry()
	specs := []CredentialSpec{
		{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "litellm-gateway",
			Class:  ClassLLM,
			Loader: func() (Secret, bool) { return NewSecret(testKeyLong), true },
		},
		{
			Name: "signing_key", EnvVar: "TEST_SIGNING_KEY", Provider: "in-process HMAC signer",
			Class:  ClassAuth,
			Loader: func() (Secret, bool) { return NewSecret(testKeyShort), true },
		},
	}
	for _, spec := range specs {
		if err := r.Register(spec); err != nil {
			t.Fatalf("登记 %s 失败: %v", spec.Name, err)
		}
	}
	return r
}

// TestCredentialRegistryFailClosedTiers 验收：登记面缺失凭证的处置形态
// 按分级固定——auth 类硬失败（阻断启动）、LLM 类告警 + 调用期拒绝。
func TestCredentialRegistryFailClosedTiers(t *testing.T) {
	t.Run("auth类缺失硬失败", func(t *testing.T) {
		r := NewCredentialRegistry()
		if err := r.Register(CredentialSpec{
			Name: "signing_key", EnvVar: "TEST_SIGNING_KEY", Provider: "p",
			Class:  ClassAuth,
			Loader: func() (Secret, bool) { return Secret{}, false },
		}); err != nil {
			t.Fatal(err)
		}
		warnings, err := r.Validate()
		if err == nil {
			t.Fatal("auth 类凭证缺失必须硬失败（fail-closed）")
		}
		if !errors.Is(err, ErrCredentialMissing) {
			t.Fatalf("错误应可经 ErrCredentialMissing 判别: %v", err)
		}
		if len(warnings) != 0 {
			t.Fatalf("auth 类缺失不产生告警（它是错误）: %v", warnings)
		}
		// 错误文本指向环境变量名（运维可行动），而非任何凭证值。
		if !strings.Contains(err.Error(), "TEST_SIGNING_KEY") {
			t.Fatalf("错误文本应指明缺失的环境变量: %v", err)
		}
	})

	t.Run("llm类缺失告警且调用期拒绝", func(t *testing.T) {
		r := NewCredentialRegistry()
		if err := r.Register(CredentialSpec{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p",
			Class:  ClassLLM,
			Loader: func() (Secret, bool) { return Secret{}, false },
		}); err != nil {
			t.Fatal(err)
		}
		warnings, err := r.Validate()
		if err != nil {
			t.Fatalf("LLM 类缺失不得阻断启动: %v", err)
		}
		if len(warnings) != 1 || !strings.Contains(warnings[0], "TEST_LLM_GATEWAY_KEY") {
			t.Fatalf("LLM 类缺失必须产生指向环境变量的告警: %v", warnings)
		}
		if _, err := r.Resolve("llm_gateway_key"); !errors.Is(err, ErrCredentialMissing) {
			t.Fatalf("调用期必须显式拒绝（ErrCredentialMissing）: %v", err)
		}
	})

	t.Run("两级同时呈现", func(t *testing.T) {
		r := NewCredentialRegistry()
		for _, spec := range []CredentialSpec{
			{Name: "signing_key", EnvVar: "TEST_SIGNING_KEY", Provider: "p", Class: ClassAuth,
				Loader: func() (Secret, bool) { return Secret{}, false }},
			{Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p", Class: ClassLLM,
				Loader: func() (Secret, bool) { return Secret{}, false }},
		} {
			if err := r.Register(spec); err != nil {
				t.Fatal(err)
			}
		}
		warnings, err := r.Validate()
		if err == nil {
			t.Fatal("auth 类缺失必须硬失败")
		}
		if len(warnings) != 1 {
			t.Fatalf("LLM 类缺失的告警必须与 auth 失败同时呈现: %v", warnings)
		}
	})

	t.Run("空值等价缺失", func(t *testing.T) {
		r := NewCredentialRegistry()
		if err := r.Register(CredentialSpec{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p",
			Class:  ClassLLM,
			Loader: func() (Secret, bool) { return NewSecret(""), true },
		}); err != nil {
			t.Fatal(err)
		}
		if warnings, err := r.Validate(); err != nil || len(warnings) != 1 {
			t.Fatalf("loader 返回空值应按缺失告警处置: %v / %v", warnings, err)
		}
	})

	t.Run("未登记名字结构不可达", func(t *testing.T) {
		r := newTestRegistry(t)
		if _, err := r.Validate(); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Resolve("not_registered"); !errors.Is(err, ErrUnknownCredential) {
			t.Fatalf("未登记名字必须被拒（ErrUnknownCredential）: %v", err)
		}
	})

	t.Run("重复登记拒绝", func(t *testing.T) {
		r := NewCredentialRegistry()
		spec := CredentialSpec{Name: "k", EnvVar: "TEST_K", Provider: "p", Class: ClassLLM,
			Loader: func() (Secret, bool) { return Secret{}, false }}
		if err := r.Register(spec); err != nil {
			t.Fatal(err)
		}
		if err := r.Register(spec); !errors.Is(err, ErrDuplicateCredential) {
			t.Fatalf("重复登记必须被拒: %v", err)
		}
	})

	t.Run("非法登记拒绝", func(t *testing.T) {
		okLoader := func() (Secret, bool) { return Secret{}, false }
		for _, spec := range []CredentialSpec{
			{Name: "", EnvVar: "V", Provider: "p", Class: ClassLLM, Loader: okLoader},
			{Name: "k", EnvVar: "", Provider: "p", Class: ClassLLM, Loader: okLoader},
			{Name: "k", EnvVar: "V", Provider: "", Class: ClassLLM, Loader: okLoader},
			{Name: "k", EnvVar: "V", Provider: "p", Class: "vault", Loader: okLoader},
			{Name: "k", EnvVar: "V", Provider: "p", Class: ClassAuth, Loader: nil},
		} {
			r := NewCredentialRegistry()
			if err := r.Register(spec); !errors.Is(err, ErrInvalidCredential) {
				t.Fatalf("登记 %+v 应被拒（ErrInvalidCredential）: %v", spec, err)
			}
		}
	})

	t.Run("校验错误零凭证值", func(t *testing.T) {
		// 一个凭证成功加载、另一个缺失：缺失错误文本不得夹带已加载凭证值。
		r := NewCredentialRegistry()
		if err := r.Register(CredentialSpec{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p", Class: ClassLLM,
			Loader: func() (Secret, bool) { return NewSecret(testKeyLong), true },
		}); err != nil {
			t.Fatal(err)
		}
		if err := r.Register(CredentialSpec{
			Name: "signing_key", EnvVar: "TEST_SIGNING_KEY", Provider: "p", Class: ClassAuth,
			Loader: func() (Secret, bool) { return Secret{}, false },
		}); err != nil {
			t.Fatal(err)
		}
		_, err := r.Validate()
		if err == nil {
			t.Fatal("auth 类缺失必须硬失败")
		}
		if strings.Contains(err.Error(), testKeyLong) {
			t.Fatal("校验错误文本不得包含已加载凭证值")
		}
	})
}

// TestCredentialRegistryResolveLifecycle 解析生命周期：Validate 后直取缓存；
// 跳过 Validate 懒加载一次并缓存；loader 计数证明「每次调用重读」不存在。
func TestCredentialRegistryResolveLifecycle(t *testing.T) {
	calls := 0
	r := NewCredentialRegistry()
	if err := r.Register(CredentialSpec{
		Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p", Class: ClassLLM,
		Loader: func() (Secret, bool) {
			calls++
			return NewSecret(testKeyLong), true
		},
	}); err != nil {
		t.Fatal(err)
	}

	t.Run("先Validate再Resolve只加载一次", func(t *testing.T) {
		if _, err := r.Validate(); err != nil {
			t.Fatal(err)
		}
		if calls != 1 {
			t.Fatalf("Validate 应恰好加载一次: %d", calls)
		}
		for i := 0; i < 3; i++ {
			s, err := r.Resolve("llm_gateway_key")
			if err != nil || s.Reveal() != testKeyLong {
				t.Fatalf("Resolve[%d] 应直取缓存: %v / %q", i, err, s)
			}
		}
		if calls != 1 {
			t.Fatalf("Resolve 不得重复加载: %d", calls)
		}
	})

	t.Run("跳过Validate懒加载并缓存", func(t *testing.T) {
		calls = 0
		r2 := NewCredentialRegistry()
		if err := r2.Register(CredentialSpec{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "p", Class: ClassLLM,
			Loader: func() (Secret, bool) {
				calls++
				return NewSecret(testKeyLong), true
			},
		}); err != nil {
			t.Fatal(err)
		}
		s, err := r2.Resolve("llm_gateway_key")
		if err != nil || s.Reveal() != testKeyLong {
			t.Fatalf("懒加载失败: %v", err)
		}
		if _, err := r2.Resolve("llm_gateway_key"); err != nil {
			t.Fatal(err)
		}
		if calls != 1 {
			t.Fatalf("懒加载必须缓存（第二次不再调 loader）: %d", calls)
		}
		// 懒加载的值同样并入 mask 值打码面。
		if got := r2.Mask("leak " + testKeyLong); strings.Contains(got, testKeyLong) || !strings.Contains(got, Masked) {
			t.Fatalf("懒加载值必须并入打码面: %q", got)
		}
	})
}

// TestSecretZeroStructuredExport 验收：Secret 的一切结构化出口只见掩码。
// 新增序列化通道时必须把该通道追加进本矩阵。
func TestSecretZeroStructuredExport(t *testing.T) {
	raw := testAuthKey
	s := NewSecret(raw)

	t.Run("fmt各动词", func(t *testing.T) {
		got := fmt.Sprintf("%v|%s|%+v|%q|%x", s, s, s, s, s)
		want := "***|***|***|\"***\"|2a2a2a"
		if got != want {
			t.Fatalf("fmt 出口泄漏: got=%q want=%q", got, want)
		}
		if strings.Contains(got, raw) {
			t.Fatal("fmt 出口出现原值")
		}
	})

	t.Run("错误链包装", func(t *testing.T) {
		werr := fmt.Errorf("wrap: %w", fmt.Errorf("detail %v", s))
		if strings.Contains(werr.Error(), raw) || !strings.Contains(werr.Error(), Masked) {
			t.Fatalf("错误链出口泄漏: %q", werr.Error())
		}
	})

	t.Run("encoding_json", func(t *testing.T) {
		b, err := json.Marshal(map[string]Secret{"field": s})
		if err != nil {
			t.Fatal(err)
		}
		if string(b) != `{"field":"***"}` {
			t.Fatalf("JSON 序列化出口泄漏: %s", b)
		}
	})

	t.Run("slog结构化字段", func(t *testing.T) {
		if got := s.LogValue().String(); got != Masked {
			t.Fatalf("slog LogValue 泄漏: %q", got)
		}
		var buf bytes.Buffer
		logger := slog.New(slog.NewTextHandler(&buf, nil))
		logger.Info("op", "credential", s)
		if strings.Contains(buf.String(), raw) || !strings.Contains(buf.String(), Masked) {
			t.Fatalf("slog 输出泄漏: %q", buf.String())
		}
	})

	t.Run("Reveal与Empty语义", func(t *testing.T) {
		if s.Reveal() != raw {
			t.Fatal("Reveal 必须返回原值（唯一出口）")
		}
		var zero Secret
		if !zero.Empty() || s.Empty() {
			t.Fatal("Empty 语义错误")
		}
		if !NewSecret("").Empty() {
			t.Fatal("空串凭证应判 Empty")
		}
	})
}

// TestRegistryMaskMatrix 统一 mask 层矩阵：值打码面（长值优先）与键名层
// （含已登记 EnvVar 与默认词表、JSON/HTTP 头形态、大小写不敏感、幂等）。
func TestRegistryMaskMatrix(t *testing.T) {
	r := newTestRegistry(t)
	if _, err := r.Validate(); err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "长值整段打码",
			in:   "upstream rejected " + testKeyLong,
			want: "upstream rejected " + Masked,
		},
		{
			name: "短值整段打码",
			in:   "signed with " + testKeyShort + " ok",
			want: "signed with " + Masked + " ok",
		},
		{
			name: "双值同行共存",
			in:   "dual " + testKeyLong + " and " + testKeyShort,
			want: "dual " + Masked + " and " + Masked,
		},
		{
			name: "已登记环境变量形态",
			in:   "TEST_LLM_GATEWAY_KEY = abcdef012345",
			want: "TEST_LLM_GATEWAY_KEY=" + Masked,
		},
		{
			name: "默认词表_键值对",
			// 值部贪婪吞到行尾：宁可多打码不漏打码（行为论证见 Mask 注释）。
			in:   "token:abcdef012345 trailing",
			want: "token=" + Masked,
		},
		{
			name: "默认词表_大小写不敏感",
			in:   `{"API_KEY":"abcdef012345"}`,
			want: "{\"API_KEY=" + Masked + "\"}",
		},
		{
			name: "authorization头形态",
			in:   "Authorization: Bearer abc.def.ghi",
			want: "Authorization=" + Masked,
		},
		{
			name: "无敏感形态原样返回",
			in:   "ordinary diagnostic text",
			want: "ordinary diagnostic text",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Mask(tc.in)
			if got != tc.want {
				t.Fatalf("Mask=%q want %q", got, tc.want)
			}
			// 幂等：已打码文本重复处理不再变化。
			if twice := r.Mask(got); twice != got {
				t.Fatalf("Mask 不幂等: %q -> %q", got, twice)
			}
			// 全矩阵总断言：任何输入路径都不残留测试凭证值。
			if strings.Contains(got, testKeyLong) || strings.Contains(got, testKeyShort) {
				t.Fatalf("值打码面泄漏: %q", got)
			}
		})
	}

	t.Run("词表动态追加", func(t *testing.T) {
		r.RegisterSensitiveKeys("private_field")
		if got := r.Mask("private_field=abcdef012345"); got != "private_field="+Masked {
			t.Fatalf("追加词表应生效: %q", got)
		}
	})
}

// TestRegistryMaskConcurrency -race 实证：请求期 Resolve/Mask 与装配期
// 词表扩张并发，读写锁下无数据竞争、打码面只增不减。
func TestRegistryMaskConcurrency(t *testing.T) {
	r := newTestRegistry(t)
	if _, err := r.Validate(); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				if _, err := r.Resolve("llm_gateway_key"); err != nil {
					t.Errorf("Resolve 失败: %v", err)
					return
				}
				if got := r.Mask("op " + testKeyLong + " token:abcdef012345"); strings.Contains(got, testKeyLong) {
					t.Errorf("并发 Mask 泄漏: %q", got)
					return
				}
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for j := 0; j < 50; j++ {
			r.RegisterSensitiveKeys(fmt.Sprintf("extra_key_%d", j))
		}
	}()
	wg.Wait()
}
