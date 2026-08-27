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

const goModStub = "module example.com/proj\n\ngo 1.26\n"

// goodProject 覆盖「合法形态必须零误报」的每类易爆点：
// 合法单用户排序、有 per-user 边界的聚合、GORM created_at、真实路由、
// 注释里的禁令说明文字、生成物与 testdata 目录（应被排除在外）.
func goodProjectFiles() map[string]string {
	return map[string]string{
		"go.mod": goModStub,
		"core/session/repo.go": `package session

// D8 说明：本模块禁止跨用户排名查询；对外呈现一律等级化。
// 这段中文里出现 排名/rank 字样属于禁令说明，不是查询实现。

const orderByTime = "ORDER BY activated_at DESC LIMIT 1"

// 有 per-user 边界的聚合是合法单生统计（别名已绑定主体参数）。
const myStatsSQL = "SELECT student_alias_id, AVG(correct_rate) AS rate FROM response_event WHERE student_alias_id = $1 GROUP BY student_alias_id"

func QueryOwnSessions(aliasID string) ([]string, error) {
	_ = orderByTime
	_ = myStatsSQL
	return nil, nil
}
`,
		"core/events/writer_mock.go": `package events

type querier interface{ Exec(q string) error }

func write(f querier) {
	if err := f.Exec("INSERT INTO response_event (event_id) VALUES ($1)"); err != nil {
		panic(err)
	}
}
`,
		"api/routes.go": `package api

import "net/http"

func Register(mux *http.ServeMux) {
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {})
	mux.HandleFunc("/students/self/practice", nil)
	mux.HandleFunc("/progress", nil)
}
`,
		"db/queries/content.sql": `-- name: ListItemVersions :many
SELECT * FROM item_version WHERE item_id = $1 ORDER BY created_at ASC;

-- name: InsertResponseEvent :exec
INSERT INTO response_event (event_id, scoring_trace) VALUES ($1, $2);
`,
		"db/queries/estimator.sql": `-- name: GetActiveEstimatorRun :one
SELECT * FROM estimator_run
WHERE purpose_scope = $1 AND retired_at IS NULL
ORDER BY activated_at DESC LIMIT 1;

-- name: ListEstimatorRunLineage :many
SELECT * FROM estimator_run WHERE purpose_scope = $1 ORDER BY activated_at ASC;
`,
		// 生成物与测试夹具即使被误配进扫描根也必须被排除：
		"api/gen/z_generated.go":     "package gen\n\nvar RankBomb = \"ORDER BY score DESC\"\n",
		"core/legacy/testdata/f.sql": "-- name: X :many\nSELECT RANK() OVER (ORDER BY points DESC) FROM t;\n",
		"db/queries/gen/g.sql":       "-- name: BadBomb :one\nSELECT DENSE_RANK() OVER () FROM t;\n",
	}
}

// badProject 每个违规类别各放一处，验证守卫红向必中.
func badProjectFiles() map[string]string {
	return map[string]string{
		"go.mod": goModStub,
		"core/report/bad.go": `package report

const orderScoreSQL = "SELECT alias_id, total_score FROM score_row ORDER BY total_score DESC LIMIT 100"

const windowSQL = "SELECT name, RANK() OVER (ORDER BY points DESC) AS r FROM response_event"

const rownumSQL = "SELECT ROW_NUMBER() OVER (PARTITION BY student_alias_id ORDER BY accuracy DESC) FROM answers"

const whereRankSQL = "SELECT * FROM scores WHERE rank <= 50"

// 跨用户聚合：按学生分组比分数，且没有把 student_alias_id 绑定到具体主体参数
const crossAliasAggSQL = "SELECT student_alias_id, SUM(total_points) AS pts FROM response_event GROUP BY student_alias_id"

type scoreQ struct{}

func (scoreQ) Order(s string) scoreQ { return scoreQ{} }

func gormLike() {
	var db scoreQ
	_ = db.Order("total_score DESC") // GORM 风格按成绩排序：对应冻结实现 orm_order_by_score
}

func GetStudentRanking(subject string) ([]int, error) { return nil, nil }
`,
		"api/bad_routes.go": `package api

import "net/http"

var pathPrefix = "/api/v1"

func Register(mux *http.ServeMux) {
	mux.HandleFunc("/leaderboard", nil)
	// 任务卡盲区：router prefix 拼接后的完整路径由子片段命中
	mux.HandleFunc(pathPrefix+"/ranking", nil)
}
`,
		"db/queries/bad.sql": `-- T-W5-023 红样例：名字与语句双双违规
-- name: ListStudentRanking :many
SELECT alias_id, correct_count FROM answer_events
ORDER BY correct_count DESC
LIMIT 50;

-- 白名单缺失理由的残缺标记也要红（fail-loud）
-- name: LegacyExport :one
-- norank-allow
SELECT * FROM score_export ORDER BY percentile DESC LIMIT 1;
`,
	}
}

