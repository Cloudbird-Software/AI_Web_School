package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeTree 按 相对路径->内容 建一棵临时目录树，返回根.
func writeTree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range files {
		abs := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", abs, err)
		}
		if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", abs, err)
		}
	}
	return root
}

const manifestHeader = "# 冻结契约清单（每行一个路径；contract-watch 强制只增不改）\n"

func baseTree(entries []string) map[string]string {
	list := manifestHeader
	for _, e := range entries {
		list += e + "\n"
	}
	files := map[string]string{
		"specs/contracts/FROZEN.txt":      list,
		"tests/contract/place_holder.txt": "",
	}
	for _, e := range entries {
		files[e] = "contract body of " + e + "\n"
	}
	return files
}

func refIn(rel, entry string) map[string]string {
	return map[string]string{rel: `"""契约测试

CONTRACT = Path("` + entry + `")
"""
`}
}

func TestScanAll_GreenWhenEveryEntryCovered(t *testing.T) {
	files := baseTree([]string{
		"specs/contracts/api/openapi-v1.yaml",
		"specs/contracts/db/item-model.md",
	})
	for rel, body := range refIn("tests/contract/api/test_openapi_contract.py",
		"specs/contracts/api/openapi-v1.yaml") {
		files[rel] = body
	}
	for rel, body := range refIn("tests/contract/db/test_item_model_contract.py",
		"specs/contracts/db/item-model.md") {
		files[rel] = body
	}
	res, err := scanAll(writeTree(t, files))
	if err != nil {
		t.Fatalf("scanAll: %v", err)
	}
	if len(res.findings) != 0 {
		t.Fatalf("全覆盖清单不应有 findings：%+v", res.findings)
	}
	if len(res.entries) != 2 || len(res.refs["specs/contracts/db/item-model.md"]) != 1 {
		t.Fatalf("refs 证据不完整：%+v", res.refs)
	}
}

func TestScanAll_RedOnUncoveredEntry(t *testing.T) {
	files := baseTree([]string{
		"specs/contracts/api/openapi-v1.yaml",
		"specs/contracts/events/response_event.md", // 有文件、无测试引用 = 盲区
	})
	for rel, body := range refIn("tests/contract/api/test_openapi_contract.py",
		"specs/contracts/api/openapi-v1.yaml") {
		files[rel] = body
	}
	res, err := scanAll(writeTree(t, files))
	if err != nil {
		t.Fatalf("scanAll: %v", err)
	}
	if len(res.findings) != 1 || res.findings[0].kind != "uncovered" ||
		res.findings[0].entry != "specs/contracts/events/response_event.md" {
		t.Fatalf("应 fail-loud 指出未覆盖条目，得 %+v", res.findings)
	}
}

func TestScanAll_RedOnMissingFile(t *testing.T) {
	files := map[string]string{
		"specs/contracts/FROZEN.txt": manifestHeader + "specs/contracts/db/paper-model.md\n",
		"tests/contract/db/test_paper.py": `CONTRACT = Path("specs/contracts/db/paper-model.md")
`,
	}
	res, err := scanAll(writeTree(t, files))
	if err != nil {
		t.Fatalf("scanAll: %v", err)
	}
	if len(res.findings) == 0 || res.findings[0].kind != "missing_file" {
		t.Fatalf("缺盘上文件必须红，得 %+v", res.findings)
	}
}

func TestScanAll_RedOnDuplicateAndEmptyManifest(t *testing.T) {
	dupFiles := baseTree([]string{"specs/contracts/db/item-model.md"})
	dupFiles["specs/contracts/FROZEN.txt"] =
		manifestHeader + "specs/contracts/db/item-model.md\nspecs/contracts/db/item-model.md\n"
	dupFiles["tests/contract/x.py"] = "specs/contracts/db/item-model.md\n"
	res, err := scanAll(writeTree(t, dupFiles))
	if err != nil {
		t.Fatalf("scanAll: %v", err)
	}
	hasDup := false
	for _, f := range res.findings {
		if f.kind == "duplicate" {
			hasDup = true
		}
	}
	if !hasDup {
		t.Fatalf("重复条目必须红，得 %+v", res.findings)
	}

	emptyFiles := map[string]string{
		"specs/contracts/FROZEN.txt":      manifestHeader + "\n",
		"tests/contract/place_holder.txt": "",
	}
	res2, err := scanAll(writeTree(t, emptyFiles))
	if err != nil {
		t.Fatalf("scanAll: %v", err)
	}
	if len(res2.findings) == 0 || res2.findings[0].kind != "empty_manifest" {
		t.Fatalf("空清单必须 fail-loud，得 %+v", res2.findings)
	}
}

func TestParseManifest_SkipsCommentsAndBlankLines(t *testing.T) {
	data := []byte("# 注释\r\n\r\nspecs/contracts/a.md\n   \n# another\nspecs/contracts/b.md\n")
	entries, dupes := parseManifest(data)
	if len(entries) != 2 || entries[0] != "specs/contracts/a.md" ||
		entries[1] != "specs/contracts/b.md" {
		t.Fatalf("entries 解析错误：%v", entries)
	}
	if len(dupes) != 0 {
		t.Fatalf("不应有重复：%v", dupes)
	}
}

func TestRun_ExitCodes(t *testing.T) {
	var out, errBuf bytes.Buffer

	good := writeTree(t, func() map[string]string {
		files := baseTree([]string{"specs/contracts/db/item-model.md"})
		for rel, body := range refIn("tests/contract/db/test_item_model_contract.py",
			"specs/contracts/db/item-model.md") {
			files[rel] = body
		}
		return files
	}())
	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, good); code != 0 {
		t.Fatalf("好树应退出 0，得 %d stderr=%s", code, errBuf.String())
	}
	if !strings.Contains(out.String(), "✅") || !strings.Contains(out.String(), "test_item_model_contract.py") {
		t.Errorf("成功输出应含证据行：%s", out.String())
	}

	bad := writeTree(t, baseTree([]string{"specs/contracts/events/response_event.md"}))
	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, bad); code != 1 {
		t.Fatalf("盲区树应退出 1，得 %d stdout=%s", code, out.String())
	}
	if !strings.Contains(errBuf.String(), "[uncovered]") ||
		!strings.Contains(errBuf.String(), "response_event.md") {
		t.Errorf("失败输出必须逐条 fail-loud：%s", errBuf.String())
	}

	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, filepath.Join(t.TempDir(), "nowhere")); code != 2 {
		t.Fatalf("非法 -root 应退出 2，得 %d", code)
	}
}

func TestRealRepo_AllFrozenEntriesCovered(t *testing.T) {
	root, err := resolveRoot("") // 测试工作目录在包目录内，逐级上溯即仓库根
	if err != nil {
		t.Skipf("不在仓库树内运行（%v），跳过实仓冒烟", err)
	}
	res, serr := scanAll(root)
	if serr != nil {
		t.Fatalf("实仓扫描失败: %v", serr)
	}
	if len(res.findings) > 0 {
		t.Errorf("FROZEN.txt 存在守卫盲区（新缺口则此测试先红）：\n%v", res.findings)
	}
	// 冻结实现的语义基准：openapi-v1.yaml 必须在受管清单里且有契约测试引用
	var hasOpenAPI bool
	for _, e := range res.entries {
		if strings.HasSuffix(e, "openapi-v1.yaml") && len(res.refs[e]) > 0 {
			hasOpenAPI = true
		}
	}
	if !hasOpenAPI {
		t.Error("openapi-v1.yaml 应被 FROZEN.txt 管辖且被 tests/contract 引用")
	}
}
