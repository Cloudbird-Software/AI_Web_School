package auth

// 本文件覆盖令牌签发/校验的拒绝路径（T-W5-005 验收 #2/#5 的核心语义）。
// 白盒测试（同包）：允许直接构造带签名但不合法的载荷，验证纵深防御层；
// 时钟一律注入推进，禁止 sleep 驱动的脆弱测试。

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

const testKeyMaterial = "0123456789abcdef0123456789abcdef-test-secret-t-w5-005"

var testAliasUUID = "3f2a7c1e-5d64-4a0f-9c8b-1e2d3a4b5c6d"

// newClockSigner 返回注入时钟的 Signer 与"把时钟拨快 d"的操作柄。
func newClockSigner(t *testing.T) (*Signer, func(time.Duration)) {
	t.Helper()
	now := time.Unix(1_700_000_000, 0).UTC()
	s, err := NewSignerWithClock([]byte(testKeyMaterial), func() time.Time { return now })
	if err != nil {
		t.Fatalf("构造测试 Signer 失败: %v", err)
	}
	return s, func(d time.Duration) { now = now.Add(d) }
}

func studentPrincipal() Principal {
	return Principal{Role: RoleStudent, SubjectID: "account-1", AliasID: testAliasUUID}
}

func staffPrincipal() Principal {
	return Principal{Role: RoleStaff, SubjectID: "staff-1"}
}

// TestIssueVerifyRoundtripAllRoles 四类主体各签发一次并校验往返等价，
// 同时断言 iat/exp 被如实编码（验收 #1：令牌含类型/id/签发/过期）。
func TestIssueVerifyRoundtripAllRoles(t *testing.T) {
	s, advance := newClockSigner(t)
	cases := []struct {
		name  string
		p     Principal
		ttl   time.Duration
		alias string // 期望还原出的 AliasID
	}{
		{"student", studentPrincipal(), 15 * time.Minute, testAliasUUID},
		{"staff", staffPrincipal(), time.Hour, ""},
		{"ops", Principal{Role: RoleOps, SubjectID: "ops-9"}, time.Hour, ""},
		{"service", Principal{Role: RoleService, SubjectID: "job-nightly"}, 24 * time.Hour, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			token, err := s.Issue(tc.p, tc.ttl)
			if err != nil {
				t.Fatalf("签发失败: %v", err)
			}
			got, err := s.Verify(token)
			if err != nil {
				t.Fatalf("校验失败: %v", err)
			}
			if got.Role != tc.p.Role || got.SubjectID != tc.p.SubjectID || got.AliasID != tc.alias {
				t.Fatalf("主体还原不一致: got %+v want(ROLE=%s SUB=%s ALIAS=%q)", got, tc.p.Role, tc.p.SubjectID, tc.alias)
			}
			if !got.IssuedAt.Equal(got.ExpiresAt.Add(-tc.ttl)) {
				t.Fatalf("iat/exp 编码不一致: ttl=%v iat=%v exp=%v", tc.ttl, got.IssuedAt, got.ExpiresAt)
			}
			advance(tc.ttl - time.Second)
			if _, err := s.Verify(token); err != nil {
				t.Fatalf("有效期内不应拒绝: %v", err)
			}
			advance(2 * time.Second)
			if _, err := s.Verify(token); !errors.Is(err, ErrExpiredToken) {
				t.Fatalf("过期后应返回 ErrExpiredToken，得到 %v", err)
			}
		})
	}
}

// TestIssueRejectsInvalidPrincipals 主体模型非法在签发口就被拦截：
// 学生必须绑 alias、非学生不得携带 alias（D9 最小权限的结构保证）。
func TestIssueRejectsInvalidPrincipals(t *testing.T) {
	s, _ := newClockSigner(t)
	cases := []struct {
		name string
		p    Principal
		ttl  time.Duration
		want error
	}{
		{"学生缺alias", Principal{Role: RoleStudent, SubjectID: "a"}, time.Minute, ErrInvalidSubject},
		{"学生空subject", Principal{Role: RoleStudent, AliasID: testAliasUUID}, time.Minute, ErrInvalidSubject},
		{"staff带alias", Principal{Role: RoleStaff, SubjectID: "s", AliasID: testAliasUUID}, time.Minute, ErrInvalidSubject},
		{"未知role", Principal{Role: "hacker", SubjectID: "x"}, time.Minute, ErrInvalidSubject},
		{"零值主体", Principal{}, time.Minute, ErrInvalidSubject},
		{"零ttl", studentPrincipal(), 0, ErrInvalidClaims},
		{"负ttl", studentPrincipal(), -time.Second, ErrInvalidClaims},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := s.Issue(tc.p, tc.ttl); !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
		})
	}
}

