// Package main 实现 T-W5-023 守卫 B：无排名静态扫描（宪法 D8/铁律 7）.
//
// 落地 specs/constitution.md#D8「代码层不得提供跨用户成绩排名的查询路径；
// 对外呈现一律等级化」。这是冻结实现 scripts/ci/check_no_ranking.py 的 Go
// 重锚定（tasks/w5/REANCHORING.md T-W5-023 行），语义不变并补齐其盲区：
//   - 只扫 SQL 字符串与 db/queries/*.sql（误报面控制；不扫注释/任意文本）
//   - SQL 文本模式与冻结实现逐条同构（ORDER BY score、窗口排名函数、
//     ROW_NUMBER OVER、rank 独立词列、路由路径 /rank|leaderboard）
//   - 新增跨用户聚合特征：聚合(score 列) + student_alias 维度且无 per-user
//     WHERE 边界 → 命中（任务卡验收 #2「跨 alias 聚合查询」盲区）
//   - 白名单逃逸口：// norank-allow <理由> / -- norank-allow <理由>，
//     理由缺失本身即违规（fail-loud，防白名单被无脑滥用）
//
// 扫描面：core/ api/ 的 .go（AST 字符串常量、GORM 风格 Order 调用、函数名）
// 与 db/queries/*.sql 全文；排除生成物 baml_client/ db/gen/ 与 testdata。
// 扫描根目录任一不存在或为空都按操作错误退出 2——守卫静默空转等于没扫。
//
// 用法：
//
//	go run ./tools/scan/norank [-root REPO_ROOT]
//
// 退出码：0 = 干净；1 = 存在违规；2 = 操作错误（根目录找不到/扫描面缺失）。
package main

