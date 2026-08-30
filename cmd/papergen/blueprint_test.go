package main

// blueprint 解析单测（审计 #148 交付 5）：合法蓝图逐字段落地；文件缺失/
// 非法 JSON 显式失败。字段级必填校验不在本层断言（单一真相源在编排层
// Orchestrate 的 ErrInvalidBlueprint，core/assembly 已覆盖）.

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeTempBlueprint(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "bp.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("写临时蓝图: %v", err)
	}
	return path
}

func TestLoadBlueprint_Fields(t *testing.T) {
	path := writeTempBlueprint(t, `{
		"profile_id": "bp-cli",
		"profile_version": "2",
		"purpose": "practice",
		"gradeband": "M",
		"pack_id": "pack-1",
		"kp_codes": ["KP1", "KP2"],
		"seed": 7,
		"snapshot_ref": "snap-cli",
		"base": {"item_count_range": [3, 3]}
	}`)
	bp, err := loadBlueprint(path)
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if bp.ProfileID != "bp-cli" || bp.ProfileVersion != "2" || bp.Purpose != "practice" ||
		bp.Gradeband != "M" || bp.PackID != "pack-1" || bp.Seed != 7 || bp.SnapshotRef != "snap-cli" {
		t.Fatalf("字段面漂移: %+v", bp)
	}
	if len(bp.KpCodes) != 2 || bp.KpCodes[0] != "KP1" || bp.KpCodes[1] != "KP2" {
		t.Fatalf("kp_codes 漂移: %v", bp.KpCodes)
	}
	if bp.Base == nil || bp.Base["item_count_range"] == nil {
		t.Fatalf("overlay 值树应原样保留（CompileInput 直通面）: %v", bp.Base)
	}
}

func TestLoadBlueprint_MissingFileFails(t *testing.T) {
	if _, err := loadBlueprint(filepath.Join(t.TempDir(), "nope.json")); err == nil || !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("文件缺失应显式失败且可 errors.Is 判定: %v", err)
	}
}

func TestLoadBlueprint_BadJSONFails(t *testing.T) {
	bad := writeTempBlueprint(t, `{"profile_id":`)
	_, err := loadBlueprint(bad)
	if err == nil {
		t.Fatal("坏 JSON 必须显式失败")
	}
	if !strings.Contains(err.Error(), "非合法 JSON") {
		t.Fatalf("错误未归入解析面（排障语义）: %v", err)
	}
}

func TestLoadBlueprint_EmptyObjectYieldsZeroBlueprint(t *testing.T) {
	// 空对象解析得零值蓝图——解析层放行、必填校验归编排层（ErrInvalidBlueprint
	// 的「蓝图为空」分支），本测试锚定这一分层边界不被悄悄上移；而空文件在
	// JSON 解析层即显式失败（与零值蓝图可区分）.
	bp, err := loadBlueprint(writeTempBlueprint(t, "{}"))
	if err != nil {
		t.Fatalf("空对象应解析为零值蓝图: %v", err)
	}
	if bp.PackID != "" || len(bp.KpCodes) != 0 {
		t.Fatalf("零值蓝图面漂移: %+v", bp)
	}
	if _, err := loadBlueprint(writeTempBlueprint(t, "")); err == nil {
		t.Fatal("空文件应在解析层显式失败")
	}
}
