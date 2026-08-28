package content

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestContentSourcesNeverCommitOrRollback 是 D11 的静态守卫（对齐 core/events、
// core/gate 的同名守卫）：用 go/parser 扫描本包全部非测试源码文件，任何
// .Commit( / .Rollback( 形态的方法调用都属于领域服务自行终结事务——白名单为
// 空，出现即红。
//
// 为什么豁免 *_test.go：测试里的 fakePublishTx 必须实现并以 Commit/Rollback
// 扮演「最外层调用方」，那正是 D11 规定的边界归属；纪律约束的是随产品发布的
// core/content 源码（运行时投影见 publish_test.go 的语句头词断言）.
//
// 守卫自己防「门空转」：一个包文件都扫不到即 Fatal（GO-1 教训——检查面失效
// 比没有检查更危险）.
func TestContentSourcesNeverCommitOrRollback(t *testing.T) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位测试源文件目录")
	}
	dir := filepath.Dir(thisFile)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("读取包目录失败: %v", err)
	}

	scanned := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		path := filepath.Join(dir, name)
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			t.Fatalf("解析 %s 失败: %v", name, perr)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, isCall := n.(*ast.CallExpr)
			if !isCall {
				return true
			}
			sel, isSel := call.Fun.(*ast.SelectorExpr)
			if !isSel {
				return true
			}
			switch sel.Sel.Name {
			case "Commit", "Rollback":
				pos := fset.Position(call.Pos())
				t.Errorf("%s:%d: 领域源码出现 .%s( 调用（D11：本域禁止 Commit/Rollback，白名单为空）",
					filepath.Base(path), pos.Line, sel.Sel.Name)
			}
			return true
		})
		scanned++
	}
	if scanned == 0 {
		t.Fatal("扫描到 0 个包源文件——检查面失效（GO-1）")
	}
	t.Logf("静态 D11 守卫扫描 %d 个产品源文件，零事务控制调用", scanned)
}

// TestContentSourcesNeverGenerateRandomIDs 是 D3 的静态守卫（T-W5-003 验收
// #2 的 CI 化：「grep -rn uuid 无用于生成内容版本 id 的路径」）：扫描本包
// 产品源码，禁止 uuid / 随机 id 生成面进包——内容版本身份只能是内容寻址
// （validators.ContentDigest），随机退化路径一出现即在 gate 红，不待其上线
// 造出不可追溯的账面.
func TestContentSourcesNeverGenerateRandomIDs(t *testing.T) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位测试源文件目录")
	}
	dir := filepath.Dir(thisFile)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("读取包目录失败: %v", err)
	}

	scanned := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		path := filepath.Join(dir, name)
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if perr != nil {
			t.Fatalf("解析 %s 失败: %v", name, perr)
		}
		for _, imp := range file.Imports {
			p := strings.Trim(imp.Path.Value, `"`)
			if strings.Contains(p, "uuid") || strings.Contains(p, "crypto/rand") {
				pos := fset.Position(imp.Pos())
				t.Errorf("%s:%d: import %q —— 本域禁止随机/UUID 身份面（D3：内容身份只能是内容寻址）",
					filepath.Base(path), pos.Line, p)
			}
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, isCall := n.(*ast.CallExpr)
			if !isCall {
				return true
			}
			sel, isSel := call.Fun.(*ast.SelectorExpr)
			if isSel && (sel.Sel.Name == "New" || sel.Sel.Name == "NewString") {
				if id, isId := sel.X.(*ast.Ident); isId && strings.Contains(strings.ToLower(id.Name), "uuid") {
					pos := fset.Position(call.Pos())
					t.Errorf("%s:%d: %s.%s( 调用 —— UUID 退化路径已终结，不得复辟（D3 fail-loud）",
						filepath.Base(path), pos.Line, id.Name, sel.Sel.Name)
				}
			}
			return true
		})
		scanned++
	}
	if scanned == 0 {
		t.Fatal("扫描到 0 个包源文件——检查面失效（GO-1）")
	}
	t.Logf("静态 D3 守卫扫描 %d 个产品源文件，零随机身份面", scanned)
}
