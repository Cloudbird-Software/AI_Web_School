package main

import (
	"os"
	"path/filepath"
	"testing"
)

// T-W5-031 验收 #3 的可执行自证：构造临时仓库，注入违规 import 必须被抓到。
// 注意：这不是"测试与实现互证"——被测物是磁盘上的真实 Go 源文件形态，
// 与 lint 在 CI 中扫描真实仓库的行为完全一致。
func TestScanDetectsViolation(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, coreDir, "demo")
	if err := os.MkdirAll(core, 0o755); err != nil {
		t.Fatal(err)
	}
	bad := `package demo

import _ "github.com/Cloudbird-Software/AI_Web_School/packs/subject-math"
`
	if err := os.WriteFile(filepath.Join(core, "demo.go"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	vs, err := scan(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(vs) != 1 {
		t.Fatalf("必须检出 1 处违规，得到 %d", len(vs))
	}
}

func TestScanCleanTree(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, coreDir, "demo")
	if err := os.MkdirAll(core, 0o755); err != nil {
		t.Fatal(err)
	}
	good := `package demo

import "strings"
`
	if err := os.WriteFile(filepath.Join(core, "demo.go"), []byte(good), 0o644); err != nil {
		t.Fatal(err)
	}
	vs, err := scan(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(vs) != 0 {
		t.Fatalf("干净代码树不应有违规，得到 %v", vs)
	}
}
