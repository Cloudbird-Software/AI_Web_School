package render

import (
	"errors"
	"testing"
)

// 地面真值：以下期望值全部取自冻结 Python 实现（src/core/render/trace_codes.py）
// 的真实运行输出，构成跨实现比对锚点（Luhn 变体 + SHA1→base32 链路逐字符一致）。
func TestTraceCodesGroundTruth(t *testing.T) {
	tests := []struct {
		name string
		got  string
		want string
	}{
		{"卷码_ULID26加校验位",
			mustPaperCode(t, "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
			"01ARZ3NDEKTSV4RRFFQ69G5FAV0"},
		{"QR载荷_spec加校验位",
			mustQRPayload(t, "spec-001"),
			"spec-0014"},
		{"题短码_样例1",
			mustShortCode(t, "01JPAPERITEM0001"),
			"BB4M9T5"},
		{"题短码_样例2_base32边界",
			mustShortCode(t, "0123456789ABCDEFGHJKMNPQRSTVW"),
			"XJPEQV1"},
		{"题短码_样例3_短id",
			mustShortCode(t, "item-x"),
			"3F86DK7"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if tt.got != tt.want {
				t.Fatalf("与冻结实现不一致:\n got: %s\nwant: %s", tt.got, tt.want)
			}
		})
	}
}

func mustPaperCode(t *testing.T, ulid string) string {
	t.Helper()
	code, err := GeneratePaperCode(ulid)
	if err != nil {
		t.Fatalf("GeneratePaperCode: %v", err)
	}
	return code
}

func mustQRPayload(t *testing.T, spec string) string {
	t.Helper()
	payload, err := GenerateQRPayload(spec)
	if err != nil {
		t.Fatalf("GenerateQRPayload: %v", err)
	}
	return payload
}

func mustShortCode(t *testing.T, id string) string {
	t.Helper()
	code, err := GenerateItemShortCode(id)
	if err != nil {
		t.Fatalf("GenerateItemShortCode: %v", err)
	}
	return code
}

func TestTraceCodesVerify(t *testing.T) {
	code := mustPaperCode(t, "01ARZ3NDEKTSV4RRFFQ69G5FAV")
	if !VerifyPaperCode(code) {
		t.Fatal("合法卷码应通过")
	}
	// 中间字符篡改（保持长度/字符集合法）
	corrupt := "01ARZ3NDEKTSV4RRFFQ69G5FAW0"
	if VerifyPaperCode(corrupt) {
		t.Fatal("篡改卷码不应通过")
	}
	if VerifyPaperCode("01ARZ3NDEKTSV4RRFFQ69G5FA") { // 26 位
		t.Fatal("长度不符不应通过")
	}
	if VerifyPaperCode("01ARZ3NDEKTSV4RRFFQ69G5FAI0") { // I 不在 Crockford 字符集
		t.Fatal("非法字符不应通过")
	}
	if VerifyPaperCode("01arz3ndektsv4rrffq69g5fav0") { // verify 不做大写归一
		t.Fatal("小写不应通过（verify 口径与生成侧 upper 不同）")
	}

	payload := mustQRPayload(t, "spec-001")
	if !VerifyQRPayload(payload) {
		t.Fatal("合法 QR 载荷应通过")
	}
	if VerifyQRPayload("x") {
		t.Fatal("单字符不应通过")
	}
	spec, ok := ExtractPaperSpecID(payload)
	if !ok || spec != "spec-001" {
		t.Fatalf("提取错: %q %v", spec, ok)
	}
	if _, ok := ExtractPaperSpecID("spec-001X"); ok {
		t.Fatal("校验失败应返回 false")
	}

	short := mustShortCode(t, "01JPAPERITEM0001")
	if !VerifyItemShortCode(short) {
		t.Fatal("合法短码应通过")
	}
	if VerifyItemShortCode("BB4M9T4") { // 校验位错
		t.Fatal("校验位不符不应通过")
	}
	if VerifyItemShortCode("ZZZZZZZ") { // 末位非数字
		t.Fatal("末位非数字不应通过")
	}
	if VerifyItemShortCode("BB4M9T") { // 6 位
		t.Fatal("长度不符不应通过")
	}
}

func TestTraceCodesErrors(t *testing.T) {
	if _, err := GeneratePaperCode("short"); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("ULID 长度错应锚定 ErrInvalidCode: %v", err)
	}
	if _, err := GeneratePaperCode("01ARZ3NDEKTSV4RRFFQ69G5FAI"); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("ULID 非法字符应锚定 ErrInvalidCode: %v", err)
	}
	if _, err := GenerateQRPayload(""); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("空 spec 应锚定 ErrInvalidCode: %v", err)
	}
	if _, err := GenerateItemShortCode(""); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("空 paper_item_id 应锚定 ErrInvalidCode: %v", err)
	}
	// QR SVG 为显式 IO 骨架：必须显式失败，不得静默降级
	if _, err := GenerateQRSVG("spec-0014", 4, 1); !errors.Is(err, ErrQRSVGNotImplemented) {
		t.Fatalf("QR SVG 骨架应锚定 ErrQRSVGNotImplemented: %v", err)
	}
}

func TestBuildTraceChain(t *testing.T) {
	paperItem := map[string]any{
		"item_short_code": "BB4M9T5", "paper_item_id": "pi-1",
		"paper_id": "p-1", "item_number": 3,
	}
	itemVersion := map[string]any{
		"item_version_id": "iv-1", "item_id": "item-1",
		"gate_certificate_id": "cert-1",
		"lineage":             map[string]any{"source": "gen"},
	}
	cert := map[string]any{
		"issued_by": "agent", "issued_at": "2026-08-01T00:00:00Z", "policy_version": "1.0.0",
	}
	chain, err := BuildTraceChain(paperItem, itemVersion, cert)
	if err != nil {
		t.Fatalf("BuildTraceChain: %v", err)
	}
	if chain.ItemShortCode != "BB4M9T5" || chain.PaperItemID != "pi-1" || chain.PaperID != "p-1" ||
		chain.ItemNumber != 3 || chain.ItemVersionID != "iv-1" || chain.ItemID != "item-1" ||
		chain.GateCertificateID != "cert-1" || chain.IssuedBy != "agent" ||
		chain.IssuedAt != "2026-08-01T00:00:00Z" || chain.PolicyVersion != "1.0.0" {
		t.Fatalf("回溯链字段错: %+v", chain)
	}
	if chain.Lineage["source"] != "gen" {
		t.Fatalf("lineage 应透传: %+v", chain.Lineage)
	}

	// 无证书行：签发面为空
	chainNoCert, err := BuildTraceChain(paperItem, itemVersion, nil)
	if err != nil {
		t.Fatalf("BuildChain(nil cert): %v", err)
	}
	if chainNoCert.IssuedBy != "" || chainNoCert.IssuedAt != "" || chainNoCert.PolicyVersion != "" {
		t.Fatalf("无证书行签发面应为空: %+v", chainNoCert)
	}
	if chainNoCert.GateCertificateID != "cert-1" {
		t.Fatalf("证书 id 来自 item_version 行: %+v", chainNoCert)
	}

	// 缺必要键：fail fast
	if _, err := BuildTraceChain(map[string]any{}, itemVersion, nil); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("缺 paper_item 字段应锚定 ErrInvalidCode: %v", err)
	}
	if _, err := BuildTraceChain(paperItem, map[string]any{}, nil); !errors.Is(err, ErrInvalidCode) {
		t.Fatalf("缺 item_version 字段应锚定 ErrInvalidCode: %v", err)
	}
}