// TestNewSignerSecretPolicy 弱密钥在装配期失败而不是运行期兜底。
func TestNewSignerSecretPolicy(t *testing.T) {
	cases := []struct {
		name   string
		secret []byte
		want   error
	}{
		{"空密钥", nil, ErrInvalidSecret},
		{"短密钥", []byte("too-short"), ErrInvalidSecret},
		{"边界31字节", make([]byte, minSecretLen-1), ErrInvalidSecret},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewSigner(tc.secret); !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
		})
	}
	s, err := NewSignerWithClock(make([]byte, minSecretLen), nil)
	if err != nil {
		t.Fatalf("nil 时钟应回落到默认时钟: %v", err)
	}
	if _, err := s.Issue(studentPrincipal(), time.Minute); err != nil {
		t.Fatalf("nil 时钟构造的 Signer 应可用: %v", err)
	}
}

// TestSignerKeyIsolation 构造后修改调用方的密钥缓冲不影响 Signer
// （内部复制切片），防止共享缓冲导致的静默换钥。
func TestSignerKeyIsolation(t *testing.T) {
	key := []byte(testKeyMaterial)
	s, err := NewSigner(key)
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	token, err := s.Issue(studentPrincipal(), time.Minute)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	for i := range key {
		key[i] = 'x'
	}
	if _, err := s.Verify(token); err != nil {
		t.Fatalf("密钥缓冲隔离被破坏: %v", err)
	}
}

// TestVerifyExpiryBoundary 严格大于才算过期（now == exp 仍有效），
// 与 Issue 的秒级截断一致，杜绝真实时钟下的边界抖动。
func TestVerifyExpiryBoundary(t *testing.T) {
	s, advance := newClockSigner(t)
	token, err := s.Issue(studentPrincipal(), time.Hour)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	advance(time.Hour)
	if _, err := s.Verify(token); err != nil {
		t.Fatalf("now == exp 应仍有效: %v", err)
	}
	advance(time.Second)
	if _, err := s.Verify(token); !errors.Is(err, ErrExpiredToken) {
		t.Fatalf("now > exp 应过期: %v", err)
	}
}

// flipB64Char 把 base64url 串的首个可换字符替换为字母表中另一合法字符，
// 保证被篡改串仍能通过 base64 解码、恰好落在 HMAC 比对阶段。
func flipB64Char(s string) string {
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	out := []byte(s)
	for i, c := range out {
		if c != alphabet[0] {
			out[i] = alphabet[0]
			return string(out)
		}
		out[i] = alphabet[1]
	}
	return string(out)
}

// TestVerifyRejectsTampering 签名或载荷任一字节变动都必须落在
// ErrBadSignature——攻击者无法定位哪段在保护范围内。
func TestVerifyRejectsTampering(t *testing.T) {
	s, _ := newClockSigner(t)
	token, err := s.Issue(studentPrincipal(), time.Minute)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	parts := strings.SplitN(token, ".", 3)
	if len(parts) != 3 {
		t.Fatalf("令牌应为三段: %q", token)
	}
	cases := map[string]string{
		"篡改载荷": parts[0] + "." + flipB64Char(parts[1]) + "." + parts[2],
		"篡改签名": parts[0] + "." + parts[1] + "." + flipB64Char(parts[2]),
	}
	for name, tok := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := s.Verify(tok); !errors.Is(err, ErrBadSignature) {
				t.Fatalf("err = %v, want ErrBadSignature", err)
			}
		})
	}
	// 原令牌不受篡改分支影响（守卫上面的翻转没碰原件）。
	if _, err := s.Verify(token); err != nil {
		t.Fatalf("原令牌应仍可用: %v", err)
	}
}

// TestVerifyMalformedTable 结构非法的输入全部落到 ErrMalformedToken，
// 且不得 panic（该入口直接暴露在网络输入面前）。
func TestVerifyMalformedTable(t *testing.T) {
	s, _ := newClockSigner(t)
	big := "v1." + strings.Repeat("A", maxTokenLen)
	cases := map[string]string{
		"空串":       "",
		"无点号":      "garbage",
		"只有一段":     "v1onlyone",
		"缺签名段":     "v1.AAAA",
		"版本前缀错误":   "v2.AAAA.BBBB",
		"坏base64":  "v1.@@@^.###",
		"带padding": "v1.AAAA==.BBBB==",
		"内嵌空白":     " v1.AAAA.BBBB",
		"前缀后空段":    "v1..",
		"超长令牌":     big,
	}
	for name, tok := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := s.Verify(tok); !errors.Is(err, ErrMalformedToken) {
				t.Fatalf("err = %v, want ErrMalformedToken", err)
			}
		})
	}
}

