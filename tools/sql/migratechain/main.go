// Command migratechain 实现 T-W5-022 补缺口的两个迁移链静态守卫（纯标准库、
// 免 Docker，任何人 push 前可本地运行）。
//
// 背景：CI 曾两次在迁移链上红——
//  1. migrate_check.py 报「版本号序列非 0001..00NN 连续」；
//  2. alembic 报 Multiple head revisions（0022 曾把 down_revision 并错前置）。
//
// 这两类问题此前只在 PR 阶段（make check → migrate-go-check，需 Docker + 临时
// PG）才暴露。本命令把它们提取为独立静态守卫：
//
// 守卫 A（版本连续性，continuity）：db/migrations/ 的 NNNN 前缀恰为 0001..N
// 连续序列——从 0001 起、无断档、无重复。此前该检查只活在 migrate_check.py
// 的 check_pairs()（Docker 门后），本地无 Docker 时根本验不了。
//
// 守卫 B（alembic↔golang-migrate 链一致性，alembic-chain）：
//   - alembic/versions/*.py 的 revision/down_revision 构成恰为 0001..N 的单
//     head 线性链：0001 的 down_revision 为 None，其余每 revision 的
//     down_revision 恰为前一 revision——链位置与 NNNN 序号一致，alembic 的
//     应用顺序与 golang-migrate 的字典序完全同构（Multiple head / 分叉 /
//     乱序在此即红）；
//   - alembic revision 与 db/migrations 的 NNNN 前缀一一对应：每 revision 恰
//     有同名一对 NNNN_*.up.sql / NNNN_*.down.sql，反之 db/migrations 不允许
//     出现 alembic 没有的版本；
//   - alembic 文件名前缀必须等于其 revision 声明（映射按文件名走，声明漂移
//     即红）；alembic stem 与 SQL stem 同名（gen_migrations_from_alembic.py
//     再生路径按 stem 产文件，改名会静默破坏再生）。
//
// 与既有检查的分工（不重复造轮子）：
//   - tools/sql/check_pairs.py（SQL-1 静态成对）：命名合规、up/down 成对、
//     非空 down、子目录禁令由它负责，本命令不复检这些规则——版本前缀重复
//     两边都会红（纵深防御，同一根因双报可接受），成对性深检不在此重复；
//   - tools/sql/migrate_check.py（migrate-go-check）：运行时全量验证
//     （parity / down→up cycle / append-only 探针，需 Docker）——本命令是它
//     的免 Docker 静态前哨，不是替代。
//
// 用法：
//
//	go run ./tools/sql/migratechain [-root REPO_ROOT]
//
// -root 可省略：从当前目录逐级上溯找 go.mod 锚定仓库根（tools/scan 同款），
// 因此可在仓库任意子目录或 CI checkout 后的任意工作目录运行。
//
// 退出码：0 = 干净；1 = 存在违规（gate 应拦截）；2 = 操作错误（找不到仓库
// 根 / 迁移面缺失或为空）。迁移面缺失按操作错误而非通过处理——守卫静默空转
// 等于没扫（GO-1 教训）。
package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// finding 是一条违规记录；file 为空表示目录级问题。
type finding struct {
	guard string // "continuity" 或 "alembic-chain"
	file  string // 相对仓库根的 slash 路径
	msg   string
}

func (f finding) String() string {
	if f.file == "" {
		return fmt.Sprintf("❌ [%s] %s", f.guard, f.msg)
	}
	return fmt.Sprintf("❌ [%s] %s: %s", f.guard, f.file, f.msg)
}

func main() {
	root := flag.String("root", "", "仓库根目录（默认从当前目录逐级上溯找 go.mod）")
	flag.Parse()

	r := *root
	if r == "" {
		var err error
		if r, err = findRoot(); err != nil {
			fmt.Fprintln(os.Stderr, "migratechain:", err)
			os.Exit(2)
		}
	}

	code, err := run(r)
	if err != nil {
		fmt.Fprintln(os.Stderr, "migratechain:", err)
		os.Exit(2)
	}
	os.Exit(code)
}

// findRoot 从当前目录逐级上溯找 go.mod（tools/scan/norank 同款锚定）。
func findRoot() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("取当前目录失败: %w", err)
	}
	for {
		if fi, statErr := os.Stat(filepath.Join(dir, "go.mod")); statErr == nil && !fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", errors.New("上溯不到 go.mod：请在仓库内运行，或用 -root 指定仓库根")
		}
		dir = parent
	}
}

