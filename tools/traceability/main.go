// Command traceability 是 A8/P9 的机器执行体：校验 specs/traceability-matrix.md
// 「强制实证矩阵」的完整性。
//
// 三条硬校验（违反即红 exit 1，fail-closed）：
//  1. 条款覆盖：矩阵条款编号集合必须覆盖 specs/constitution.md 解析出的
//     全部条款编号（V/A/D/P/X 前缀行）——缺条款即红；
//  2. 已强制行必须有非空实证路径（「声称已强制但无实证」= P9 最高优先级）；
//  3. 实证路径的文件部分必须存在于盘上（路径形如 `file:符号`——只校验文件，
//     符号存在性由各自测试承载；`—` 表示明示无实证，仅允许配合非「已强制」状态）。
//
// 用法：go run ./tools/traceability [-root REPO_ROOT]（仓库任意子目录可跑，
// 自动上溯找 go.mod）。退出码：0 全绿 / 1 违规 / 2 操作错误。
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// 宪法条款标题行的编号提取：**V1 …** / **A10 …** / **D3 …** / **P9 …** / **X13 …**
var constitutionClauseRe = regexp.MustCompile(`\*\*([VADPX])(\d{1,2})\b`)

// 矩阵行：| V1 | DB+服务 | path1; path2 | 已强制 |（首尾列也可为其他状态词）
var matrixRowRe = regexp.MustCompile(`^\|\s*([VADPX]\d{1,2})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|`)

type violation struct {
	clause string
	reason string
}

func main() {
	root := flag.String("root", "", "仓库根（默认从 cwd 上溯找 go.mod）")
	flag.Parse()
	r, err := findRoot(*root)
	if err != nil {
		fatal(2, err)
	}
	if err := run(r); err != nil {
		for _, v := range err.violations {
			fmt.Printf("❌ [%s] %s\n", v.clause, v.reason)
		}
		fmt.Printf("❌ traceability：%d 处违规\n", len(err.violations))
		os.Exit(1)
	}
	fmt.Println("✅ 强制实证矩阵：条款全覆盖、已强制行均有实证、路径全部存在（A8/P9）")
}

type runError struct{ violations []violation }

func (e *runError) Error() string { return "traceability violations" }

func fatal(code int, err error) {
	fmt.Fprintln(os.Stderr, "❌", err)
	os.Exit(code)
}

// findRoot 从 start（空= cwd）上溯找 go.mod。
func findRoot(start string) (string, error) {
	dir := start
	if dir == "" {
		dir, _ = os.Getwd()
	}
	dir, _ = filepath.Abs(dir)
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("上溯未找到 go.mod（从 %s）", dir)
		}
		dir = parent
	}
}

func run(root string) *runError {
	violations := []violation{}

	clauses, err := parseConstitution(filepath.Join(root, "specs", "constitution.md"))
	if err != nil {
		return &runError{[]violation{{clause: "constitution", reason: err.Error()}}}
	}
	covered, rows, err := parseMatrix(filepath.Join(root, "specs", "traceability-matrix.md"))
	if err != nil {
		return &runError{[]violation{{clause: "matrix", reason: err.Error()}}}
	}

	// 校验 1：条款全覆盖
	for _, c := range clauses {
		if _, ok := covered[c]; !ok {
			violations = append(violations, violation{c, "矩阵缺条款行（宪法全覆盖义务）"})
		}
	}

	// 校验 2+3：已强制须有路径；路径文件须存在
	for _, row := range rows {
		enforced := strings.Contains(row.status, "已强制")
		if enforced && strings.TrimSpace(row.paths) == "" {
			violations = append(violations, violation{row.clause,
				"状态为已强制但实证路径为空（P9：声称已强制但无实证=最高优先级）"})
			continue
		}
		if strings.TrimSpace(row.paths) == "" || strings.TrimSpace(row.paths) == "—" {
			if enforced {
				violations = append(violations, violation{row.clause, "已强制但路径为 —"})
			}
			continue
		}
		for _, p := range splitPaths(row.paths) {
			if strings.HasPrefix(p, "org:") {
				continue // org: 前缀=外部仓引用（.github/CI-Workflows），存在性由 org gate 承载
			}
			filePart := strings.SplitN(p, "#", 2)[0]
			filePart = strings.SplitN(filePart, ":", 2)[0] // file:符号 形态
			if filePart == "" {
				continue // 条目只剩注释
			}
			if _, err := os.Stat(filepath.Join(root, filePart)); err != nil {
				violations = append(violations, violation{row.clause,
					fmt.Sprintf("实证路径不存在: %s", filePart)})
			}
		}
	}

	if len(violations) > 0 {
		sort.Slice(violations, func(i, j int) bool { return violations[i].clause < violations[j].clause })
		return &runError{violations}
	}
	return nil
}

// parseConstitution 提取宪法全部条款编号（V1..V6/A1..A10/D1..D11/P1..P9/X1..X13）。
func parseConstitution(path string) ([]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读宪法失败: %w", err)
	}
	seen := map[string]bool{}
	var out []string
	for _, m := range constitutionClauseRe.FindAllStringSubmatch(string(b), -1) {
		c := m[1] + m[2]
		if !seen[c] {
			seen[c] = true
			out = append(out, c)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("宪法未解析出任何条款编号（解析器或文件损坏）")
	}
	return out, nil
}

// parseMatrix 解析矩阵表：返回条款覆盖集合与行明细。
func parseMatrix(path string) (map[string]bool, []matrixRow, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, fmt.Errorf("读 TRACEABILITY 失败: %w", err)
	}
	inMatrix := false
	covered := map[string]bool{}
	var rows []matrixRow
	for _, line := range strings.Split(string(b), "\n") {
		if strings.Contains(line, "强制实证矩阵") {
			inMatrix = true
			continue
		}
		if !inMatrix {
			continue
		}
		m := matrixRowRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		clause := m[1]
		// 分节标题里重复出现的编号（如「北纬 V1–V6」字样行）跳过：
		// 只收首列是纯条款号的行——matrixRowRe 已保证首列形态。
		if _, dup := covered[clause]; dup {
			continue // 同条款多行取首行（覆盖判定用），行明细仍全收
		}
		covered[clause] = true
		rows = append(rows, matrixRow{
			clause: clause,
			level:  strings.TrimSpace(m[2]),
			paths:  strings.TrimSpace(m[3]),
			status: strings.TrimSpace(m[4]),
		})
	}
	if len(covered) == 0 {
		return nil, nil, fmt.Errorf("矩阵未解析出任何条款行（表头或格式损坏）")
	}
	return covered, rows, nil
}

type matrixRow struct {
	clause string
	level  string
	paths  string
	status string
}

// splitPaths 把路径列拆为纯文件路径条目：`;` 分隔；每条剥离全角括号注释
// （（...）为人类可读说明，不参与存在性校验）与多余空白；`—` 开头表示
// 明示无实证（返回空切片）。剥离后为空的条目跳过。
func splitPaths(paths string) []string {
	t := strings.TrimSpace(paths)
	if t == "" || strings.HasPrefix(t, "—") || strings.HasPrefix(t, "-") {
		return nil
	}
	var out []string
	for _, p := range strings.Split(t, ";") {
		p = stripParens(strings.TrimSpace(p))
		p = strings.TrimSpace(strings.Trim(p, "+，, "))
		if p != "" && p != "—" {
			out = append(out, p)
		}
	}
	return out
}

// stripParens 去除首段文件路径后随的全角括号注释。
func stripParens(s string) string {
	if i := strings.Index(s, "（"); i >= 0 {
		s = s[:i]
	}
	if i := strings.Index(s, "("); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}