import (
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const (
	snippetMax    = 120            // 违规片段截断长度（防刷屏，对齐冻结实现）
	marker        = "norank-allow" // 白名单标记（Go/SQL 同名，理由必填）
	coreRel       = "core"         // 默认扫描根（相对仓库根）
	apiRel        = "api"          //
	sqlQueriesRel = "db/queries"   //
)

// ── 检测模式（逐条移植 scripts/ci/check_no_ranking.py 的语义基准）──────────

// 成绩类列名：表达学生表现度量，按其排序即排名。
const scoreColsSrc = `(?:score|total_score|total_points|points|correct_count|` +
	`correct_rate|correctness|accuracy|exam_score|test_score|raw_score|` +
	`scaled_score|percentile)`

var (
	// 1. SQL ORDER BY + 成绩列（跨用户排名的典型形态）
	reOrderByScore = regexp.MustCompile(`(?i)ORDER\s+BY[^\n;]*?\b(?:` + scoreColsSrc + `)\b`)
	// 2. 窗口排名函数 RANK()/DENSE_RANK()/PERCENT_RANK()/CUME_DIST()
	reWindowRank = regexp.MustCompile(`(?i)\b(?:RANK|DENSE_RANK|PERCENT_RANK|CUME_DIST)\s*\(\s*\)`)
	// 3. ROW_NUMBER() OVER(...成绩列...)：按成绩编号即排名
	reRownumOverScore = regexp.MustCompile(`(?i)ROW_NUMBER\s*\(\s*\)\s+OVER\s*\([^)]*?\b(?:` + scoreColsSrc + `)\b`)
	// 4. rank 作为独立词出现在查询子句中
	reRankColumn = regexp.MustCompile(`(?i)\b(?:SELECT|WHERE|ORDER\s+BY|GROUP\s+BY|HAVING)[^\n;]*?\brank\b`)
	// 成绩列独立词（供 GORM Order 启发式复用）
	reScoreToken = regexp.MustCompile(`(?i)\b(?:` + scoreColsSrc + `)\b`)
	// 5a. 聚合函数作用于成绩列
	reAggScore = regexp.MustCompile(`(?i)\b(?:MAX|MIN|AVG|SUM)\s*\([^)]*?\b(?:` + scoreColsSrc + `)\b`)
	// 5b. student_alias 维度出现在语句内
	reAliasDim = regexp.MustCompile(`(?i)\bstudent_alias`)
	// 5c. per-user WHERE 边界：student_alias_id 以 = / IN / ANY 绑定到具体参数
	reAliasBound = regexp.MustCompile(`(?i)\bstudent_alias_id\b\s*(?:=\s*(?:\$\d+|\S)|=\s*ANY\s*\(|\bIN\b\s*\()`)
	// SQL 门（只把形似 SQL 的字符串当 SQL 扫——误报面控制的关键闸门）
	reSQLStatement = regexp.MustCompile(`(?i)\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b`)
	// 6. 路由路径含排名语义。只对路径形字符串生效，天然覆盖 prefix 拼接后的
	//    完整路径——拼接两侧只要子片段含 /ranking 即命中，前缀在哪不影响检测
	reRouteRank = regexp.MustCompile(`(?i)/(?:rank|ranking|rankings|leaderboard|leaderboards)\b`)
	// 7. 函数/查询名含排名语义。前两个分支逐字移植冻结实现的正则（snake_case
	//    锚定）；第三分支是 Go 补丁：冻结实现的正则对驼峰名是盲的
	//    （GetStudentRanking 既非 ^/_ 锚定结尾，也非 get 后紧跟 rank），这里用
	//    「动作动词开头 + 排名名词收尾」的双锚定兜住 CamelCase 查询处理函数，
	//    同时保证 EnsureNoRanking 这类守卫/禁令说明性命名不误报。
	reNameRank = regexp.MustCompile(`(?i)(?:^|_)(?:rank|ranking|leaderboard)(?:s|ing|_for|_by|_list)?$|` +
		`^(?:get|compute|fetch|list|build)_?(?:rank|ranking|leaderboard)|` +
		`^(?:get|compute|fetch|list|build|query|load|top)\w*(?:ranking|leaderboard)s?$`)

	// sqlc 查询名行：-- name: Ident :one
	reSQLCName = regexp.MustCompile(`^--\s*name:\s*(\S+)`)
)

// violation 一处排名违规（字段语义对齐冻结实现）.
type violation struct {
	file     string
	line     int // 1-based
	category string
	snippet  string
}

func (v violation) String() string {
	return fmt.Sprintf("%s:%d [%s] %s", v.file, v.line, v.category, v.snippet)
}

func less(a, b violation) bool {
	if a.file != b.file {
		return a.file < b.file
	}
	if a.line != b.line {
		return a.line < b.line
	}
	if a.category != b.category {
		return a.category < b.category
	}
	return a.snippet < b.snippet
}

func sortViolations(vs []violation) {
	for i := 1; i < len(vs); i++ {
		for j := i; j > 0 && less(vs[j], vs[j-1]); j-- {
			vs[j], vs[j-1] = vs[j-1], vs[j]
		}
	}
}

func dedupe(vs []violation) []violation {
	seen := map[violation]bool{}
	res := vs[:0]
	for _, v := range vs {
		if seen[v] {
			continue
		}
		seen[v] = true
		res = append(res, v)
	}
	sortViolations(res)
	return res
}

// allowVerdict 对一个区间的白名单判定结果.
type allowVerdict int

const (
	allowNone allowVerdict = iota // 无标记：维持全部候选
	allowOK                       // 有合法标记：候选全免
	allowBad                      // 有标记但缺理由：追加 whitelist_no_reason
)

// judgeAllow 逐行找标记：首个标记合法 → allowOK；只有残缺标记 → allowBad 并给出所在行.
func judgeAllow(rows []string, firstLine int) (allowVerdict, int) {
	for i, row := range rows {
		idx := strings.Index(row, marker)
		if idx < 0 {
			continue
		}
		rest := strings.TrimSpace(row[idx+len(marker):])
		reason := strings.TrimSpace(strings.TrimPrefix(rest, ":"))
		valid := rest != "" && !strings.HasPrefix(rest, "-") &&
			!(strings.HasPrefix(rest, ":") && reason == "")
		if valid {
			return allowOK, firstLine + i
		}
		return allowBad, firstLine + i
	}
	return allowNone, 0
}

// spannedViolations 候选违规 + 标记可见的行区间 [firstLine,lastLine].
type spannedViolations struct {
	v         []violation
	firstLine int
	lastLine  int
}

// applyAllow 对一组候选做白名单过滤；返回保留项与新增的 invalid-marker 违规.
func applyAllow(sv spannedViolations, fileLines []string, fileLabel string) ([]violation, []violation) {
	if len(sv.v) == 0 {
		return nil, nil
	}
	from, to := sv.firstLine, sv.lastLine
	if from < 1 || from > to || to > len(fileLines) {
		return sv.v, nil
	}
	verdict, row := judgeAllow(fileLines[from-1:to], from)
	switch verdict {
	case allowOK:
		return nil, nil
	case allowBad:
		return nil, []violation{{
			file:     fileLabel,
			line:     row,
			category: "whitelist_no_reason",
			snippet:  clip(fileLines[row-1]),
		}}
	default:
		return sv.v, nil
	}
}

// resolveRoot 定位仓库根：显式 -root 必须含 go.mod；否则从当前目录逐级上溯.
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
		if fi, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil && !fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			if flagVal != "" {
				return "", fmt.Errorf("-root %q 下未找到 go.mod，不是仓库根", flagVal)
			}
			return "", fmt.Errorf("从 %s 上溯未找到 go.mod——请在仓库内运行或用 -root 指定", start)
		}
		dir = parent
	}
}

