package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/models"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 本套件以 fakeTx 承载入账链路的可本地验证语义（无 Docker/PG 的面）：
// - 接受路径：母题身份/版本 → item → item_version → 证书 → gate_run×N →
//   Publish 三写，语句序与字段形态逐一对表，主事务 COMMIT；
// - 门失败：主事务零语句回滚 + gate_failure 独立事务留痕（铁律 9）；
// - 重放幂等：PK 冲突显形为 already-ingested 拒绝，不产生失败留痕；
// - 公式一：digest 验证器重算 id 与 models.ComputeInstanceID 逐字一致。
// FK 物理拒绝、append-only 触发器、真库行为由迁移 + E2E 承载，本套件不宣称。

// ── 真实记录构造：mathgen 产一条 → JSONL 往返（UseNumber 保数字原文）──────

// testPackDigest / testEngineDigest 是测试装配值（公式一 pd/ed 任意显式值
// 均可证——仓库无学科包摘要真源，这正是 -pack-digest flag 存在的原因）。
const (
	testPackDigest   = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testEngineDigest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func testOptions() options {
	return options{
		packID:        "subject-math",
		packDigest:    testPackDigest,
		engineDigest:  testEngineDigest,
		policyVersion: "1.0",
		issuedBy:      "ingest-test",
		operator:      "ingest-test",
	}
}

// jsonlLine 生成一条真实 mathgen 记录并序列化成 JSONL 行。
func jsonlLine(t *testing.T) []byte {
	t.Helper()
	recs, _, err := subjectmath.Run(subjectmath.Options{TemplateID: "tpl-sm-int-mul-sc", N: 1, Seed: 20260827})
	if err != nil {
		t.Fatalf("subjectmath.Run: %v", err)
	}
	line, err := json.Marshal(recs[0])
	if err != nil {
		t.Fatalf("marshal Record: %v", err)
	}
	return line
}

// tamperedLine 解码一行、注入变异、再序列化（负例载体：真实形态上做单点变异）。
func tamperedLine(t *testing.T, line []byte, mutate func(r *subjectmath.Record)) []byte {
	t.Helper()
	rec, err := decodeRecord(line)
	if err != nil {
		t.Fatalf("decodeRecord: %v", err)
	}
	mutate(rec)
	out, err := json.Marshal(rec)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return out
}

// ── fake 事务/连接面 ─────────────────────────────────────────────────────

type stmt struct {
	sql  string
	args []any
}

// has 子串匹配（sqlc 语句常量带 `-- name:` 头注释行，前缀匹配不可靠）。
func (s stmt) has(fragment string) bool { return strings.Contains(s.sql, fragment) }

// clause 抽取语句首个 SQL 关键字起的单行文本（语句序断言面）。
func clause(sql string) string {
	for _, kw := range []string{"INSERT INTO ", "UPDATE ", "SELECT ", "DELETE FROM "} {
		if i := strings.Index(sql, kw); i >= 0 {
			rest := sql[i:]
			if j := strings.IndexByte(rest, '\n'); j >= 0 {
				rest = rest[:j]
			}
			return strings.TrimSpace(rest)
		}
	}
	return strings.TrimSpace(sql)
}

// scanRow 是 pgx.Row 的最小桩：按位置回填（与 publish_test 同形）。
type scanRow struct {
	vals []any
	err  error
}

func (r *scanRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	if len(dest) != len(r.vals) {
		return fmt.Errorf("fake: scan 目标 %d 列 ≠ 桩 %d 列", len(dest), len(r.vals))
	}
	for i, d := range dest {
		v := r.vals[i]
		switch t := d.(type) {
		case *string:
			*t = v.(string)
		case *[]byte:
			*t = v.([]byte)
		case *pgtype.Text:
			*t = v.(pgtype.Text)
		case *pgtype.Timestamptz:
			*t = v.(pgtype.Timestamptz)
		default:
			rv := reflect.ValueOf(d)
			vv := reflect.ValueOf(v)
			if rv.Kind() == reflect.Ptr && rv.Elem().Kind() == reflect.String &&
				vv.Kind() == reflect.String {
				// 字符串型别名（dbgen 枚举等）直接按字符串值回填。
				rv.Elem().SetString(vv.String())
				continue
			}
			return fmt.Errorf("fake: 不支持的 scan 目标 %T", d)
		}
	}
	return nil
}

// fakeTx 以最小状态机模拟最外层持有的 pgx.Tx：Exec/QueryRow 落 pending，
// Commit 并入 applied，Rollback 丢弃 pending；duplicateSQL 非 nil 时该前缀的
// 下一次 Exec 返回 23505（重放幂等路径的驱动注入点）。
type fakeTx struct {
	mu           sync.Mutex
	pending      []stmt
	rolledBack   bool
	committed    bool
	duplicateSQL string
	versionArgs  []any // 最近一次 INSERT INTO item_version 的参数（取证桩数据源）
	certArgs     []any // 最近一次 INSERT INTO gate_certificate 的参数
	now          time.Time
}

func (f *fakeTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]any, len(args))
	copy(cp, args)
	if f.duplicateSQL != "" && strings.Contains(sql, f.duplicateSQL) {
		f.duplicateSQL = ""
		f.pending = append(f.pending, stmt{sql: sql, args: cp})
		return pgconn.CommandTag{}, &pgconn.PgError{Code: "23505", Message: "duplicate key"}
	}
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	switch {
	case strings.Contains(sql, "INSERT INTO item_version"):
		f.versionArgs = cp
	case strings.Contains(sql, "INSERT INTO gate_certificate"):
		f.certArgs = cp
	}
	return pgconn.CommandTag{}, nil
}