// run 执行两个守卫，返回进程退出码（0/1）；操作错误以 error 返回（退出码 2）。
func run(root string) (int, error) {
	migDir := filepath.Join(root, "db", "migrations")
	alembicDir := filepath.Join(root, "alembic", "versions")

	// 操作错误面：目录缺失或扫描面为空 = 守卫空转，按操作错误处理（exit 2）。
	// 后续 check 函数自己重新 glob，这里只负责把"没得扫"拦成显式失败。
	if _, err := globNonEmpty(migDir, "*.up.sql", "db/migrations 无任何 *.up.sql（迁移扫描面为空）"); err != nil {
		return 0, err
	}
	if _, err := globNonEmpty(alembicDir, "*.py", "alembic/versions 无任何 *.py（链扫描面为空）"); err != nil {
		return 0, err
	}

	findings, err := checkContinuity(migDir)
	if err != nil {
		return 0, err
	}
	chainFindings, err := checkAlembicChain(alembicDir, migDir)
	if err != nil {
		return 0, err
	}
	findings = append(findings, chainFindings...)

	if len(findings) > 0 {
		for _, f := range findings {
			fmt.Println(f.String())
		}
		fmt.Printf("❌ migratechain：%d 处违规（守卫 A 版本连续性 / 守卫 B alembic↔golang-migrate 链一致性）\n", len(findings))
		return 1, nil
	}
	fmt.Println("✅ [continuity] db/migrations 版本号 0001..N 连续（免 Docker 静态守卫 A）")
	fmt.Println("✅ [alembic-chain] alembic 单 head 线性链且与 db/migrations NNNN 一一对应（静态守卫 B）")
	return 0, nil
}

// globNonEmpty 断言目录存在且模式至少命中一个文件（扫描面非空），否则操作错误。
func globNonEmpty(dir, pattern, emptyMsg string) ([]string, error) {
	fi, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("%s 不存在: %w", dir, err)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("%s 不是目录", dir)
	}
	matches, err := filepath.Glob(filepath.Join(dir, pattern))
	if err != nil {
		return nil, fmt.Errorf("glob %s: %w", pattern, err)
	}
	if len(matches) == 0 {
		return nil, errors.New(emptyMsg)
	}
	return matches, nil
}

// ── 守卫 A：db/migrations 版本连续性 ────────────────────────────────────────

// checkContinuity 断言 db/migrations 的 *.up.sql NNNN 前缀恰为 0001..N 连续
// （从 0001 起、无断档、无重复）。只取版本前缀做序列断言；成对性/非空 down/
// 命名合规等深检归 check_pairs.py，不在此重复。
func checkContinuity(migDir string) ([]finding, error) {
	ups, err := filepath.Glob(filepath.Join(migDir, "*.up.sql"))
	if err != nil {
		return nil, fmt.Errorf("glob up.sql: %w", err)
	}
	type entry struct {
		rev  int
		name string
	}
	entries := make([]entry, 0, len(ups))
	var findings []finding
	for _, p := range ups {
		prefix := fileVersionPrefix(p)
		n, convErr := strconv.Atoi(prefix)
		if convErr != nil || len(prefix) < 4 {
			findings = append(findings, finding{
				guard: "continuity",
				file:  migRelPath(p),
				msg:   fmt.Sprintf("版本前缀 %q 无法按 NNNN 解析（连续性检查无法进行，fail-loud；命名合规归 check_pairs.py）", prefix),
			})
			continue
		}
		entries = append(entries, entry{rev: n, name: filepath.Base(p)})
	}
	if len(findings) > 0 {
		return findings, nil
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].rev < entries[j].rev })

	seen := map[int]string{}
	var dups []string
	uniq := make([]int, 0, len(entries))
	for _, e := range entries {
		if prev, ok := seen[e.rev]; ok {
			dups = append(dups, fmt.Sprintf("%04d（%s 与 %s）", e.rev, prev, e.name))
			continue
		}
		seen[e.rev] = e.name
		uniq = append(uniq, e.rev)
	}
	// 序列断言：恰为 1..N。与 migrate_check.py check_pairs() 的
	// 「版本号序列非 0001..NNNN 连续」同一语义，但免 Docker 可本地跑。
	var gaps []string
	for i, rev := range uniq {
		if rev != i+1 {
			gaps = append(gaps, fmt.Sprintf("第 %d 个版本应为 %04d，实际 %04d", i+1, i+1, rev))
		}
	}
	if len(dups) > 0 {
		findings = append(findings, finding{guard: "continuity", msg: "版本号重复: " + strings.Join(dups, "; ")})
	}
	if len(gaps) > 0 {
		actual := make([]string, 0, len(uniq))
		for _, rev := range uniq {
			actual = append(actual, fmt.Sprintf("%04d", rev))
		}
		findings = append(findings, finding{
			guard: "continuity",
			msg: fmt.Sprintf("版本号序列非 0001..%04d 连续：[%s]（%s）",
				len(uniq), strings.Join(actual, " "), strings.Join(gaps, "; ")),
		})
	}
	return findings, nil
}

