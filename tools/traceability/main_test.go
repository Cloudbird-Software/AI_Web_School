package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// writeTree 在临时目录落最小仓库骨架（constitution + 矩阵 + 实证文件）。
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
	if err := os.WriteFile(filepath.Join(root, matrixRelPath), []byte(matrix), 0o644); err != nil {
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

const matrixHeader = "## 强制实证矩阵\n| 条款 | 层级 | 实证路径 | 状态 |\n|---|---|---|---|\n"

const greenMatrix = matrixHeader +
	"| V1 | 测试 | a_test.go（愿景实证说明） | 已强制 |\n" +
	"| D1 | DB | db/migrations/0001_x.up.sql; tools/scan/norank（说明一）; tools/scan/frozencontract | 已强制 |\n" +
	"| X1 | 流程 | —（说明：流程条款） | 仅纪律 |\n" +
	"| X1 | CI | org:.github/workflows/gate.yml（外部引用示例） | 已强制（org 门） |\n"

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
		matrixHeader+"| V1 | 测试 | a_test.go | 已强制 |\n",
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
		matrixHeader+"| V1 | 测试 | — | 已强制 |\n| D1 | DB | a_test.go | 已强制 |\n| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("已强制无实证应红")
	}
}

// TestEnforcedWithDashNoteFails 红队 Major-1 变异 A：已强制行写
// `—（无实证说明）` 形态必须红（解析器 fail-open 修复的回归钉）。
func TestEnforcedWithDashNoteFails(t *testing.T) {
	root := writeTree(t, minimalConstitution,
		matrixHeader+"| V1 | 测试 | —（无实证，承接 W6） | 已强制 |\n| D1 | DB | a_test.go | 已强制 |\n| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("已强制 + —（注）形态应红")
	}
}

// TestFullWidthSeparators 红队 Major-2：全角；分隔与 + 连接列表的分段路径
// 全部参与存在性校验（缺任一段即红）。
func TestFullWidthSeparators(t *testing.T) {
	root := writeTree(t, minimalConstitution,
		matrixHeader+
			"| V1 | 测试 | a_test.go + missing_test.go | 已强制 |\n"+
			"| D1 | DB | db/0001.up.sql；a_test.go | 已强制 |\n"+
			"| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go", "db/0001.up.sql"})
	err := run(root)
	if err == nil {
		t.Fatal("多路径行存在缺失文件应红")
	}
	joined := ""
	for _, v := range err.violations {
		joined += v.reason
	}
	if !strings.Contains(joined, "missing_test.go") {
		t.Fatalf("+ 连接的缺失段未被校验: %s", joined)
	}
}

func TestMissingEvidenceFile(t *testing.T) {
	// 引用的实证文件不在盘上——红
	root := writeTree(t, minimalConstitution,
		matrixHeader+"| V1 | 测试 | nope_test.go | 已强制 |\n| D1 | DB | a_test.go | 已强制 |\n| X1 | 流程 | — | 仅纪律 |\n",
		[]string{"a_test.go"})
	err := run(root)
	if err == nil {
		t.Fatal("路径不存在应红")
	}
}

// TestParensAndOrgPrefixTolerated 全角括号注释剥离 + org: 前缀跳过 +
// 条目只剩注释跳过（容错形态不误红）。
func TestParensAndOrgPrefixTolerated(t *testing.T) {
	root := writeTree(t, minimalConstitution,
		matrixHeader+
			"| V1 | 测试 | a_test.go（愿景实证说明）; org:.github/workflows/g.yml | 已强制 |\n"+
			"| D1 | DB | db/migrations/0001_x.up.sql（说明）; （纯注释条目） | 已强制 |\n"+
			"| X1 | 流程 | —（明示无实证） | 仅纪律 |\n",
		[]string{"a_test.go", "db/migrations/0001_x.up.sql"})
	if err := run(root); err != nil {
		t.Fatalf("容错形态不应红: %v", err.violations)
	}
}

// TestRealRepo_MatrixIsGreen 实仓冒烟（红队 Major-3：README 曾声称「随 go
// test 进 gate」但无实仓调用——本测试补上，矩阵/实证文件破坏时先于 CI 红）。
func TestRealRepo_MatrixIsGreen(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Dir(filepath.Dir(filepath.Dir(file))) // tools/traceability → 仓库根
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Skipf("非仓库检出（%s 无 go.mod），跳过实仓冒烟", root)
	}
	if err := run(root); err != nil {
		for _, v := range err.violations {
			t.Errorf("[%s] %s", v.clause, v.reason)
		}
		t.Fatal("实仓强制实证矩阵违规")
	}
}