func (f *fakeTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, errors.New("fake: 本套件只走 QueryRow 取证与 Exec 写路径")
}

func (f *fakeTx) QueryRow(_ context.Context, sql string, _ ...any) pgx.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case strings.Contains(sql, "FROM item_version WHERE"):
		// 用「本事务刚写入的 item_version 参数」作取证桩——发布侧重算内容
		// 寻址所读的行就是入账侧写的行，桩与账面天然一致（不做第二事实源）。
		if f.versionArgs == nil {
			return &scanRow{err: errors.New("fake: 无 item_version 写入可取证")}
		}
		return &scanRow{vals: []any{
			f.versionArgs[0], f.versionArgs[1], f.versionArgs[2], f.versionArgs[3],
			f.versionArgs[4], f.versionArgs[5], f.versionArgs[6], f.versionArgs[7],
			f.versionArgs[8],
			[]byte(nil),          // rendered_snapshot（draft 为 NULL，发布语句 COALESCE）
			pgtype.Text{},        // gate_certificate_id
			pgtype.Timestamptz{}, // published_at
			pgtype.Timestamptz{}, // retired_at
			pgtype.Timestamptz{Time: f.now, Valid: true}, // created_at
		}}
	case strings.Contains(sql, "FROM gate_certificate WHERE"):
		if f.certArgs == nil {
			return &scanRow{err: errors.New("fake: 无 gate_certificate 写入可取证")}
		}
		return &scanRow{vals: append(append([]any{}, f.certArgs...),
			pgtype.Timestamptz{Time: f.now, Valid: true})}
	}
	return &scanRow{err: fmt.Errorf("fake: 未桩定的取证语句 %q", sql)}
}

func (f *fakeTx) Commit(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.committed = true
	return nil
}

// Begin 满足 pgx.Tx 的可嵌套形态签名；本套件不测嵌套事务，一律拒绝。
func (f *fakeTx) Begin(context.Context) (pgx.Tx, error) {
	return nil, errors.New("fake: 不支持嵌套事务")
}

// Conn 满足 pgx.Tx 完整接口；本套件不涉连接取回，一律拒绝。
func (f *fakeTx) Conn() *pgx.Conn { return nil }

// CopyFrom 满足 pgx.Tx 完整接口；本套件不涉批量拷贝，一律拒绝。
func (f *fakeTx) CopyFrom(context.Context, pgx.Identifier, []string, pgx.CopyFromSource) (int64, error) {
	return 0, errors.New("fake: 不支持 CopyFrom")
}