func categories(vs []violation) map[string]int {
	out := map[string]int{}
	for _, v := range vs {
		out[v.category]++
	}
	return out
}

func TestScanBadProject_AllCategoriesHit(t *testing.T) {
	vs, err := scanRoot(writeTree(t, badProjectFiles()))
	if err != nil {
		t.Fatalf("scanRoot: %v", err)
	}
	got := categories(vs)
	want := []string{
		"sql_order_by_score",
		"sql_window_rank",
		"sql_rownum_over_score",
		"sql_rank_column",
		"cross_alias_agg_score",
		"orm_order_by_score",
		"route_rank_path",
		"func_name_rank",
		"query_name_rank",
		"whitelist_no_reason", // 残缺标记 fail-loud
	}
	for _, cat := range want {
		if got[cat] == 0 {
			t.Errorf("类别 %s 未命中（got=%v）", cat, got)
		}
	}
	// 排除面实证：生成物 gen/ 与 testdata 的炸弹不得进入违规集
	for _, v := range vs {
		if strings.Contains(v.file, "gen/") || strings.Contains(v.file, "testdata/") {
			t.Errorf("生成物/夹具不应被扫：%s", v.file)
		}
		// prefix 拼接的前半段独立成串时不算路径违规
		if v.category == "route_rank_path" && v.snippet == "/api/v1" {
			t.Errorf("prefix 片段不应被判为路径违规: %+v", v)
		}
	}
	// 两个 stanza 各有一处 ORDER BY 成绩列；第二个（percentile）带残缺标记。
	// 单独复扫该 SQL 文件：第二 stanza 的原违规必须被摘除，只剩 whitelist_no_reason
	// 与第一节的自然命中——验证"标记生效但残缺时 fail-loud"而非静默放行或误摘。
	stanzaVS, serr := scanSQLFile(filepath.Join(
		writeTree(t, badProjectFiles()), "db", "queries", "bad.sql"))
	if serr != nil {
		t.Fatalf("scanSQLFile: %v", serr)
	}
	stanzaGot := categories(stanzaVS)
	if stanzaGot["sql_order_by_score"] != 1 || stanzaGot["whitelist_no_reason"] != 1 {
		t.Errorf("stanza 摘除语义不符：期望恰 1 处 order-by + 1 处缺理由标记，得 %v", stanzaGot)
	}
}

func TestScanGoodProject_ZeroFalsePositive(t *testing.T) {
	vs, err := scanRoot(writeTree(t, goodProjectFiles()))
	if err != nil {
		t.Fatalf("scanRoot: %v", err)
	}
	if len(vs) != 0 {
		t.Errorf("合法项目不应误报：%s", joinViolations(vs))
	}
}

func TestNorankAllowSameLineOnGoLiteral(t *testing.T) {
	src := `package p

var okay = "SELECT * FROM t ORDER BY score DESC" // norank-allow 单用户自评历史列内排序，边界见评审记录 PR#61
`
	root := writeTree(t, map[string]string{"go.mod": goModStub, "core/p/p.go": src})
	vs, err := scanGoFile(filepath.Join(root, "core", "p", "p.go"))
	if err != nil {
		t.Fatalf("scanGoFile: %v", err)
	}
	if len(vs) != 0 {
		t.Errorf("同线合法豁免失效：%v", vs)
	}

	bare := `package p

var bad = "SELECT * FROM t ORDER BY score DESC" // norank-allow
`
	root2 := writeTree(t, map[string]string{"go.mod": goModStub, "core/p/p.go": bare})
	vs2, err := scanGoFile(filepath.Join(root2, "core", "p", "p.go"))
	if err != nil {
		t.Fatalf("scanGoFile: %v", err)
	}
	if len(vs2) != 1 || vs2[0].category != "whitelist_no_reason" {
		t.Errorf("缺理由的标记必须 fail-loud 出 whitelist_no_reason，得 %v", vs2)
	}
}