// walkFiles 收集 rootDir 下指定后缀文件（第二返回值=全部普通文件数，
// 用于「扫描面为空」判定——GO-1 教训：门空转等于没扫）；跳过 testdata 与生成物目录.
func walkFiles(rootDir string, suffixes ...string) (files []string, allCount int, err error) {
	werr := filepath.WalkDir(rootDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "testdata":
				// 测试 fixture 不参与对外扫描
				return filepath.SkipDir
			case "baml_client", "gen":
				// 生成物永不在面上（即使根目录配置误配也兜底排除）
				return filepath.SkipDir
			}
			return nil
		}
		allCount++
		for _, suf := range suffixes {
			if strings.HasSuffix(path, suf) {
				files = append(files, path)
				break
			}
		}
		return nil
	})
	return files, allCount, werr
}

// scanRoot 扫描整个默认扫描面，返回去重排序后的违规列表.
func scanRoot(repoRoot string) ([]violation, error) {
	var out []violation

	scanGoArea := func(rel string) error {
		rootDir := filepath.Join(repoRoot, rel)
		if fi, statErr := os.Stat(rootDir); statErr != nil || !fi.IsDir() {
			return fmt.Errorf("扫描面缺失：%s/（守卫拒绝空转）", rel)
		}
		gofiles, _, werr := walkFiles(rootDir, ".go")
		if werr != nil {
			return fmt.Errorf("遍历 %s/ 失败: %w", rel, werr)
		}
		if len(gofiles) == 0 {
			return fmt.Errorf("扫描面为空：%s/ 下没有 .go 文件（守卫拒绝空转）", rel)
		}
		for _, f := range gofiles {
			vs, serr := scanGoFile(f)
			if serr != nil {
				return fmt.Errorf("%s: %w", f, serr)
			}
			out = append(out, vs...)
		}
		return nil
	}
	for _, rel := range []string{coreRel, apiRel} {
		if err := scanGoArea(rel); err != nil {
			return nil, err
		}
	}

	sqlRel := sqlQueriesRel
	sqlRoot := filepath.Join(repoRoot, sqlRel)
	if fi, statErr := os.Stat(sqlRoot); statErr != nil || !fi.IsDir() {
		return nil, fmt.Errorf("扫描面缺失：%s/（守卫拒绝空转）", sqlRel)
	}
	sqlfiles, _, werr := walkFiles(sqlRoot, ".sql")
	if werr != nil {
		return nil, fmt.Errorf("遍历 %s/ 失败: %w", sqlRel, werr)
	}
	if len(sqlfiles) == 0 {
		return nil, fmt.Errorf("扫描面为空：%s/ 下没有 .sql 文件（守卫拒绝空转）", sqlRel)
	}
	for _, f := range sqlfiles {
		vs, serr := scanSQLFile(f)
		if serr != nil {
			return nil, fmt.Errorf("%s: %w", f, serr)
		}
		out = append(out, vs...)
	}
	out = dedupe(out)
	return out, nil
}