// LargeObjects 满足 pgx.Tx 完整接口；本套件不涉大对象，一律拒绝。
func (f *fakeTx) LargeObjects() pgx.LargeObjects {
	return pgx.LargeObjects{}
}

// Prepare 满足 pgx.Tx 完整接口；本套件全走 sqlc 参数化语句，不涉 prepare。
func (f *fakeTx) Prepare(context.Context, string, string) (*pgconn.StatementDescription, error) {
	return nil, errors.New("fake: 不支持 Prepare")
}

// SendBatch 满足 pgx.Tx 完整接口；本套件不涉批量管道，一律拒绝。
func (f *fakeTx) SendBatch(context.Context, *pgx.Batch) pgx.BatchResults {
	return nil
}

func (f *fakeTx) Rollback(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rolledBack = true
	f.pending = nil
	return nil
}

// fakeConnect 按次发 fakeTx（顺序即事务创建序：主事务在前，失败留痕在后）。
type fakeConnect struct {
	txs []*fakeTx
	now time.Time
}

func (c *fakeConnect) Begin(context.Context) (pgx.Tx, error) {
	tx := &fakeTx{now: c.now}
	c.txs = append(c.txs, tx)
	return tx, nil
}

func newTestRunner(t *testing.T, fc *fakeConnect) *Runner {
	t.Helper()
	interactions := contractIDs{"single_choice": "active", "numeric_blank": "active"}
	scorers := contractIDs{"exact_match": "active"}
	rn := NewRunner(testOptions(), fc, interactions, scorers)
	rn.now = func() time.Time { return fc.now }
	rn.newID = func() (string, error) { return "TESTULID000000000000000000", nil }
	return rn
}

// stmtsClauses 抽取事务内语句关键字序列（语句序断言面）。
func stmtsClauses(tx *fakeTx) []string {
	out := make([]string, 0, len(tx.pending))
	for _, s := range tx.pending {
		out = append(out, clause(s.sql))
	}
	return out
}

// ── 接受路径 ─────────────────────────────────────────────────────────────