// fileVersionPrefix 取文件名首个 `_` 前的版本前缀（0001_x.up.sql → 0001）。
func fileVersionPrefix(path string) string {
	base := filepath.Base(path)
	prefix, _, _ := strings.Cut(base, "_")
	return prefix
}

// migRelPath / alembicRelPath 把文件路径规整为相对仓库根的 slash 路径（输出
// 与平台无关，Windows 下同样输出 db/migrations/... 形态）。
func migRelPath(path string) string {
	r, err := filepath.Rel("db/migrations", path)
	if err != nil {
		return filepath.ToSlash(path)
	}
	return "db/migrations/" + filepath.ToSlash(r)
}

func alembicRelPath(base string) string { return "alembic/versions/" + base }

func orNone(s string) string {
	if s == "" {
		return "<无>"
	}
	return s
}

// ── 守卫 B：alembic 链 ↔ db/migrations 映射一致性 ─────────────────────────

var (
	// 仅匹配模块级赋值形态（行首 revision / down_revision，注解允许
	// `: str` / `: Union[str, None]` 但不得跨行或含 `=`）；文档串里的
	// 提法（如 0022 的「down_revision 改为 '0021'…」无行首赋值+`=` 形态）
	// 不会误配。同文件多次命中且值冲突才红（防御性，正常文件各恰一处）。
	reRevision = regexp.MustCompile(`(?m)^\s*revision\s*(?::[^=\n]+)?=\s*"([^"]+)"\s*$`)
	reDown     = regexp.MustCompile(`(?m)^\s*down_revision\s*(?::[^=\n]+)?=\s*(?:"([^"]*)"|(None))\s*$`)
)

