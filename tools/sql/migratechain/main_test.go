package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeTree 按 相对路径->内容 建一棵临时目录树，返回根（tools/scan 同款）。
func writeTree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for relPath, body := range files {
		abs := filepath.Join(root, filepath.FromSlash(relPath))
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", abs, err)
		}
		if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", abs, err)
		}
	}
	return root
}

// rev4 格式化四位版本号。
func rev4(i int) string { return fmt.Sprintf("%04d", i) }

// sqlPair 生成一对迁移文件的（路径, 路径）；内容非空即可——空 down 是
// check_pairs.py 的检查面，本守卫不关心内容。
func sqlPair(stem string) (string, string) {
	return "db/migrations/" + stem + ".up.sql", "db/migrations/" + stem + ".down.sql"
}

// pyEntry 生成 alembic 版本文件的（路径, 内容）对，与仓内真实形态同构
// （行首模块级赋值 + Union 注解）。
func pyEntry(rev, down string) (string, string) {
	downLine := "down_revision: Union[str, None] = None"
	if down != "" {
		downLine = `down_revision: Union[str, None] = "` + down + `"`
	}
	body := "from __future__ import annotations\n" +
		"revision: str = \"" + rev + "\"\n" +
		downLine + "\n"
	return "alembic/versions/" + rev + "_m" + rev + ".py", body
}

// greenTree 构造两端一致的 0001..n 链（守卫 A/B 双绿的基线 fixture）。
func greenTree(n int) map[string]string {
	files := map[string]string{}
	for i := 1; i <= n; i++ {
		rev := rev4(i)
		var down string
		if i > 1 {
			down = rev4(i - 1)
		}
		upPath, downPath := sqlPair(rev + "_m" + rev)
		files[upPath] = "-- up\nCREATE TABLE t" + rev + " (id int);\n"
		files[downPath] = "-- down\nDROP TABLE t" + rev + ";\n"
		pyKey, pyBody := pyEntry(rev, down)
		files[pyKey] = pyBody
	}
	return files
}

func findingsOf(t *testing.T, files map[string]string) []finding {
	t.Helper()
	root := writeTree(t, files)
	fs, err := checkContinuity(filepath.Join(root, "db", "migrations"))
	if err != nil {
		t.Fatalf("checkContinuity: %v", err)
	}
	as, err := checkAlembicChain(filepath.Join(root, "alembic", "versions"), filepath.Join(root, "db", "migrations"))
	if err != nil {
		t.Fatalf("checkAlembicChain: %v", err)
	}
	return append(fs, as...)
}

func mustGreen(t *testing.T, files map[string]string) {
	t.Helper()
	if fs := findingsOf(t, files); len(fs) != 0 {
		t.Fatalf("应为绿，得 findings：%+v", fs)
	}
}

func mustRed(t *testing.T, files map[string]string, wantSubstrings ...string) {
	t.Helper()
	fs := findingsOf(t, files)
	if len(fs) == 0 {
		t.Fatalf("应为红（%v），实际零 findings", wantSubstrings)
	}
	lines := make([]string, 0, len(fs))
	for _, f := range fs {
		lines = append(lines, f.String())
	}
	all := strings.Join(lines, "\n")
	for _, want := range wantSubstrings {
		if !strings.Contains(all, want) {
			t.Fatalf("findings 应含 %q，实际：\n%s", want, all)
		}
	}
}

// ── 守卫 A：版本连续性 ─────────────────────────────────────────────────────

func TestContinuity_GreenOnContiguous(t *testing.T) {
	mustGreen(t, greenTree(3))
}

func TestContinuity_RedOnGap(t *testing.T) {
	files := greenTree(3)
	delete(files, "db/migrations/0002_m0002.up.sql")
	mustRed(t, files, "版本号序列非 0001..0002 连续", "0003")
}

func TestContinuity_RedOnNotStartingAtOne(t *testing.T) {
	files := greenTree(2)
	delete(files, "db/migrations/0001_m0001.up.sql")
	mustRed(t, files, "第 1 个版本应为 0001，实际 0002")
}

func TestContinuity_RedOnDuplicatePrefix(t *testing.T) {
	files := greenTree(2)
	files["db/migrations/0002_other.up.sql"] = "-- 同前缀另一文件\nSELECT 1;\n"
	mustRed(t, files, "版本号重复", "0002_m0002.up.sql 与 0002_other.up.sql")
}

func TestContinuity_RedOnUnparsablePrefix(t *testing.T) {
	files := greenTree(2)
	delete(files, "db/migrations/0002_m0002.up.sql")
	files["db/migrations/abc_bad.up.sql"] = "SELECT 1;\n"
	mustRed(t, files, "无法按 NNNN 解析")
}

// ── 守卫 B：alembic 链 ↔ db/migrations 映射 ────────────────────────────────

func TestChain_GreenOnLinearChain(t *testing.T) {
	mustGreen(t, greenTree(3))
}