func TestIngestRecordAcceptPath(t *testing.T) {
	line := jsonlLine(t)
	fc := &fakeConnect{now: time.Unix(1_769_000_000, 0).UTC()}
	rn := newTestRunner(t, fc)

	outcome, reason, err := rn.ingestRecord(context.Background(), line)
	if err != nil {
		t.Fatalf("ingestRecord: %v", err)
	}
	if outcome != outcomeAccepted || reason != "" {
		t.Fatalf("outcome=%s reason=%s，期望 accepted", outcome, reason)
	}
	if len(fc.txs) != 1 {
		t.Fatalf("应只开主事务，实际 %d 个", len(fc.txs))
	}
	tx := fc.txs[0]
	if !tx.committed || tx.rolledBack {
		t.Fatalf("主事务应 COMMIT：committed=%v rolledBack=%v", tx.committed, tx.rolledBack)
	}

	// 语句序：母题身份 → 母题版本 → item → item_version → 证书 → run×2 →
	// 发布三写（状态前移/签发账/指针前移）。
	want := []string{
		"INSERT INTO item_template (",
		"INSERT INTO item_template_version (",
		"INSERT INTO item (",
		"INSERT INTO item_version (",
		"INSERT INTO gate_certificate (",
		"INSERT INTO gate_run (",
		"INSERT INTO gate_run (",
		"UPDATE item_version SET",
		"INSERT INTO publication (",
		"UPDATE item SET current_version_id",
	}
	got := stmtsClauses(tx)
	if len(got) != len(want) {
		t.Fatalf("语句数 %d ≠ %d:\n%s", len(got), len(want), strings.Join(got, "\n"))
	}
	for i := range want {
		if !strings.Contains(got[i], want[i]) {
			t.Fatalf("语句序[%d] = %q，期望含 %q", i, got[i], want[i])
		}
	}

	// item：A/B 级 item_id = item_version_id 自引用；tier A；版本外键就位。
	ivid := tx.pending[3].args[0].(string)
	itemStmt := tx.pending[2]
	if itemStmt.args[0] != ivid {
		t.Fatalf("item_id %v ≠ item_version_id %v（自引用破坏）", itemStmt.args[0], ivid)
	}
	if fmt.Sprint(itemStmt.args[2]) != "A" { // dbgen.ItemTierEnum
		t.Fatalf("tier = %v，期望 A", itemStmt.args[2])
	}
	tvd, tvdOK := itemStmt.args[3].(pgtype.Text)
	if !tvdOK || !tvd.Valid || tvd.String != tx.pending[1].args[0] {
		t.Fatalf("item.template_version_id %v 与母题版本行 %v 脱钩",
			itemStmt.args[3], tx.pending[1].args[0])
	}
	// item_version：draft 状态 + 六块 + lineage。
	ivStmt := tx.pending[3]
	if fmt.Sprint(ivStmt.args[2]) != "draft" { // dbgen.ItemVersionStatusEnum
		t.Fatalf("status = %v，期望 draft", ivStmt.args[2])
	}
	for i, name := range []string{"objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage"} {
		blob, ok := ivStmt.args[3+i].([]byte)
		if !ok || len(blob) == 0 {
			t.Fatalf("%s 列缺失或非 JSONB 字节", name)
		}
	}
	var lineage map[string]any
	dec := json.NewDecoder(strings.NewReader(string(ivStmt.args[8].([]byte))))
	dec.UseNumber()
	if err := dec.Decode(&lineage); err != nil {
		t.Fatalf("lineage 不是合法 JSON: %v", err)
	}
	for _, key := range []string{"template_id", "source", "operator", "pack_id",
		"corpus_version_id", "pack_digest", "engine_digest", "content_digest"} {
		if _, ok := lineage[key]; !ok {
			t.Fatalf("lineage 缺审计卡点名键 %q", key)
		}
	}
	if lineage["template_id"] != "tpl-sm-int-mul-sc" || lineage["pack_id"] != "subject-math" {
		t.Fatalf("lineage 键值异常: template_id=%v pack_id=%v", lineage["template_id"], lineage["pack_id"])
	}
	if _, ok := lineage["seed"]; !ok {
		t.Fatalf("lineage 丢失生成侧 seed（回放证据）")
	}

	// 证书与 gate_run：publish 用途绑定本版本；两验证器 pass、置信 1.000。
	certStmt := tx.pending[4]
	if certStmt.args[1] != ivid || certStmt.args[2] != "publish" {
		t.Fatalf("证书绑定异常: artifact_ref=%v cert_type=%v", certStmt.args[1], certStmt.args[2])
	}
	if !strings.HasPrefix(certStmt.args[0].(string), "cert_") {
		t.Fatalf("cert_id 形态违反冻结惯例: %v", certStmt.args[0])
	}
	seenValidators := map[string]bool{}
	for _, runIdx := range []int{5, 6} {
		runStmt := tx.pending[runIdx]
		vid := runStmt.args[3].(string)
		seenValidators[vid] = true
		if fmt.Sprint(runStmt.args[5]) != "pass" { // dbgen.GateRunVerdictEnum
			t.Fatalf("gate_run %s verdict = %v", vid, runStmt.args[5])
		}
		conf := runStmt.args[7].(pgtype.Numeric)
		if !conf.Valid || conf.Int.Cmp(confidenceCertain().Int) != 0 || conf.Exp != -3 {
			t.Fatalf("gate_run %s confidence = %+v，期望 1.000", vid, conf)
		}
		if runStmt.args[9] != int32(0) {
			t.Fatalf("gate_run %s cost_tokens = %v，确定性路径应为 0", vid, runStmt.args[9])
		}
		if !strings.HasPrefix(runStmt.args[0].(string), "run_") {
			t.Fatalf("run_id 形态违反冻结惯例: %v", runStmt.args[0])
		}
	}
	if !seenValidators["digest"] || !seenValidators["registry"] {
		t.Fatalf("gate_run 未覆盖 digest/registry 两类验证器: %v", seenValidators)
	}
}

