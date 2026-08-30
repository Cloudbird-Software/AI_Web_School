package audio

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/render"
)

// qr_test.go：卷面贴码验收（冻结实现 qr_generator.py 跨语言黄金互验）。
//   - 签名 URL 字节级黄金（Python 现算：HMAC-SHA256 前 32 hex + urlencode 序）；
//   - 验签：正确/过期/篡改/错密钥/结构破损；
//   - QR SVG 骨架面如实上抛（不静默降级）。

// 黄金采样（Python datetime(2026,8,30,tzinfo=utc) + 24h）.
const (
	goldenQRAudioID  = "2641629083fc4dbe8ab7e6a0355b1ba2"
	goldenQRPaperID  = "paper-e2e"
	goldenQRSigSeed  = "e2e-default-secret"
	goldenQRExpTS    = 1788134400
	goldenQRSig      = "73e97478d113ecf05295d8f2d258eddf"
	goldenQRFullURL  = "http://localhost:9000/audio-listening/2641629083fc4dbe8ab7e6a0355b1ba2.mp3?paper=paper-e2e&exp=1788134400&sig=73e97478d113ecf05295d8f2d258eddf"
	qrGoldenNowEpoch = 1788048000 // 2026-08-30T00:00:00Z
)

func qrGoldenNow() time.Time { return time.Unix(qrGoldenNowEpoch, 0).UTC() }

func TestGenerateSignedURLMatchesFrozen(t *testing.T) {
	u, err := GenerateSignedURL(goldenQRAudioID, goldenQRPaperID, QROptions{
		Secret: goldenQRSigSeed,
		Now:    qrGoldenNow(),
	})
	if err != nil {
		t.Fatalf("GenerateSignedURL: %v", err)
	}
	if u.SignedURL != goldenQRFullURL {
		t.Fatalf("签名 URL 与冻结实现分歧：\ngot=%s\nwant=%s", u.SignedURL, goldenQRFullURL)
	}
	if u.ExpiresAt.Unix() != goldenQRExpTS {
		t.Fatalf("过期时间分歧：got=%d want=%d", u.ExpiresAt.Unix(), goldenQRExpTS)
	}
	if u.AudioID != goldenQRAudioID || u.PaperID != goldenQRPaperID {
		t.Fatalf("绑定标识分歧：%+v", u)
	}
	// 缺省面：BaseURL 空 → DefaultBaseURL（黄金 URL 即缺省基址）；24h 缺省时效
	// 已由黄金 URL 隐含断言.
}

func TestGenerateSignedURLErrorAndDeterminism(t *testing.T) {
	if _, err := GenerateSignedURL("", "p", QROptions{Secret: "s"}); !errors.Is(err, ErrQRParam) {
		t.Fatalf("空 audio_id 必须报 ErrQRParam：got=%v", err)
	}
	if _, err := GenerateSignedURL("a", "", QROptions{Secret: "s"}); !errors.Is(err, ErrQRParam) {
		t.Fatalf("空 paper_id 必须报 ErrQRParam：got=%v", err)
	}
	if _, err := GenerateSignedURL("a", "p", QROptions{}); !errors.Is(err, ErrQRParam) {
		t.Fatalf("空 secret 必须报 ErrQRParam：got=%v", err)
	}
	// 确定性：同参必同 URL（签名可重放）.
	o := QROptions{Secret: "s", Now: qrGoldenNow()}
	a, _ := GenerateSignedURL("a", "p", o)
	b, _ := GenerateSignedURL("a", "p", o)
	if a.SignedURL != b.SignedURL {
		t.Fatalf("同参必须同 URL：%s != %s", a.SignedURL, b.SignedURL)
	}
	// 时效绑定：now 前移 → exp 变 → URL 变（防重放语义面）.
	o2 := QROptions{Secret: "s", Now: qrGoldenNow().Add(time.Hour)}
	c, _ := GenerateSignedURL("a", "p", o2)
	if c.SignedURL == a.SignedURL {
		t.Fatal("不同签发时刻必须产生不同签名 URL")
	}
}

func TestGenerateQRSVGIsExplicitSkeleton(t *testing.T) {
	// QR SVG 位图是 render.GenerateQRSVG 骨架面：如实上抛，不静默降级.
	_, err := GenerateQR(goldenQRAudioID, goldenQRPaperID, QROptions{Secret: goldenQRSigSeed, Now: qrGoldenNow()})
	if err == nil {
		t.Fatal("骨架期 GenerateQR 必须显式失败")
	}
	if !errors.Is(err, render.ErrQRSVGNotImplemented) {
		t.Fatalf("必须透传 render 骨架哨兵：got=%v", err)
	}
}

func TestVerifyQRURLGolden(t *testing.T) {
	if !VerifyQRURL(goldenQRFullURL, goldenQRSigSeed, qrGoldenNow()) {
		t.Fatal("黄金 URL 在有效期内必须验证通过")
	}
	// 边界：恰好到过期秒 → 有效（冻结实现 > 判据同语义）.
	if !VerifyQRURL(goldenQRFullURL, goldenQRSigSeed, time.Unix(goldenQRExpTS, 0).UTC()) {
		t.Fatal("exp 秒上必须仍有效")
	}
	// 过期 1 秒 → 无效.
	if VerifyQRURL(goldenQRFullURL, goldenQRSigSeed, time.Unix(goldenQRExpTS+1, 0).UTC()) {
		t.Fatal("过期 URL 必须无效")
	}
	// 错密钥 → 无效.
	if VerifyQRURL(goldenQRFullURL, "wrong-secret", qrGoldenNow()) {
		t.Fatal("错密钥必须无效")
	}
}

func TestVerifyQRURLTamperAndMalformed(t *testing.T) {
	u, _ := GenerateSignedURL("audio-1", "paper-1", QROptions{Secret: "s", Now: qrGoldenNow()})
	base := u.SignedURL

	// 篡改各签名绑定域 → 无效（签名与 audio/paper/exp 绑定）.
	swappedSig := strings.Replace(base, "sig=", "sig=0", 1)
	if VerifyQRURL(swappedSig, "s", qrGoldenNow()) {
		t.Fatal("篡改 sig 必须无效")
	}
	if VerifyQRURL(strings.Replace(base, "exp=", "exp=9", 1), "s", qrGoldenNow()) {
		t.Fatal("篡改 exp 必须无效")
	}
	// 篡改 audio_id（重放到他音频）→ 无效.
	if VerifyQRURL(strings.Replace(base, "audio-1", "audio-2", 1), "s", qrGoldenNow()) {
		t.Fatal("篡改 audio_id 必须无效")
	}
	// 结构破损 → 无效.
	for _, bad := range []string{
		"", // 空
		"http://x/other.txt?paper=p&exp=1&sig=abc",     // 非 .mp3 path
		"http://x/audio-1.mp3",                         // 缺全部参数
		"http://x/audio-1.mp3?paper=p&exp=abc&sig=abc", // exp 非整数
		"http://x/audio-1.mp3?exp=1&sig=abc",           // 缺 paper
	} {
		if VerifyQRURL(bad, "s", qrGoldenNow()) {
			t.Fatalf("结构破损 URL 必须无效：%q", bad)
		}
	}
}
