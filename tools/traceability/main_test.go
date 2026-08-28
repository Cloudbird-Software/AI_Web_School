// Package main 的双向自测：fixture 树构造红绿用例，锁定解析器的
// 三条硬校验与容错行为（全角括号注释剥离、org: 外部前缀、— 无实证）。
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeTree 在临时目录落最小仓库骨架（constitution + TRACEABILITY + 实证文件）。
func writeTree(t *testing.T, constitution, matrix string, files []string) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "specs", "contracts"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module x\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "specs", "constitution.md"), []byte(constitution), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "specs", "traceability-matrix.md"), []byte(matrix), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		fp := filepath.Join(root, f)
		if err := os.MkdirAll(filepath.Dir(fp), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(fp, []byte("// fixture\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

const minimalConstitution = "- **V1 愿景一**：x\n- **D1 铁律一**：y\n- **X1 禁令一**：z\n"

const greenMatrix = `# 契约对照

## 强制实证矩阵
| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| V1 | 测试 | a_test.go（愿景实证说明） | 已强制 |
| D1 | DB | db/migrations/0001_x.up.sql; tools/scan/norank（说明一）; tools/scan/frozencontract | 已强制 |
| X1 | 流程 | —（说明：流程条款） | 仅纪律 |
| X1 | CI | org:.github/workflows/gate.yml（外部引用示例） | 已强制（org 门） |
`

func TestGreenFixture(t *testing.T) {
	root := writeTree(t, minimalConstitution, greenMatrix, []string{
		"a_test.go",
		"db/migrations/0001_x.up.sql",
		"tools/scan/norank/main.go",
		"tools/scan/frozencontract/main.go",
	})
	if err := run(root); err != nil {
		t.Fatalf("绿灯 fixture 不应违规: %v", err.violations)
	}
}

func TestMissingClause(t *testing.T) {
	// 宪法有 X1，矩阵缺——覆盖校验红
	root := writeTree(t, minimalConstitution,
		"## 强制实证矩阵\n| 条款 | 层级 | 实证路径 | 状态 |\n|---|---|---|---|\n| V1 | 测试 | a_test.go | 已强制 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("缺条款应红")
	}
	found := false
	for _, v := range err.violations {
		if v.clause == "X1" && strings.Contains(v.reason, "缺条款") {
			found = true
		}
	}
	if !found {
		t.Fatalf("应报 X1 缺条款，得到: %v", err.violations)
	}
}

func TestEnforcedWithoutPath(t *testing.T) {
	// 已强制但路径空——P9 最高优先级红
	root := writeTree(t, minimalConstitution,
		"## 强制实证矩阵\n| 条款 | 层级 | 实证路径 | 状态 |\n|---|---|---|---|\n| V1 | 测试 | — | 已强制 |\n| D1 | DB | a_test.go | 已强制 |\n| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("已强制无实证应红")
	}
}

func TestMissingEvidenceFile(t *testing.T) {
	// 引用的实证文件不在盘上——红
	root := writeTree(t, minimalConstitution,
		"## 强制实证矩阵\n| 条款 | 层级 | 实证路径 | 状态 |\n|---|---|---|---|\n| V1 | 测试 | nope_test.go | 已强制 |\n| D1 | DB | a_test.go | 已强制 |\n| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("路径不存在应红")
	}
}

func TestParensAndOrgPrefixTolerated(t *testing.T) {
	// 全角括号注释剥离 + org: 前缀跳过 + 条目只剩注释跳过
	root := writeTree(t, minimalConstitution,
		"## 强制实证矩阵\n| 条款 | 层级 | 实证路径 | 状态 |\n|---|---|---|---|\n| V1 | 测试 | a_test.go（愿景实证说明）; org:.github/workflows/g.yml | 已强制 |\n| D1 | DB | db/migrations/0001_x.up.sql（说明）; （纯注释条目） | 已强制 |\n| X1 | 流程 | —（明示无实证） | 仅纪律 |\n",
		[]string{"a_test.go", "db/migrations/0001_x.up.sql"})
	if err := run(root); err != nil {
		t.Fatalf("容错形态不应红: %v", err.violations)
	}
}