// ── 门失败路径：主事务回滚 + 独立事务留痕 ─────────────────────────────────

func TestIngestRecordGateFailureLeavesTrail(t *testing.T) {
	line := tamperedLine(t, jsonlLine(t), func(r *subjectmath.Record) {
		r.ContentDigest = "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
	})
	fc := &fakeConnect{now: time.Unix(1_769_000_000, 0).UTC()}
	rn := newTestRunner(t, fc)

	outcome, reason, err := rn.ingestRecord(context.Background(), line)
	if err != nil {
		t.Fatalf("ingestRecord: %v", err)
	}
	if outcome != outcomeRejected || reason != "digest:fail" {
		t.Fatalf("outcome=%s reason=%s，期望 rejected/digest:fail", outcome, reason)
	}
	if len(fc.txs) != 2 {
		t.Fatalf("应有主事务 + 留痕事务，实际 %d 个", len(fc.txs))
	}
	main, trail := fc.txs[0], fc.txs[1]
	if !main.rolledBack || len(main.pending) != 0 {
		t.Fatalf("主事务应零语句回滚: rolledBack=%v pending=%d", main.rolledBack, len(main.pending))
	}
	if !trail.committed {
		t.Fatalf("留痕事务应 COMMIT")
	}
	failures := 0
	validatorID := ""
	for _, s := range trail.pending {
		if s.has("INSERT INTO gate_failure") {
			failures++
			validatorID = s.args[3].(string)
		}
	}
	if failures != 1 || validatorID != "digest" {
		t.Fatalf("留痕应 1 条 digest 失败: got %d 条 validator=%s", failures, validatorID)
	}
}

func TestIngestRecordUnregisteredInteractionRejected(t *testing.T) {
	line := tamperedLine(t, jsonlLine(t), func(r *subjectmath.Record) {
		r.InteractionRef["interaction_id"] = "private_flash_card" // 私造交互类型
	})
	fc := &fakeConnect{now: time.Unix(1_769_000_000, 0).UTC()}
	rn := newTestRunner(t, fc)

	outcome, reason, err := rn.ingestRecord(context.Background(), line)
	if err != nil {
		t.Fatalf("ingestRecord: %v", err)
	}
	if outcome != outcomeRejected || reason != "registry:fail" {
		t.Fatalf("outcome=%s reason=%s，期望 rejected/registry:fail", outcome, reason)
	}
	trail := fc.txs[1]
	found := false
	for _, s := range trail.pending {
		if s.has("INSERT INTO gate_failure") && s.args[3] == "registry" {
			found = true
		}
	}
	if !found {
		t.Fatalf("registry 失败未留痕")
	}
}

// ── 重放幂等：PK 冲突 = already-ingested，不走失败留痕 ────────────────────

func TestIngestRecordDuplicateIsIdempotentReject(t *testing.T) {
	fc := &fakeConnect{now: time.Unix(1_769_000_000, 0).UTC()}
	rn := newTestRunner(t, fc)
	// 注入点在母题版本行之后第一处账行：INSERT INTO item（前两语句是
	// ON CONFLICT DO NOTHING 的指针表 upsert，重放天然无冲突）。
	rn.begin = func(ctx context.Context) (pgx.Tx, error) { // 拦截主事务注入 23505
		tx, err := fc.Begin(ctx)
		if err != nil {
			return nil, err
		}
		tx.(*fakeTx).duplicateSQL = "INSERT INTO item ("
		return tx, nil
	}

	outcome, reason, err := rn.ingestRecord(context.Background(), jsonlLine(t))
	if err != nil {
		t.Fatalf("ingestRecord: %v", err)
	}
	if outcome != outcomeRejected || reason != "already-ingested" {
		t.Fatalf("outcome=%s reason=%s，期望 rejected/already-ingested", outcome, reason)
	}
	if len(fc.txs) != 1 || !fc.txs[0].rolledBack {
		t.Fatalf("重放路径不应开留痕事务，主事务应回滚")
	}
}

// ── 解码与纯函数 ─────────────────────────────────────────────────────────