// lineAt 把 text 内字节偏移换算成 1-based 行号（baseLine 为 text 第一行的行号）.
func lineAt(text string, off int, baseLine int) int {
	return baseLine + strings.Count(text[:off], "\n")
}

// clip 截断违规片段.
func clip(s string) string {
	s = strings.TrimSpace(s)
	if len(s) > snippetMax {
		return s[:snippetMax]
	}
	return s
}

func lineStartOffset(text string, off int) int {
	if i := strings.LastIndexByte(text[:off], '\n'); i >= 0 {
		return i + 1
	}
	return 0
}

func lineEndOffset(text string, off int) int {
	if i := strings.IndexByte(text[off:], '\n'); i >= 0 {
		return off + i
	}
	return len(text)
}

// checkSQLText 对一段 SQL 文本跑全部文本级模式，行号相对 baseLine 锚定.
func checkSQLText(fileLabel, text string, baseLine int) []violation {
	var out []violation
	addMatch := func(re *regexp.Regexp, cat string) {
		loc := re.FindStringIndex(text)
		if loc == nil {
			return
		}
		row := lineAt(text, loc[0], baseLine)
		ln := lineStartOffset(text, loc[0])
		end := lineEndOffset(text, loc[0])
		out = append(out, violation{
			file: fileLabel, line: row, category: cat,
			snippet: clip(text[ln:end]),
		})
	}
	addMatch(reOrderByScore, "sql_order_by_score")
	addMatch(reWindowRank, "sql_window_rank")
	addMatch(reRownumOverScore, "sql_rownum_over_score")
	addMatch(reRankColumn, "sql_rank_column")

	// 跨用户聚合特征：同一语句单元（分号切分）里既有 agg(成绩列)，又出现
	// student_alias 维度，且 alias 没有绑定到具体主体参数——即「按学生分组比
	// 分数」的无界形态。有 per-user 边界的单生统计不误报；更复杂的形态走
	// norank-allow 显式豁免并写明理由。
	for _, unit := range strings.Split(text, ";") {
		aggLoc := reAggScore.FindStringIndex(unit)
		if aggLoc == nil || !reAliasDim.MatchString(unit) || reAliasBound.MatchString(unit) {
			continue
		}
		posInUnit := aggLoc[0]
		unitOff := strings.Index(text, unit[:posInUnit])
		if unitOff < 0 {
			unitOff = 0
		}
		globalOff := unitOff + posInUnit
		row := lineAt(text, globalOff, baseLine)
		ln := lineStartOffset(text, globalOff)
		end := lineEndOffset(text, globalOff)
		out = append(out, violation{
			file: fileLabel, line: row, category: "cross_alias_agg_score",
			snippet: clip(text[ln:end]),
		})
		break // 一个文本段报一次即可，避免刷屏
	}
	return out
}

// evalConstString 求值可静态确定的字符串表达式：BasicLit 或纯字面量拼接链.
func evalConstString(n ast.Expr) (val string, ok bool) {
	switch e := n.(type) {
	case *ast.BasicLit:
		if e.Kind != token.STRING {
			return "", false
		}
		s, uerr := strconv.Unquote(e.Value)
		if uerr != nil {
			return "", false
		}
		return s, true
	case *ast.BinaryExpr:
		if e.Op != token.ADD {
			return "", false
		}
		lhs, lok := evalConstString(e.X)
		rhs, rok := evalConstString(e.Y)
		if lok && rok {
			return lhs + rhs, true
		}
		return "", false
	default:
		return "", false
	}
}

