package audio

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/render"
)

// qr.go 承载卷面音频二维码：签名 URL + QR 码语义（冻结实现
// src/core/audio/qr_generator.py T-W4-024 的 Go 重锚定，架构 v2 §4.6/§4.8）。
//
// 卷面印二维码，学生扫码播放听力音频。二维码内容是签名 URL——含时效签名
// （24h 有效），防盗链（ADR §4.8「试卷与音频资源走签名 URL 防盗链」）。
//
// 签名机制（与冻结实现逐字节互验，golden 测试锁定）：
//
//	message = "{audio_id}|{paper_id}|{expires_at_ts}"
//	sig     = HMAC-SHA256(secret, message) hex 前 32 字符
//	url     = "{base_url}/{audio_id}.mp3?paper={paper_id}&exp={ts}&sig={sig}"
//
// 为什么用 HMAC 而非裸 MD5：HMAC 带密钥，攻击者无法伪造签名（防盗链关键）。
// 为什么 24h：卷面二维码印发后学生当天使用；过期后扫码返回 410 Gone。
// 为什么纳入 expires_at_ts：签名与时效绑定，过期后签名失效（防重放）。
// 为什么手工拼查询串（不用 url.Values.Encode）：冻结实现 urlencode 按 dict
// 插入序产出 paper→exp→sig，Encode 按字母序排序——为与冻结 URL 字节一致，
// 按同序构造（验签侧 parse 双序兼容，不受影响）。
//
// QR SVG 位图：复用 core/render.GenerateQRSVG（T-W2-037 已落地）——零新依赖
// 约束下该实现是显式骨架（ErrQRSVGNotImplemented），本包不静默降级：
//   - GenerateSignedURL：签名 URL 全量实现（贴码语义的载荷面），不受骨架影响；
//   - GenerateQR：签名 URL + QR SVG 全量面，骨架期如实上抛骨架错误；
//   - 数据面静态卷产物（listening.go）承载签名 URL，贴码位留白（QRSVG 空），
//     接线专用 QR 实现后经 GenerateQR/本包无改动补齐。
//
// 宪法 D7：audio_id 是内容寻址 hash，不含 PII；paper_id 是卷规格 id，不含 PII。

const (
	// DefaultValidityHours 签名 URL 默认有效期（小时）.
	DefaultValidityHours = 24
	// DefaultBaseURL 签名 URL 默认基础地址（与 MockAudioStorageWriter 的
	// 存储桶一致）.
	DefaultBaseURL = "http://localhost:9000/audio-listening"
	// sigHexLen 签名截取长度（HMAC-SHA256 产出 64 hex，取前 32 足够防伪且
	// URL 更短）.
	sigHexLen = 32
	// qrBoxSize / qrBorder QR SVG 参数（冻结实现 generate_qr_svg 默认
	// box_size=4 / border=1）.
	qrBoxSize = 4
	qrBorder  = 1
)

// ErrQRParam 是 QR 签名参数缺失的哨兵错误（audio_id/paper_id/secret 为空）.
var ErrQRParam = errors.New("audio: QR 签名参数缺失")

// QRSignedURL 是二维码签名 URL 结果（验收 #2）.
type QRSignedURL struct {
	// AudioID / PaperID 签名绑定的资源标识.
	AudioID string
	PaperID string
	// SignedURL 含签名的音频访问 URL（QR 码内容）.
	SignedURL string
	// ExpiresAt 过期时间（UTC）.
	ExpiresAt time.Time
	// QRSVG QR 码 SVG 字符串（可嵌入 HTML/PDF）；仅 GenerateQR 填充.
	QRSVG string
}

// QROptions 是 QR 生成配置（冻结实现 generate_qr 关键字参数对齐）.
type QROptions struct {
	// Secret HMAC 签名密钥（从环境注入，禁止硬编码；必填）.
	Secret string
	// BaseURL 音频服务基础地址（空 → DefaultBaseURL）.
	BaseURL string
	// ValidityHours 签名有效期（小时；<=0 → DefaultValidityHours）.
	ValidityHours int
	// Now 当前时间（零值 → time.Now()，测试注入固定时间）.
	Now time.Time
}

// computeSignature 计算 HMAC-SHA256 签名（冻结实现 _compute_signature 对齐）.
func computeSignature(audioID, paperID string, expiresAtTS int64, secret string) string {
	message := fmt.Sprintf("%s|%s|%d", audioID, paperID, expiresAtTS)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(message))
	return hex.EncodeToString(mac.Sum(nil))[:sigHexLen]
}