func TestDecodeRecordFailClosed(t *testing.T) {
	if _, err := decodeRecord([]byte(`{not-json`)); err == nil {
		t.Fatalf("坏 JSON 应拒绝")
	}
	line := tamperedLine(t, jsonlLine(t), func(r *subjectmath.Record) { r.Locale = "" })
	if _, err := decodeRecord(line); err == nil {
		t.Fatalf("缺 locale 应拒绝（公式一 l 输入必填）")
	}
	line = tamperedLine(t, jsonlLine(t), func(r *subjectmath.Record) { r.ContentDigest = "" })
	if _, err := decodeRecord(line); err == nil {
		t.Fatalf("缺 content_digest 应拒绝")
	}
}

// TestJSONLNumberPrecision 数字原文保真：JSONL 往返后数值是 json.Number
// （原文进哈希与公式一），不得漂移成 float64。probe 取谱系 seed（int64 注入）
// 与全记录扫描双保险。
func TestJSONLNumberPrecision(t *testing.T) {
	rec, err := decodeRecord(jsonlLine(t))
	if err != nil {
		t.Fatalf("decodeRecord: %v", err)
	}
	seed, ok := rec.Lineage["seed"].(json.Number)
	if !ok || seed.String() != "20260827" {
		t.Fatalf("seed 应为 json.Number 原文 20260827，得 %T %v", rec.Lineage["seed"], rec.Lineage["seed"])
	}
	var walk func(v any)
	walk = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			for _, e := range x {
				walk(e)
			}
		case []any:
			for _, e := range x {
				walk(e)
			}
		case float64:
			t.Fatalf("记录出现 float64 %v（精度口径漂移）", x)
		}
	}
	walk(rec.Instance)
}

// TestDigestCheckFormulaOneMatchesModels digest 验证器第③段与
// core/models.ComputeInstanceID（冻结公式一 Go 唯一实现）逐字一致。
func TestDigestCheckFormulaOneMatchesModels(t *testing.T) {
	rec, err := decodeRecord(jsonlLine(t))
	if err != nil {
		t.Fatalf("decodeRecord: %v", err)
	}
	lineage := buildLineage(rec, testOptions())
	got := digestCheck(rec, lineage, testOptions())
	if got.Verdict != "pass" {
		t.Fatalf("digest 验证器应 pass，得 %v（%v）", got.Verdict, got.Evidence["fail_reason"])
	}
	want, err := models.ComputeInstanceID(
		rec.TemplateVersionID, normalizedParams(lineage),
		testPackDigest, testEngineDigest, nil, rec.Locale)
	if err != nil {
		t.Fatalf("ComputeInstanceID: %v", err)
	}
	if got.ItemVersionID != want {
		t.Fatalf("公式一 id 漂移:\n got  %s\n want %s", got.ItemVersionID, want)
	}
	if got.Evidence["content_digest_recomputed"] != rec.ContentDigest {
		t.Fatalf("content 摘要重算与声明不一致: %v", got.Evidence)
	}
}

func TestBuildLineageCarriesAuditKeys(t *testing.T) {
	rec, err := decodeRecord(jsonlLine(t))
	if err != nil {
		t.Fatalf("decodeRecord: %v", err)
	}
	lineage := buildLineage(rec, testOptions())
	if lineage["source"] != "subjectmath-mathgen" {
		t.Fatalf("source 应取谱系生产线 id，得 %v", lineage["source"])
	}
	if lineage["corpus_version_id"] != nil {
		t.Fatalf("无数料引用时 corpus_version_id 应为 null，得 %v", lineage["corpus_version_id"])
	}
	if lineage["operator"] != "ingest-test" || lineage["pack_digest"] != testPackDigest {
		t.Fatalf("operator/pack_digest 补全异常: %v %v", lineage["operator"], lineage["pack_digest"])
	}
}

func TestConfidenceCertainIsOneDotZeroZero(t *testing.T) {
	conf := confidenceCertain()
	if !conf.Valid || conf.Exp != -3 || conf.Int == nil || conf.Int.Int64() != 1000 {
		t.Fatalf("置信度应为 NUMERIC 1.000，得 %+v", conf)
	}
}
