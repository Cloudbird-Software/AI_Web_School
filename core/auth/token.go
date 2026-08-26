package auth

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const (
	// tokenPrefix 是令牌格式版本前缀：将来密钥轮换或方案演进时按前缀
	// 分流，旧令牌不与新逻辑混跑。它也是签名输入的一部分（见 Issue）。
	tokenPrefix = "v1"

	// minSecretLen 是 HMAC 密钥的最小字节数。HMAC-SHA256 的安全强度受
	// 密钥熵约束：32 字节是抵御暴力枚举的工程下限（NIST SP 800-108 建议
	// 密钥长度不低于输出块大小）。启动期短于该值直接拒绝装配。
	minSecretLen = 32

	// maxTokenLen 是单个令牌的长度上限。令牌来自不可信的网络输入，
	// 先卡长度再解码，防止异常巨大的串进入 base64/JSON 解析路径。
	maxTokenLen = 8192
)

// claims 是令牌负载的线上格式。字段名与 time 布局固定为 UTC 秒级 unix
// 时间戳：可读、跨语言、且避免纳秒精度在不同序列化器间的漂移问题。
type claims struct {
	Type      string `json:"typ"`
	SubjectID string `json:"sub"`
	AliasID   string `json:"alias,omitempty"`
	IssuedAt  int64  `json:"iat"`
	ExpiresAt int64  `json:"exp"`
}

// Signer 签发并校验短期 HMAC 令牌。now 以函数注入而不是包级变量：
// 过期边界测试要精确到秒级推进，不允许 sleep 驱动的脆弱测试；生产用
// NewSigner 即可（默认 time.Now）。
type Signer struct {
	key []byte
	now func() time.Time
}

// NewSigner 用密钥构造默认（真实时钟）的 Signer。
func NewSigner(secret []byte) (*Signer, error) {
	return NewSignerWithClock(secret, time.Now)
}

// NewSignerWithClock 构造带注入时钟的 Signer（测试确定性用）。
//
// 密钥要求：非空且不少于 minSecretLen 字节——弱密钥在启动期就该失败，
// 而不是签出一堆一撞即破的令牌等运行期兜底。内部复制密钥切片：调用方
// 之后复用/清零原缓冲不应影响已构造的 Signer（防御性，代价可忽略）。
func NewSignerWithClock(secret []byte, now func() time.Time) (*Signer, error) {
	if now == nil {
		now = time.Now
	}
	if len(secret) == 0 {
		return nil, fmt.Errorf("%w: 密钥为空", ErrInvalidSecret)
	}
	if len(secret) < minSecretLen {
		return nil, fmt.Errorf("%w: 密钥长度 %d 字节，低于安全下限 %d", ErrInvalidSecret, len(secret), minSecretLen)
	}
	key := make([]byte, len(secret))
	copy(key, secret)
	return &Signer{key: key, now: now}, nil
}

// Issue 为主体签发短期令牌。ttl 必须 > 0（"短期"是 D9 语义的一部分：
// 令牌泄漏的影响窗口由 ttl 决定，因此不存在永不过期的合法令牌）。
func (s *Signer) Issue(p Principal, ttl time.Duration) (string, error) {
	if err := validatePrincipal(p); err != nil {
		return "", err
	}
	if ttl <= 0 {
		return "", fmt.Errorf("%w: ttl 必须为正数", ErrInvalidClaims)
	}
	now := s.now().UTC().Truncate(time.Second)
	c := claims{
		Type:      string(p.Role),
		SubjectID: p.SubjectID,
		AliasID:   p.AliasID,
		IssuedAt:  now.Unix(),
		ExpiresAt: now.Add(ttl).Unix(),
	}
	payload, err := json.Marshal(c)
	if err != nil {
		return "", fmt.Errorf("auth: 序列化令牌载荷失败: %w", err)
	}
	payloadB64 := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, s.key)
	// 对 wire 形态（版本前缀 + base64 载荷）整体签名：校验侧无需重新
	// 编码即可比对原始字节，不存在规范化歧义。
	mac.Write([]byte(tokenPrefix + "." + payloadB64))
	sigB64 := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return tokenPrefix + "." + payloadB64 + "." + sigB64, nil
}

// Verify 校验令牌并还原主体。任何失败都以哨兵错误返回，调用方据
// IsAuthenticationError 分类映射 HTTP 状态；具体失败分支只进服务端日志，
// 不进响应体（防校验分支探针）。
func (s *Signer) Verify(token string) (Principal, error) {
	if len(token) == 0 || len(token) > maxTokenLen {
		return Principal{}, ErrMalformedToken
	}
	parts := bytes.Split([]byte(token), []byte{'.'})
	if len(parts) != 3 || string(parts[0]) != tokenPrefix {
		return Principal{}, ErrMalformedToken
	}
	payloadPart := parts[1]
	// 空段是结构残缺而非语义错误：先于 HMAC/JSON 阶段判定，
	// 保证拒绝分类与输入损坏程度对应。
	givenSig, err := base64.RawURLEncoding.Strict().DecodeString(string(parts[2]))
	if err != nil || len(givenSig) == 0 {
		return Principal{}, ErrMalformedToken
	}
	payload, err := base64.RawURLEncoding.Strict().DecodeString(string(payloadPart))
	if err != nil || len(payload) == 0 {
		return Principal{}, ErrMalformedToken
	}
	// 用线上的原始字节计算 HMAC 再恒定时间比较：攻击者无法通过
	// 观察比较耗时逐字节逼近签名。
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(tokenPrefix + "."))
	mac.Write(payloadPart)
	if !hmac.Equal(mac.Sum(nil), givenSig) {
		return Principal{}, ErrBadSignature
	}
	var c claims
	dec := json.NewDecoder(bytes.NewReader(payload))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return Principal{}, ErrInvalidClaims
	}
	if c.IssuedAt <= 0 || c.ExpiresAt <= 0 || c.ExpiresAt < c.IssuedAt {
		return Principal{}, ErrInvalidClaims
	}
	now := s.now().UTC()
	// 严格大于才判过期：now == exp 的瞬间仍有效（语义为"有效期至 exp 含"），
	// 与 Issue 的秒级截断一致，避免边界抖动。
	if now.Unix() > c.ExpiresAt {
		return Principal{}, ErrExpiredToken
	}
	p := Principal{
		Role:      Role(c.Type),
		SubjectID: c.SubjectID,
		AliasID:   c.AliasID,
		IssuedAt:  time.Unix(c.IssuedAt, 0).UTC(),
		ExpiresAt: time.Unix(c.ExpiresAt, 0).UTC(),
	}
	if err := validatePrincipal(p); err != nil {
		// 签名成立但主体模型矛盾（如 staff 携带 alias）：按认证失败拒绝。
		// 正常路径到不了这里（Issue 已拦截），它是纵深防御层。
		return Principal{}, errors.Join(ErrInvalidClaims, err)
	}
	return p, nil
}