func TestFuncDocAllowAndCamelNames(t *testing.T) {
	src := `package p

// GetStudentRanking 名字语义已越线，doc 白名单可豁免但必须写清理由。
//
// Deprecated: 改用等级化接口。
// norank-allow 兼容旧移动端别名的只读门面，函数体内仅转发本人数据接口（PR #61 评审）。
func GetStudentRanking() {}

// EnsureNoRanking 是守卫说明性命名，绝不能误报。
func EnsureNoRanking() {}

func ComputeGlobalLeaderboard(board string) {} // 裸奔的驼峰查询名，应命中 func_name_rank
`
	root := writeTree(t, map[string]string{"go.mod": goModStub, "core/p/names.go": src})
	vs, err := scanGoFile(filepath.Join(root, "core", "p", "names.go"))
	if err != nil {
		t.Fatalf("scanGoFile: %v", err)
	}
	if len(vs) != 1 || vs[0].category != "func_name_rank" || !strings.Contains(vs[0].snippet, "ComputeGlobalLeaderboard") {
		t.Errorf("驼峰补丁判定不符预期，得 %v", vs)
	}
}

func TestReNameRank_ParityWithFrozenImplementationPlusCamelPatch(t *testing.T) {
	hit := []string{
		"get_student_ranking", "compute_rank", "list_leaderboards",
		"GetStudentRanking", "ComputeGlobalLeaderboard",
		"BuildLeaderboard", "fetch_rankings",
	}
	miss := []string{
		// rank_by_subject 与冻结实现的正则行为一致：不命中（其正则只锚定
		// 结尾或动作动词开头，_by 后缀链不覆盖）——保持语义不变，不擅自放宽
		"rank_by_subject",
		"EnsureNoRanking", "GetActiveEstimatorRun", "ListEstimatorRunLineage",
		"InsertResponseEvent", "QueryOwnSessions", "median_practice",
	}
	for _, n := range hit {
		if !reNameRank.MatchString(n) {
			t.Errorf("reNameRank 应命中 %q", n)
		}
	}
	for _, n := range miss {
		if reNameRank.MatchString(n) {
			t.Errorf("reNameRank 不应命中 %q", n)
		}
	}
}

func TestRun_ExitCodes(t *testing.T) {
	good := writeTree(t, goodProjectFiles())
	var out, errBuf bytes.Buffer
	if code := run(&out, &errBuf, good); code != 0 {
		t.Fatalf("好项目应退出 0，得 %d\nstdout=%s stderr=%s", code, out.String(), errBuf.String())
	}
	if !strings.Contains(out.String(), "✅") {
		t.Errorf("成功输出缺少 ✅：%s", out.String())
	}

	bad := writeTree(t, badProjectFiles())
	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, bad); code != 1 {
		t.Fatalf("坏项目应退出 1，得 %d\nstderr=%s", code, errBuf.String())
	}
	if !strings.Contains(errBuf.String(), "违反 D8") || !strings.Contains(errBuf.String(), "norank-allow") {
		t.Errorf("失败输出缺关键信息：%s", errBuf.String())
	}

	// 扫描面缺失（无 db/queries）→ 操作错误 exit 2，绝不静默空转（GO-1 教训）
	amputated := writeTree(t, map[string]string{
		"go.mod":      goModStub,
		"core/a/a.go": "package a\n",
		"api/api.go":  "package api\n",
	})
	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, amputated); code != 2 {
		t.Fatalf("扫描面缺失应退出 2，得 %d stderr=%s", code, errBuf.String())
	}

	// 显式 -root 不是仓库根 → exit 2
	out.Reset()
	errBuf.Reset()
	if code := run(&out, &errBuf, filepath.Join(t.TempDir(), "nowhere")); code != 2 {
		t.Fatalf("非法 -root 应退出 2，得 %d", code)
	}
}

func TestScanRealRepoIsClean(t *testing.T) {
	root, err := resolveRoot("") // 测试工作目录在包目录内，逐级上溯即仓库根
	if err != nil {
		t.Skipf("不在仓库树内运行（%v），跳过实仓冒烟", err)
	}
	vs, serr := scanRoot(root)
	if serr != nil {
		t.Fatalf("实仓扫描失败: %v", serr)
	}
	if len(vs) > 0 {
		t.Errorf("当前仓库 HEAD 应无 D8 违规（新违规则此测试先红）：\n%s", joinViolations(vs))
	}
}

func joinViolations(vs []violation) string {
	var b strings.Builder
	for _, v := range vs {
		b.WriteString(v.String())
		b.WriteString("\n")
	}
	return b.String()
}