// scanGoFile 扫描单个 .go：字符串常量（SQL 形态/路由路径）、Order 调用、函数名.
func scanGoFile(path string) ([]violation, error) {
	src, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, rerr
	}
	fset := token.NewFileSet()
	file, perr := parser.ParseFile(fset, path, src, parser.ParseComments|parser.SkipObjectResolution)
	if perr != nil {
		return nil, perr
	}
	lines := strings.Split(string(src), "\n")
	rel := filepath.ToSlash(filepath.Clean(path)) // 显示用相对路径（walkFiles 以相对根启动）

	var out []violation

	handleString := func(val string, startPos, endPos token.Pos) {
		if strings.TrimSpace(val) == "" {
			return
		}
		startLine := fset.Position(startPos).Line
		endLine := fset.Position(endPos).Line
		var cands []violation

		// SQL 门：仅当内容形似 SQL 才进入 SQL 模式匹配（误报面控制）
		if reSQLStatement.MatchString(val) {
			cands = append(cands, checkSQLText(rel, val, startLine)...)
		}
		// 路由路径：路径形字符串（首非空白字符为 '/'）
		if strings.HasPrefix(strings.TrimSpace(val), "/") && reRouteRank.MatchString(val) {
			cands = append(cands, violation{
				file: rel, line: startLine, category: "route_rank_path",
				snippet: clip(val),
			})
		}
		if len(cands) == 0 {
			return
		}
		to := endLine
		if to > len(lines) {
			to = len(lines)
		}
		keep, extra := applyAllow(spannedViolations{
			v: cands, firstLine: startLine, lastLine: to,
		}, lines, rel)
		out = append(out, keep...)
		out = append(out, extra...)
	}

	ast.Inspect(file, func(n ast.Node) bool {
		switch node := n.(type) {
		case *ast.FuncDecl:
			if !reNameRank.MatchString(node.Name.Name) {
				return true
			}
			firstLine := fset.Position(node.Pos()).Line
			lastLine := firstLine
			rows := []string{}
			if node.Doc != nil {
				for _, c := range node.Doc.List {
					txt := c.Text // // 注释原文（含注释符），天然支持 norank-allow 出现在 doc
					rows = append(rows, txt)
				}
				firstLine = fset.Position(node.Doc.Pos()).Line
				lastLine = fset.Position(node.Doc.End()).Line
			} else if declLine := fset.Position(node.Pos()).Line; declLine <= len(lines) {
				rows = []string{lines[declLine-1]}
			}
			if lastLine > len(lines) {
				lastLine = len(lines)
			}
			keep, extra := applyAllow(spannedViolations{
				v: []violation{{
					file: rel, line: fset.Position(node.Pos()).Line, category: "func_name_rank",
					snippet: clip("func " + node.Name.Name),
				}},
				firstLine: firstLine, lastLine: lastLine,
			}, lines, rel)
			out = append(out, keep...)
			out = append(out, extra...)
		case *ast.CallExpr:
			// GORM 风格 .Order(...)/.OrderBy(...)：对应冻结实现的 orm_order_by_score
			sel, isSel := node.Fun.(*ast.SelectorExpr)
			if !isSel || (sel.Sel.Name != "Order" && sel.Sel.Name != "OrderBy") {
				return true
			}
			for _, arg := range node.Args {
				val, ok := evalConstString(arg)
				if !ok || !reScoreToken.MatchString(val) {
					continue
				}
				startLine := fset.Position(arg.Pos()).Line
				lastLine := fset.Position(arg.End()).Line
				to := lastLine
				if to+1 <= len(lines) { // 容纳行尾同行注释
					to++
				}
				keep, extra := applyAllow(spannedViolations{
					v: []violation{{
						file: rel, line: startLine, category: "orm_order_by_score",
						snippet: clip("." + sel.Sel.Name + "(" + val + ")"),
					}},
					firstLine: startLine, lastLine: to,
				}, lines, rel)
				out = append(out, keep...)
				out = append(out, extra...)
			}
		case *ast.BasicLit:
			if val, ok := evalConstString(node); ok {
				handleString(val, node.Pos(), node.End())
			}
		case *ast.BinaryExpr:
			if val, ok := evalConstString(node); ok {
				handleString(val, node.Pos(), node.End())
			}
		}
		return true
	})
	return dedupe(out), nil
}