func TestChain_RedOnMultipleHeads(t *testing.T) {
	// CI 实证的 Multiple head 形态：003 的 down_revision 并错前置。
	files := greenTree(3)
	key, _ := pyEntry("0003", "0002")
	delete(files, key)
	k2, b2 := pyEntry("0003", "0001")
	files[k2] = b2
	mustRed(t, files, `0003 的 down_revision 应为 "0002"`, `得到 "0001"`)
}

func TestChain_RedOnSecondRootNone(t *testing.T) {
	// 第二个链根：003.down = None（Multiple head 的另一形态）。
	files := greenTree(3)
	key, _ := pyEntry("0003", "0002")
	delete(files, key)
	k2, b2 := pyEntry("0003", "")
	files[k2] = b2
	mustRed(t, files, "Multiple head")
}

func TestChain_RedOnDanglingDownRevision(t *testing.T) {
	files := greenTree(3)
	key, _ := pyEntry("0003", "0002")
	delete(files, key)
	k2, b2 := pyEntry("0003", "9999")
	files[k2] = b2
	mustRed(t, files, `down_revision 应为 "0002"`, `"9999"`)
}

func TestChain_RedOnMissingMigrationPair(t *testing.T) {
	files := greenTree(3)
	delete(files, "db/migrations/0003_m0003.down.sql")
	mustRed(t, files, "缺 down 侧 SQL")
}

func TestChain_RedOnExtraMigrationWithoutAlembic(t *testing.T) {
	files := greenTree(3)
	up, down := sqlPair("0004_m0004")
	files[up] = "SELECT 1;\n"
	files[down] = "SELECT 1;\n"
	mustRed(t, files, "在 alembic/versions 无对应 revision")
}

func TestChain_RedOnFilenameRevisionMismatch(t *testing.T) {
	files := greenTree(3)
	key, _ := pyEntry("0003", "0002")
	delete(files, key)
	// 文件名前缀 0003，声明 0004——映射按文件名走，声明漂移即红。
	_, body := pyEntry("0004", "0002")
	files["alembic/versions/0003_m0004.py"] = body
	mustRed(t, files, `文件名前缀 "0003" ≠ revision 声明 "0004"`)
}

func TestChain_RedOnSlugMismatch(t *testing.T) {
	files := greenTree(3)
	delete(files, "db/migrations/0002_m0002.up.sql")
	delete(files, "db/migrations/0002_m0002.down.sql")
	up, down := sqlPair("0002_renamed")
	files[up] = "SELECT 1;\n"
	files[down] = "SELECT 1;\n"
	mustRed(t, files, "SQL stem 与 alembic stem 不一致")
}

func TestChain_ToleratesDocstringMentions(t *testing.T) {
	// 0022 真实形态：文档串里提「down_revision 改为 '0021'…」不构成声明。
	files := greenTree(2)
	key, body := pyEntry("0002", "0001")
	files[key] = body + "\"\"\"\n链序说明：down_revision 改为 '0001'（m0001）以保持线性链。\n\"\"\"\n"
	mustGreen(t, files)
}

func TestChain_ToleratesAlternateAssignmentForms(t *testing.T) {
	// 0027/0028 真实形态：down_revision 无注解直接赋值。
	files := greenTree(2)
	delete(files, "alembic/versions/0002_m0002.py")
	files["alembic/versions/0002_m0002.py"] = "# revision identifiers, used by Alembic.\n" +
		"revision: str = \"0002\"\n" +
		"down_revision = \"0001\"\n"
	mustGreen(t, files)
}

// ── run() 退出码与实仓冒烟 ─────────────────────────────────────────────────

func TestRun_OperationalErrorOnMissingFaces(t *testing.T) {
	root := writeTree(t, map[string]string{
		"alembic/versions/0001_m0001.py": "revision: str = \"0001\"\ndown_revision: Union[str, None] = None\n",
	})
	if _, err := run(root); err == nil {
		t.Fatal("db/migrations 缺失应按操作错误（exit 2 语义）返回 error")
	}
}

func TestRun_ExitOneOnFindings(t *testing.T) {
	files := greenTree(2)
	delete(files, "db/migrations/0002_m0002.up.sql")
	code, err := run(writeTree(t, files))
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if code != 1 {
		t.Fatalf("有 findings 应退出码 1，得 %d", code)
	}
}

func TestRun_ExitZeroOnGreen(t *testing.T) {
	code, err := run(writeTree(t, greenTree(2)))
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if code != 0 {
		t.Fatalf("干净树应退出码 0，得 %d", code)
	}
}

// TestRealRepo_Head_IsGreen 实仓冒烟：当前仓库 HEAD 两守卫必须全绿——
// 若未来提交破坏链/连续性，测试先于 CI 红（tools/scan 同款守门）。
func TestRealRepo_Head_IsGreen(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("定位仓库根: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Skipf("非仓库布局（go.mod 不在 %s），跳过实仓冒烟", root)
	}
	code, err := run(root)
	if err != nil {
		t.Fatalf("run(实仓): %v", err)
	}
	if code != 0 {
		t.Fatalf("实仓 HEAD 应全绿，退出码 %d（见上方 findings 输出）", code)
	}
}
