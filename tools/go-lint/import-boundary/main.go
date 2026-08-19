// Package main 实现 GO-3/X6 的 import 边界 lint（T-W5-031 验收 #3）：
// core/ 目录下的 Go 包禁止 import 本仓 packs/ 目录的任何包
// （宪法 A5/X6：核心域学科零特判，架构上由依赖方向保证）。
//
// 用法：go run ./tools/go-lint/import-boundary
// 退出码：0 = 边界干净；1 = 存在违规（gate 拦截）。
package main

import (
	"fmt"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

const (
	coreDir = "core"
	packPkg = "github.com/Cloudbird-Software/AI_Web_School/packs"
)

func main() {
	violations, err := scan(".")
	if err != nil {
		fmt.Fprintf(os.Stderr, "import-boundary: 扫描失败: %v\n", err)
		os.Exit(2)
	}
	if len(violations) > 0 {
		for _, v := range violations {
			fmt.Fprintf(os.Stderr, "❌ core 引用了学科/学段包: %s: %s\n", v.file, v.importPath)
		}
		os.Exit(1)
	}
	fmt.Println("✅ core/ 未引用任何 packs/ 包（X6/GO-3）")
}

type violation struct {
	file       string
	importPath string
}

// scan 遍历 coreDir 下所有 .go 文件（跳过 vendor/ 与测试目录），解析 import。
func scan(root string) ([]violation, error) {
	var out []violation
	base := filepath.Join(root, coreDir)
	fset := token.NewFileSet()
	err := filepath.WalkDir(base, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			// 测试 fixture 目录不参与（其内部包不进构建）
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
			return fmt.Errorf("%s: %w", path, perr)
		}
		for _, imp := range f.Imports {
			if strings.Trim(imp.Path.Value, `"`) == packPkg ||
				strings.HasPrefix(strings.Trim(imp.Path.Value, `"`), packPkg+"/") {
				out = append(out, violation{file: path, importPath: imp.Path.Value})
			}
		}
		return nil
	})
	return out, err
}