// scanSQLFile 扫描单个 db/queries/*.sql：按 sqlc 查询名分节，
// 每节整体跑文本模式 + 查询名语义 + 节级 norank-allow.
func scanSQLFile(path string) ([]violation, error) {
	raw, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, rerr
	}
	lines := strings.Split(string(raw), "\n")
	rel := filepath.ToSlash(filepath.Clean(path))

	var out []violation
	flushStanza := func(start, end int) { // [start,end) 行闭开区间
		if start >= end {
			return
		}
		text := strings.Join(lines[start:end], "\n")
		baseLine := start + 1
		cands := checkSQLText(rel, text, baseLine)
		for i := start; i < end; i++ { // 查询名语义：-- name: XxxRanking :one
			m := reSQLCName.FindStringSubmatch(lines[i])
			if m == nil {
				continue
			}
			if reNameRank.MatchString(m[1]) {
				cands = append(cands, violation{
					file: rel, line: i + 1, category: "query_name_rank",
					snippet: clip(lines[i]),
				})
			}
		}
		if len(cands) == 0 {
			return
		}
		keep, extra := applyAllow(spannedViolations{
			v: cands, firstLine: start + 1, lastLine: end,
		}, lines, rel)
		out = append(out, keep...)
		out = append(out, extra...)
	}
	stanzaStart := 0
	for i, ln := range lines {
		if reSQLCName.MatchString(ln) {
			flushStanza(stanzaStart, i)
			stanzaStart = i
		}
	}
	flushStanza(stanzaStart, len(lines))
	return dedupe(out), nil
}

// outPrintf / errPrintf：CLI 输出显式忽略写入错误——结论由退出码承载，
// stderr 管道断裂（如 CI 截断）不得把扫描结论翻转为假绿/假红.
func outPrintf(w io.Writer, format string, a ...any) {
	_, _ = fmt.Fprintf(w, format, a...)
}

func errPrintf(w io.Writer, format string, a ...any) {
	_, _ = fmt.Fprintf(w, format, a...)
}

// run 可测入口：返回进程退出码（0/1/2）.
func run(outw, errw io.Writer, rootFlag string) int {
	root, err := resolveRoot(rootFlag)
	if err != nil {
		errPrintf(errw, "norank: ❌ %v\n", err)
		return 2
	}
	violations, serr := scanRoot(root)
	if serr != nil {
		errPrintf(errw, "norank: 扫描失败: %v\n", serr)
		return 2
	}
	if len(violations) > 0 {
		errPrintf(errw, "❌ 发现 %d 处跨用户排名查询路径（违反 D8）：\n", len(violations))
		for _, v := range violations {
			errPrintf(errw, "  %s\n", v)
		}
		errPrintf(errw, "如确属合法单用户内排序，请在该处加注释：// norank-allow <理由>（SQL 文件用 -- norank-allow <理由>）\n")
		return 1
	}
	outPrintf(outw, "✅ 未发现跨用户排名查询路径（D8 实证通过）：core/ api/ db/queries/\n")
	return 0
}

var (
	stdout io.Writer = os.Stdout
	stderr io.Writer = os.Stderr
)

func main() {
	rootFlag := flag.String("root", "", "仓库根目录（默认从当前目录向上找含 go.mod 的目录）")
	flag.Parse()
	os.Exit(run(stdout, stderr, *rootFlag))
}