// TestTokenWireFields 验收 #1 的线上形态实证：负载确含类型/主体 id/
// alias/签发时间/过期时间五个字段，键名稳定不随 Go 结构体重命名漂移。
func TestTokenWireFields(t *testing.T) {
	s, _ := newClockSigner(t)
	token, err := s.Issue(studentPrincipal(), time.Minute)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	parts := strings.SplitN(token, ".", 3)
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatalf("载荷解码失败: %v", err)
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatalf("载荷必须是 JSON 对象: %v", err)
	}
	for _, k := range []string{"typ", "sub", "alias", "iat", "exp"} {
		if _, ok := fields[k]; !ok {
			t.Fatalf("负载缺少字段 %q: %s", k, raw)
		}
	}
	if fields["typ"] != string(RoleStudent) || fields["alias"] != testAliasUUID {
		t.Fatalf("字段值不符: %v", fields)
	}
}

// mustCraft 用 Signer 的真实密钥直接对任意 claims 签名（白盒），用于抵达
// "签名成立但载荷语义非法"的纵深防御分支——正常 Issue 永远造不出这类令牌。
func mustCraft(t *testing.T, s *Signer, c claims) string {
	t.Helper()
	payload, err := json.Marshal(c)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	payloadB64 := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(tokenPrefix + "." + payloadB64))
	return tokenPrefix + "." + payloadB64 + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// TestVerifyRejectsForeignClaimCombos 密钥若泄漏，伪造者混搭出的
// "非学生带 alias / 学生缺 alias / 未知类型 / 时间倒挂 / 多余字段 /
// 缺时间字段"载荷在校验侧仍被拒——验签不是唯一防线。
func TestVerifyRejectsForeignClaimCombos(t *testing.T) {
	s, _ := newClockSigner(t)
	base := int64(1_700_000_000)
	cases := map[string]claims{
		"staff携带alias":   {Type: string(RoleStaff), SubjectID: "s", AliasID: testAliasUUID, IssuedAt: base, ExpiresAt: base + 60},
		"service携带alias": {Type: string(RoleService), SubjectID: "j", AliasID: testAliasUUID, IssuedAt: base, ExpiresAt: base + 60},
		"学生缺alias":       {Type: string(RoleStudent), SubjectID: "a", IssuedAt: base, ExpiresAt: base + 60},
		"未知类型":           {Type: "root", SubjectID: "x", IssuedAt: base, ExpiresAt: base + 60},
		"exp早于iat":       {Type: string(RoleStaff), SubjectID: "s", IssuedAt: base + 60, ExpiresAt: base},
		"缺iat":           {Type: string(RoleStaff), SubjectID: "s", ExpiresAt: base + 60},
		"缺exp":           {Type: string(RoleStaff), SubjectID: "s", IssuedAt: base},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := s.Verify(mustCraft(t, s, c))
			if !errors.Is(err, ErrInvalidClaims) {
				t.Fatalf("err = %v, want ErrInvalidClaims", err)
			}
		})
	}

	t.Run("多余字段", func(t *testing.T) {
		// DisallowUnknownFields：未来/异构实现多塞的字段直接判非法，
		// 不静默忽略（静默忽略意味着两端对同一令牌解释不一致）。
		payload := `{"typ":"staff","sub":"s","iat":1700000000,"exp":1700000060,"admin":true}`
		payloadB64 := base64.RawURLEncoding.EncodeToString([]byte(payload))
		mac := hmac.New(sha256.New, s.key)
		mac.Write([]byte(tokenPrefix + "." + payloadB64))
		token := tokenPrefix + "." + payloadB64 + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
		if _, err := s.Verify(token); !errors.Is(err, ErrInvalidClaims) {
			t.Fatalf("err = %v, want ErrInvalidClaims", err)
		}
	})

	t.Run("坏JSON但签名正确", func(t *testing.T) {
		payloadB64 := base64.RawURLEncoding.EncodeToString([]byte(`{not-json`))
		mac := hmac.New(sha256.New, s.key)
		mac.Write([]byte(tokenPrefix + "." + payloadB64))
		token := tokenPrefix + "." + payloadB64 + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
		if _, err := s.Verify(token); !errors.Is(err, ErrInvalidClaims) {
			t.Fatalf("err = %v, want ErrInvalidClaims", err)
		}
	})
}
