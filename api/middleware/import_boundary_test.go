package middleware

// X6 静态实证（T-W5-008 验收 #5）：本包（及 api 层全部中间件）不得 import
// 任何学科/学段包（packs/**）。GO-3 的 import-boundary lint 只扫 core/，
// 本测试把同一红线覆盖到中间件层——中间件是协议层，学科差异永远到不了这里。

import (
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestMiddlewareImportsNoSubjectPacks(t *testing.T) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位源文件目录")
	}
	dir := filepath.Dir(thisFile)
	fset := token.NewFileSet()
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "testdata" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		f, perr := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if perr != nil {
			return perr
		}
		for _, imp := range f.Imports {
			p := strings.Trim(imp.Path.Value, `"`)
			if p == "github.com/Cloudbird-Software/AI_Web_School/packs" ||
				strings.HasPrefix(p, "github.com/Cloudbird-Software/AI_Web_School/packs/") {
				t.Errorf("%s: 中间件 import 学科包 %q（X6 红线）", filepath.Base(path), p)
			}
		}
		return nil
	})
	if err != nil {
		if os.IsNotExist(err) {
			t.Fatalf("源目录不存在: %v", err)
		}
		t.Fatalf("扫描失败: %v", err)
	}
}
