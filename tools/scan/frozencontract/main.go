// Package main 实现 T-W5-023 守卫 A：冻结契约全量遍历守卫.
//
// 冻结实现 scripts/ci/check_openapi_diff.py 只硬编码守护单个文件
// （openapi-v1.yaml），FROZEN.txt 里其余冻结契约不在任何机器守卫的面上——
// 这就是任务卡指出的盲区 ①。本工具把判定面锚定到清单本身：
//   - 读 specs/contracts/FROZEN.txt 全部条目（只增不改的清单，注释行跳过）
//   - 每个条目两条断言：文件存在；被 tests/contract 引用（覆盖事实源 =
//     tests/contract 下任一文本文件中出现该条目的完整相对路径——契约测试
//     均以 Path("specs/contracts/...") 形式引用被测契约，属文件名映射事实源）
//   - 无法机器判定的条目 fail-loud 列出（exit 1 + 逐条明细），绝不静默放行
//
// 本工具不替代 diff 守卫（scripts/ci/check_openapi_diff.py 管修改拦截），
// 它管「冻结面无遗漏、每个冻结契约都有活的契约测试」。两者互补。
//
// 用法：
//
//	go run ./tools/scan/frozencontract [-root REPO_ROOT]
//
// 退出码：0 = 清单全量被引用且文件齐；1 = 有缺口（fail-loud 明细）；
// 2 = 操作错误（找不到仓库根/FROZEN.txt/tests/contract）。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const (
	manifestRel = "specs/contracts/FROZEN.txt" // 冻结契约清单（只增不改）
	testsRel    = "tests/contract"             // 覆盖事实源目录（Python 冻结契约测试）
	maxFileBob  = 1 << 20                      // 覆盖扫描单文件读取上限 1MB（防巨型 fixture 拖慢）
)

// goRefDirs 是 Go 侧契约测试的引用面补充（T-W5-028 起：v1.1 契约测试在
// api 包内）——冻结条目被其中任一 *_test.go 以完整路径引用即算覆盖。
var goRefDirs = []string{"api", "tools"}

// finding 一处清单遍历断言失败.
type finding struct {
	entry  string // 涉及的 FROZEN.txt 条目
	kind   string // missing_file / uncovered / duplicate / empty_manifest
	detail string
}

// scanResult 全量遍历的结果：findings 非空即有缺口；refs 为成功输出证据.
type scanResult struct {
	findings []finding
	entries  []string            // 清单条目（按文件出现序）
	refs     map[string][]string // 条目 -> 引用它的 tests/contract 相对路径
}

// resolveRoot 定位仓库根：显式 -root 必须含 FROZEN.txt；否则从当前目录上溯.
func resolveRoot(flagVal string) (string, error) {
	start := flagVal
	if start == "" {
		var err error
		start, err = os.Getwd()
		if err != nil {
			return "", fmt.Errorf("取当前目录失败: %w", err)
		}
	}
	dir := start
	for {
		if fi, err := os.Stat(filepath.Join(dir, manifestRel)); err == nil && !fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			if flagVal != "" {
				return "", fmt.Errorf("-root %q 下未找到 %s，不是仓库根", flagVal, manifestRel)
			}
			return "", fmt.Errorf("从 %s 上溯未找到 %s——请在仓库内运行或用 -root 指定", start, manifestRel)
		}
		dir = parent
	}
}

