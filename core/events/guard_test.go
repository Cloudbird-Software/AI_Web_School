package events

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

// TestDomainSourcesNeverCommitOrRollback 是 D11 的静态守卫（T-W5-017 验收 #2）：
// 用 go/parser 扫描本包全部非测试源码文件，任何 .Commit( / .Rollback( 形态的
// 方法调用都属于领域服务自行终结事务——白名单为空，出现即红。
//
// 为什么豁免 *_test.go：单测里的 fakeTx 必须实现并以 Commit/Rollback 扮演
// 「最外层调用方」，那正是 D11 规定的边界归属（所有者终结事务），不属于领域
// 服务自身行为；纪律约束的是随产品发布的 core/events 源码（doc.go 为包级红线
// 声明，运行时投影见 TestWriterNeverIssuesTransactionControlStatements）。
//
// 守卫自己防「门空转」：一个包文件都扫不到即 Fatal（GO-1 教训——检查面失效
// 比 没有检查 更危险）.
func TestDomainSourcesNeverCommitOrRollback(t *testing.T) {
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
		t.Fatal("未扫描到任何包源码文件——守卫失效")
	}
}