// buildSignedURL 构造签名 URL（冻结实现 _build_signed_url 对齐，参数序
// paper→exp→sig 与冻结 urlencode 字节一致；paper 值经 quote_plus 同构转义）.
func buildSignedURL(baseURL, audioID, paperID string, expiresAtTS int64, sig string) string {
	return baseURL + "/" + audioID + ".mp3?paper=" + url.QueryEscape(paperID) +
		"&exp=" + strconv.FormatInt(expiresAtTS, 10) + "&sig=" + sig
}

// GenerateSignedURL 生成卷面音频签名 URL（GenerateQR 的载荷面，全量实现）：
//
//  1. 计算过期时间（now + validity_hours，UTC）；
//  2. 用 HMAC-SHA256 签名 (audio_id, paper_id, expires_at_ts)；
//  3. 构造签名 URL。
//
// audio_id/paper_id/secret 为空 → ErrQRParam 包装错误.
func GenerateSignedURL(audioID, paperID string, opts QROptions) (*QRSignedURL, error) {
	if audioID == "" {
		return nil, fmt.Errorf("%w: audio_id 不能为空", ErrQRParam)
	}
	if paperID == "" {
		return nil, fmt.Errorf("%w: paper_id 不能为空", ErrQRParam)
	}
	if opts.Secret == "" {
		return nil, fmt.Errorf("%w: secret 不能为空（HMAC 签名密钥）", ErrQRParam)
	}
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	now = now.UTC()
	validity := opts.ValidityHours
	if validity <= 0 {
		validity = DefaultValidityHours
	}
	expiresAt := now.Add(time.Duration(validity) * time.Hour)
	expiresAtTS := expiresAt.Unix()

	sig := computeSignature(audioID, paperID, expiresAtTS, opts.Secret)
	base := opts.BaseURL
	if base == "" {
		base = DefaultBaseURL
	}
	return &QRSignedURL{
		AudioID:   audioID,
		PaperID:   paperID,
		SignedURL: buildSignedURL(base, audioID, paperID, expiresAtTS, sig),
		ExpiresAt: expiresAt,
	}, nil
}

// GenerateQR 生成卷面音频二维码全量面（签名 URL + QR 码 SVG）。
// QR SVG 经 render.GenerateQRSVG（box_size=4/border=1，冻结默认）——零新依赖
// 约束下该实现为显式骨架，错误如实上抛（errors.Is(err, render.
// ErrQRSVGNotImplemented) 可判别），不静默降级.
func GenerateQR(audioID, paperID string, opts QROptions) (*QRSignedURL, error) {
	u, err := GenerateSignedURL(audioID, paperID, opts)
	if err != nil {
		return nil, err
	}
	svg, err := render.GenerateQRSVG(u.SignedURL, qrBoxSize, qrBorder)
	if err != nil {
		return nil, fmt.Errorf("audio: QR SVG 生成失败: %w", err)
	}
	u.QRSVG = svg
	return u, nil
}

// VerifyQRURL 验证签名 URL 是否有效（签名正确且未过期；冻结实现
// verify_qr_url 对齐）：true 有效；false 无效（结构不符/签名不符/已过期）。
// 签名比对恒定时间（hmac.Equal，冻结 hmac.compare_digest 同语义）.
func VerifyQRURL(signedURL, secret string, now time.Time) bool {
	parsed, err := url.Parse(signedURL)
	if err != nil {
		return false
	}
	q := parsed.Query()
	paperID := q.Get("paper")
	expStr := q.Get("exp")
	sig := q.Get("sig")
	if paperID == "" || expStr == "" || sig == "" {
		return false
	}
	expiresAtTS, err := strconv.ParseInt(expStr, 10, 64)
	if err != nil {
		return false
	}

	// 从 URL path 提取 audio_id（格式：/{audio_id}.mp3）.
	path := strings.TrimRight(parsed.Path, "/")
	if path == "" || !strings.HasSuffix(path, ".mp3") {
		return false
	}
	audioID := path[strings.LastIndex(path, "/")+1:]
	audioID = strings.TrimSuffix(audioID, ".mp3")

	// 验签（恒定时间比对）.
	expectedSig := computeSignature(audioID, paperID, expiresAtTS, secret)
	if !hmac.Equal([]byte(sig), []byte(expectedSig)) {
		return false
	}

	// 验时效.
	if now.IsZero() {
		now = time.Now()
	}
	return now.UTC().Unix() <= expiresAtTS
}