// checkAlembicChain 做三层静态断言：
//  1. alembic revision 集合恰为 0001..N 连续，且 down_revision 链为
//     0001(None) ← 0002 ← … ← 00N 的单 head 线性链；
//  2. 每 alembic revision 在 db/migrations 恰有同名一对 up/down（stem 与
//     alembic stem 逐字一致），反之不允许有 alembic 没有的版本；
//  3. alembic 文件名前缀 == revision 声明（映射按文件名走）。
//
// 只断言映射与链；成对性深检（空 down、命名合规）仍归 check_pairs.py。
func checkAlembicChain(alembicDir, migDir string) ([]finding, error) {
	pys, err := filepath.Glob(filepath.Join(alembicDir, "*.py"))
	if err != nil {
		return nil, fmt.Errorf("glob alembic py: %w", err)
	}
	type node struct {
		down string // down_revision 的字符串值；isNone 时无意义
		file string // 文件名（base）
	}
	nodes := map[string]node{}  // key = revision 声明
	isNone := map[string]bool{} // revision → down_revision 是否为 None
	var findings []finding

	for _, p := range pys {
		body, readErr := os.ReadFile(p)
		if readErr != nil {
			return nil, fmt.Errorf("读 %s: %w", p, readErr)
		}
		text := string(body)
		base := filepath.Base(p)
		revs := reRevision.FindAllStringSubmatch(text, -1)
		downs := reDown.FindAllStringSubmatch(text, -1)

		if len(revs) == 0 {
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
				msg: "解析不到 revision 声明（链检查无法进行，fail-loud）"})
			continue
		}
		rev := revs[0][1]
		for _, m := range revs[1:] {
			if m[1] != rev {
				findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
					msg: fmt.Sprintf("revision 声明冲突: %q 与 %q", rev, m[1])})
			}
		}
		if prefix := fileVersionPrefix(p); prefix != rev {
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
				msg: fmt.Sprintf("文件名前缀 %q ≠ revision 声明 %q（alembic↔db/migrations 映射按文件名走）", prefix, rev)})
		}
		if len(downs) == 0 {
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
				msg: "解析不到 down_revision 声明（fail-loud）"})
			continue
		}
		rootNone := downs[0][2] == "None"
		down := ""
		if !rootNone {
			down = downs[0][1]
		}
		for _, m := range downs[1:] {
			mNone := m[2] == "None"
			if mNone != rootNone || (!mNone && m[1] != down) {
				findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
					msg: "down_revision 声明冲突（多次赋值且值不一致）"})
			}
		}
		if prev, ok := nodes[rev]; ok {
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(base),
				msg: fmt.Sprintf("revision %q 重复声明（已在 %s 声明）", rev, prev.file)})
			continue
		}
		nodes[rev] = node{down: down, file: base}
		isNone[rev] = rootNone
	}

	// 链断言：revision 集合恰为 0001..N 且逐个 down_revision == 前一 revision。
	// 单 head 线性是它的推论（若出现两个 head，必有某 revision 的
	// down_revision 不等于前一号或集合断档，逐条报出可定位 revision）。
	revs := make([]string, 0, len(nodes))
	for rev := range nodes {
		revs = append(revs, rev)
	}
	sort.Slice(revs, func(i, j int) bool {
		a, _ := strconv.Atoi(revs[i])
		b, _ := strconv.Atoi(revs[j])
		return a < b
	})
	for i, rev := range revs {
		n := nodes[rev]
		switch {
		case i == 0 && !isNone[rev]:
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(n.file),
				msg: fmt.Sprintf("链根 %s 的 down_revision 应为 None，得到 %q（多 root / 链悬挂）", rev, n.down)})
		case i > 0 && isNone[rev]:
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(n.file),
				msg: fmt.Sprintf("%s 的 down_revision 应为 %q，得到 None（Multiple head：出现第二个链根）", rev, revs[i-1])})
		case i > 0 && n.down != revs[i-1]:
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(n.file),
				msg: fmt.Sprintf("%s 的 down_revision 应为 %q（单 head 线性链，链位=序号），得到 %q", rev, revs[i-1], n.down)})
		}
	}
	// revision 集合连续性（断档单独点名，与链序错误互补）
	var setGaps []string
	for i, rev := range revs {
		if want := fmt.Sprintf("%04d", i+1); rev != want {
			setGaps = append(setGaps, fmt.Sprintf("第 %d 个 revision 应为 %s，实际 %s", i+1, want, rev))
		}
	}
	if len(setGaps) > 0 {
		findings = append(findings, finding{guard: "alembic-chain",
			msg: "alembic revision 集合非 0001..N 连续: " + strings.Join(setGaps, "; ")})
	}

	// 映射断言：alembic revision ↔ db/migrations NNNN 双向一一对应，且 stem
	// 与 alembic stem 逐字一致（gen_migrations_from_alembic 再生按 stem 产文
	// 件）。每个 revision 恰一对 up/down 的存在性在此断言；成对语义深检
	// （非空 down 等）归 check_pairs.py。
	byRev := map[string][2]string{} // rev → {up, down} 文件名
	sqls, err := filepath.Glob(filepath.Join(migDir, "*.sql"))
	if err != nil {
		return nil, fmt.Errorf("glob sql: %w", err)
	}
	for _, p := range sqls {
		base := filepath.Base(p)
		kind := ""
		switch {
		case strings.HasSuffix(base, ".up.sql"):
			kind = "up"
		case strings.HasSuffix(base, ".down.sql"):
			kind = "down"
		default:
			findings = append(findings, finding{guard: "alembic-chain", file: "db/migrations/" + base,
				msg: "文件名既非 *.up.sql 也非 *.down.sql（映射无法归位，fail-loud）"})
			continue
		}
		rev := fileVersionPrefix(p)
		pair := byRev[rev]
		if kind == "up" {
			pair[0] = base
		} else {
			pair[1] = base
		}
		byRev[rev] = pair
	}
	for _, rev := range revs {
		n := nodes[rev]
		wantStem := strings.TrimSuffix(n.file, ".py")
		pair := byRev[rev]
		switch {
		case pair[0] == "" || pair[1] == "":
			missing := "up"
			if pair[0] != "" {
				missing = "down"
			}
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(n.file),
				msg: fmt.Sprintf("alembic %s 在 db/migrations 缺 %s 侧 SQL（alembic↔golang-migrate 一一对应破坏）", rev, missing)})
		case pair[0] != wantStem+".up.sql" || pair[1] != wantStem+".down.sql":
			findings = append(findings, finding{guard: "alembic-chain", file: alembicRelPath(n.file),
				msg: fmt.Sprintf("SQL stem 与 alembic stem 不一致：期望 %s.{up,down}.sql，实际 %s / %s（gen_migrations_from_alembic 再生按 stem 产文件）",
					wantStem, orNone(pair[0]), orNone(pair[1]))})
		}
	}
	for rev, pair := range byRev {
		if _, ok := nodes[rev]; !ok {
			findings = append(findings, finding{guard: "alembic-chain", file: "db/migrations/" + pair[0],
				msg: fmt.Sprintf("db/migrations 版本 %s 在 alembic/versions 无对应 revision（两链一一对应破坏）", rev)})
		}
	}
	return findings, nil
}