// parseManifest 解析 FROZEN.txt：兼容两种格式——
//  1. JSON 数组（T-W5-028 起：{"frozen_contracts": [...]}）——contract-check
//     引擎按扩展名+内容双判定把 specs/contracts/** 下被触碰的非 JSON 文件
//     一律按 JSON 解析（纯文本清单必红，main 实证），故清单本体升级为 JSON；
//  2. v1 纯文本逐行格式（保持兼容，回退路径）。
//
// 两种形态语义一致：路径清单，只增不改；重复条目=清单被污染，fail-loud。
func parseManifest(data []byte) (entries []string, dupes []string) {
	var wrapper struct {
		FrozenContracts []string `json:"frozen_contracts"`
	}
	if err := json.Unmarshal(data, &wrapper); err == nil && wrapper.FrozenContracts != nil {
		seen := map[string]bool{}
		for _, line := range wrapper.FrozenContracts {
			line = strings.TrimSpace(filepath.ToSlash(line))
			if line == "" {
				continue
			}
			if seen[line] {
				dupes = append(dupes, line)
				continue
			}
			seen[line] = true
			entries = append(entries, line)
		}
		return entries, dupes
	}
	seen := map[string]bool{}
	for _, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(strings.TrimSuffix(raw, "\r"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = filepath.ToSlash(line)
		if seen[line] {
			dupes = append(dupes, line)
			continue
		}
		seen[line] = true
		entries = append(entries, line)
	}
	return entries, dupes
}

// coverageRefs 遍历 testsDir，返回每个条目被哪些测试文件引用（内容子串匹配
// 完整相对路径）。无法映射的条目不入表——由调用方 fail-loud 列出。
func coverageRefs(repoRoot string, entries []string) (map[string][]string, error) {
	refs := map[string][]string{}
	// 覆盖事实源 = tests/contract 全量（Python 冻结契约测试）+ goRefDirs 的
	// Go 测试文件（T-W5-028 起 v1.1 契约测试在 api 包）。原断言面全保留，引用面只增。
	dirs := []string{filepath.Join(repoRoot, testsRel)}
	for _, rel := range goRefDirs {
		d := filepath.Join(repoRoot, rel)
		if fi, statErr := os.Stat(d); statErr == nil && fi.IsDir() {
			dirs = append(dirs, d)
		}
	}
	walk := func(path string, d fs.DirEntry, werr error) error {
		return walkOne(repoRoot, path, d, werr, entries, refs)
	}
	if err := filepath.WalkDir(dirs[0], walk); err != nil {
		return nil, err
	}
	for _, dir := range dirs[1:] {
		if err := filepath.WalkDir(dir, walk); err != nil {
			return nil, err
		}
	}
	return refs, nil
}

// walkOne 单文件引用判定：tests/contract 下全文本类；goRefDirs 下只认 *_test.go。
// inGoRef 以「相对仓库根的目录前缀」判定（不能用路径子串——tests/contract/api/
// 这类子目录名会与 goRefDirs 的 "api" 撞车，误把 Python 契约测试排除）。
func walkOne(repoRoot string, path string, d fs.DirEntry, werr error, entries []string, refs map[string][]string) error {
	if werr != nil {
		return werr
	}
	if d.IsDir() {
		switch d.Name() {
		case "__pycache__", "testdata", "gen":
			return filepath.SkipDir
		}
		return nil
	}
	relRepo, relErr := filepath.Rel(repoRoot, path)
	relSlash := filepath.ToSlash(relRepo)
	inGoRef := relErr == nil
	if inGoRef {
		inGoRef = false
		for _, rel := range goRefDirs {
			if relSlash == rel || strings.HasPrefix(relSlash, rel+"/") {
				inGoRef = true
				break
			}
		}
	}
	if inGoRef && !strings.HasSuffix(path, "_test.go") {
		return nil // Go 引用面只认测试文件（非测试 Go 文件不是契约事实源）
	}
	switch filepath.Ext(path) { // 只读文本类文件；二进制天然不入事实源
	case ".py", ".md", ".yaml", ".yml", ".txt", ".json", ".sql", ".go":
	default:
		return nil
	}
	fi, serr := d.Info()
	if serr != nil {
		return serr
	}
	if fi.Size() > maxFileBob {
		return nil
	}
	raw, rerr := os.ReadFile(path)
	if rerr != nil {
		return rerr
	}
	content := strings.ReplaceAll(string(raw), "\r\n", "\n")
	for _, e := range entries {
		if strings.Contains(content, e) {
			refs[e] = append(refs[e], relSlash)
		}
	}
	return nil
}

// scanAll 全量遍历断言。操作错误返回 error；逻辑缺口装入 findings（可为空表）.
func scanAll(repoRoot string) (*scanResult, error) {
	data, err := os.ReadFile(filepath.Join(repoRoot, manifestRel))
	if err != nil {
		return nil, fmt.Errorf("读 %s 失败: %w", manifestRel, err)
	}
	testsDir := filepath.Join(repoRoot, testsRel)
	if fi, statErr := os.Stat(testsDir); statErr != nil || !fi.IsDir() {
		return nil, fmt.Errorf("覆盖事实源缺失：%s/ 不存在或不是目录", testsRel)
	}

	entries, dupes := parseManifest(data)
	res := &scanResult{entries: entries, refs: map[string][]string{}}
	for _, d := range dupes {
		res.findings = append(res.findings, finding{entry: d, kind: "duplicate",
			detail: "FROZEN.txt 中重复出现——清单只增不改，重复说明被人为污染"})
	}
	if len(entries) == 0 {
		res.findings = append(res.findings, finding{entry: manifestRel, kind: "empty_manifest",
			detail: "清单为空：没有任何冻结契约受管——守卫失去意义"})
	}

	for _, e := range entries {
		if _, statErr := os.Stat(filepath.Join(repoRoot, filepath.FromSlash(e))); statErr != nil {
			res.findings = append(res.findings, finding{entry: e, kind: "missing_file",
				detail: "冻结契约文件在盘上不存在"})
		}
	}
	refs, cerr := coverageRefs(repoRoot, entries)
	if cerr != nil {
		return nil, fmt.Errorf("遍历 %s/ 失败: %w", testsRel, cerr)
	}
	res.refs = refs
	for _, e := range entries {
		if len(refs[e]) == 0 {
			res.findings = append(res.findings, finding{entry: e, kind: "uncovered",
				detail: "未被 tests/contract 任何文件以完整路径引用——该冻结契约没有活的契约测试"})
		}
	}
	sort.Slice(res.findings, func(i, j int) bool {
		if res.findings[i].kind != res.findings[j].kind {
			return res.findings[i].kind < res.findings[j].kind
		}
		return res.findings[i].entry < res.findings[j].entry
	})
	return res, nil
}

var (
	stdout io.Writer = os.Stdout
	stderr io.Writer = os.Stderr
)

// outPrintf / errPrintf：CLI 输出显式忽略写入错误——结论由退出码承载，
// stderr 管道断裂（如 CI 截断）不得把扫描结论翻转为假绿/假红.
func outPrintf(w io.Writer, format string, a ...any) {
	_, _ = fmt.Fprintf(w, format, a...)
}

func errPrintf(w io.Writer, format string, a ...any) {
	_, _ = fmt.Fprintf(w, format, a...)
}

// run 可测入口：返回进程退出码（0/1/2），并打印人类可读明细.
func run(outw, errw io.Writer, rootFlag string) int {
	root, err := resolveRoot(rootFlag)
	if err != nil {
		errPrintf(errw, "frozencontract: ❌ %v\n", err)
		return 2
	}
	res, err := scanAll(root)
	if err != nil {
		errPrintf(errw, "frozencontract: %v\n", err)
		return 2
	}
	if len(res.findings) > 0 {
		errPrintf(errw, "❌ 冻结契约守卫有缺口（%d 条）：\n", len(res.findings))
		for _, f := range res.findings {
			errPrintf(errw, "  [%s] %s\n      %s\n", f.kind, f.entry, f.detail)
		}
		errPrintf(errw, "修复指引：为 uncovered 条目补 tests/contract 契约测试（内文引用完整 spec 路径）；\n")
		errPrintf(errw, "missing_file 需恢复冻结文件；清单改动必须走契约变更申请→人类批准（P5）。\n")
		return 1
	}
	outPrintf(outw, "✅ FROZEN.txt 全量 %d 个冻结契约：文件均在盘上且都被 tests/contract 引用\n", len(res.entries))
	for _, e := range res.entries {
		outPrintf(outw, "  %-55s <- %s\n", e, strings.Join(res.refs[e], ", "))
	}
	return 0
}

func main() {
	rootFlag := flag.String("root", "", "仓库根目录（默认从当前目录向上找含 specs/contracts/FROZEN.txt 的目录）")
	flag.Parse()
	os.Exit(run(stdout, stderr, *rootFlag))
}
