package content

// GO-RW-001 内容只读查询服务的本地可验证语义（无 Docker/PG，PG 运行时行为
// 不在此宣称覆盖，FK/触发器由迁移 + CI 真库验证）：
//   - 四查询的正例投影：行 → 契约视图的字段折叠（可空列 NULL → null、
//     NUMERIC → 十进制字符串、空 runs/verdicts → [] 而非 null）；
//   - 无行 → 各实体哨兵（errors.Is 可判，api 层据此映射 404）；
//   - 指针悬空 → ErrDanglingCurrentVersion（冻结实现静默 null 的反模式在此红）；
//   - 驱动故障原样放行（≠ 业务哨兵，api 层据此外映射 500，不许混报）；
//   - 未装配执行面 → ErrNoExecutor（fail-closed）。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// errLookupFailed / errListFailed 驱动故障替身：与全部业务哨兵异源.
var (
	errLookupFailed = errors.New("fakeq: 取证驱动故障")
	errListFailed   = errors.New("fakeq: 列表驱动故障")
)

// ── fake 执行面：按 SQL 形态路由的行桩 ─────────────────────────────────────

// qrow 单行/单列表桩：vals 与生成层 Scan 目标列序一一对应.
type qrow struct {
	vals []any
}

func (r qrow) Scan(dest ...any) error {
	if len(dest) != len(r.vals) {
		return errors.New("fakeq: scan 列数不符")
	}
	for i, d := range dest {
		if err := scanInto(d, r.vals[i]); err != nil {
			return err
		}
	}
	return nil
}

// scanInto 把桩值落入生成层的 Scan 目标；不支持的目标类型显式报错（列序
// 漂移第一时间红，绝不静默零值）.
func scanInto(d any, v any) error {
	switch p := d.(type) {
	case *string:
		s, ok := v.(string)
		if !ok {
			return typeErr(v)
		}
		*p = s
	case *[]byte:
		b, ok := v.([]byte)
		if !ok {
			return typeErr(v)
		}
		*p = b
	case *int32:
		n, ok := v.(int32)
		if !ok {
			return typeErr(v)
		}
		*p = n
	case *int64:
		n, ok := v.(int64)
		if !ok {
			return typeErr(v)
		}
		*p = n
	case *pgtype.Text:
		t, ok := v.(pgtype.Text)
		if !ok {
			return typeErr(v)
		}
		*p = t
	case *pgtype.Timestamptz:
		t, ok := v.(pgtype.Timestamptz)
		if !ok {
			return typeErr(v)
		}
		*p = t
	case *pgtype.Numeric:
		n, ok := v.(pgtype.Numeric)
		if !ok {
			return typeErr(v)
		}
		*p = n
	case *dbgen.ItemTierEnum:
		e, ok := v.(dbgen.ItemTierEnum)
		if !ok {
			return typeErr(v)
		}
		*p = e
	case *dbgen.ItemVersionStatusEnum:
		e, ok := v.(dbgen.ItemVersionStatusEnum)
		if !ok {
			return typeErr(v)
		}
		*p = e
	case *dbgen.ItemTemplateVersionStatusEnum:
		e, ok := v.(dbgen.ItemTemplateVersionStatusEnum)
		if !ok {
			return typeErr(v)
		}
		*p = e
	case *dbgen.GateRunVerdictEnum:
		e, ok := v.(dbgen.GateRunVerdictEnum)
		if !ok {
			return typeErr(v)
		}
		*p = e
	default:
		return errors.New("fakeq: 不支持的 scan 目标")
	}
	return nil
}

func typeErr(v any) error {
	return fmt.Errorf("fakeq: scan 目标与桩值类型不符: %T", v)
}

// qrows :many 桩：生成层只走 Next/Scan/Err/Close，其余方法不应触达.
type qrows struct {
	pgx.Rows // 嵌入接口兜编译形状；未实现的路径 panic 即"被误用"的信号
	rows     []qrow
	i        int
}

func (r *qrows) Next() bool { r.i++; return r.i <= len(r.rows) }

func (r *qrows) Scan(dest ...any) error { return r.rows[r.i-1].Scan(dest...) }

func (r *qrows) Err() error { return nil }

func (r *qrows) Close() {}

// fakeQueryDB 最小只读执行面：QueryRow/Query 按语句形态路由到行桩；
// failLookup/failList 注入驱动故障。Exec 不应被只读面触达.
type fakeQueryDB struct {
	item            *dbgen.Item
	itemVersion     *dbgen.ItemVersion
	template        *dbgen.ItemTemplate
	templateVersion *dbgen.ItemTemplateVersion
	cert            *dbgen.GateCertificate
	runs            []dbgen.GateRun
	verdicts        []dbgen.GateVerdict

	failLookup bool // QueryRow 一律故障
	failList   bool // Query 一律故障
}

var _ Executor = (*fakeQueryDB)(nil)

func (f *fakeQueryDB) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	panic("content fakeq: 只读查询服务不应发出 Exec")
}

func (f *fakeQueryDB) Query(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
	if f.failList {
		return nil, errListFailed
	}
	switch {
	case strings.Contains(sql, "FROM gate_run WHERE"):
		certRef, ok := args[0].(pgtype.Text)
		if !ok {
			return nil, errors.New("fakeq: gate_run 参数形态漂移")
		}
		var out []qrow
		for _, r := range f.runs {
			if r.CertificateID.String == certRef.String {
				out = append(out, qrow{vals: runVals(r)})
			}
		}
		// 桩内模拟语句契约的 ORDER BY run_at ASC, run_id ASC（排序是读面承诺的一部分）.
		sort.SliceStable(out, func(i, j int) bool {
			a, b := f.runByID(out[i].vals[0].(string)), f.runByID(out[j].vals[0].(string))
			if !a.RunAt.Time.Equal(b.RunAt.Time) {
				return a.RunAt.Time.Before(b.RunAt.Time)
			}
			return a.RunID < b.RunID
		})
		return &qrows{rows: out}, nil
	case strings.Contains(sql, "FROM gate_verdict"):
		certRef, ok := args[0].(pgtype.Text)
		if !ok {
			return nil, errors.New("fakeq: gate_verdict 参数形态漂移")
		}
		allowed := map[string]bool{}
		for _, r := range f.runs {
			if r.CertificateID.String == certRef.String {
				allowed[r.RunID] = true
			}
		}
		var out []qrow
		for _, v := range f.verdicts {
			if allowed[v.RunID] {
				out = append(out, qrow{vals: verdictVals(v)})
			}
		}
		// 同上：ORDER BY verdict_id ASC.
		sort.SliceStable(out, func(i, j int) bool {
			return f.verdictByID(out[i].vals[0].(int64)).VerdictID <
				f.verdictByID(out[j].vals[0].(int64)).VerdictID
		})
		return &qrows{rows: out}, nil
	}
	return nil, errors.New("fakeq: 未桩定的列表语句 " + sql)
}

// runByID / verdictByID 按主键反查桩值（排序键读取用；桩只由本 fake 构造）.
func (f *fakeQueryDB) runByID(id string) dbgen.GateRun {
	for _, g := range f.runs {
		if g.RunID == id {
			return g
		}
	}
	return dbgen.GateRun{}
}

func (f *fakeQueryDB) verdictByID(id int64) dbgen.GateVerdict {
	for _, v := range f.verdicts {
		if v.VerdictID == id {
			return v
		}
	}
	return dbgen.GateVerdict{}
}

func (f *fakeQueryDB) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	if f.failLookup {
		return errRow{err: errLookupFailed}
	}
	id, _ := args[0].(string)
	switch {
	// 匹配序按最长表名优先：FROM item 是 FROM item_version 的前缀子串.
	case strings.Contains(sql, "FROM item_template_version WHERE"):
		return rowByID(f.templateVersion, id,
			func(r dbgen.ItemTemplateVersion) string { return r.TemplateVersionID }, templateVersionVals)
	case strings.Contains(sql, "FROM item_template WHERE"):
		return rowByID(f.template, id,
			func(r dbgen.ItemTemplate) string { return r.TemplateID }, templateVals)
	case strings.Contains(sql, "FROM item_version WHERE"):
		return rowByID(f.itemVersion, id,
			func(r dbgen.ItemVersion) string { return r.ItemVersionID }, itemVersionVals)
	case strings.Contains(sql, "FROM gate_certificate WHERE"):
		return rowByID(f.cert, id,
			func(r dbgen.GateCertificate) string { return r.CertID }, certVals)
	case strings.Contains(sql, "FROM item WHERE"):
		return rowByID(f.item, id,
			func(r dbgen.Item) string { return r.ItemID }, itemVals)
	}
	return errRow{err: errors.New("fakeq: 未桩定的取证语句 " + sql)}
}

// rowByID 行桩：行存在且 id 命中才返回值行，否则 ErrNoRows——fake 必须对
// "查别的 id"给出真实的无行语义，not-found 分支的断言才可信.
func rowByID[T any](row *T, id string, key func(T) string, vals func(T) []any) pgx.Row {
	if row == nil || key(*row) != id {
		return errRow{err: pgx.ErrNoRows}
	}
	return qrow{vals: vals(*row)}
}

// errRow 恒错行（驱动故障 / ErrNoRows 共用载体）.
type errRow struct{ err error }

func (r errRow) Scan(...any) error { return r.err }

// ── 列序投影（与 db/gen 生成物的 Scan 序一一对应）────────────────────────

func itemVals(r dbgen.Item) []any {
	return []any{r.ItemID, r.PackID, r.Tier, r.TemplateVersionID, r.CurrentVersionID, r.CreatedAt}
}

func itemVersionVals(r dbgen.ItemVersion) []any {
	return []any{r.ItemVersionID, r.ItemID, r.Status, r.Objective, r.InteractionRef,
		r.Content, r.ScoringRef, r.ErrorBindings, r.Lineage, r.RenderedSnapshot,
		r.GateCertificateID, r.PublishedAt, r.RetiredAt, r.CreatedAt}
}

func certVals(r dbgen.GateCertificate) []any {
	return []any{r.CertID, r.ArtifactRef, r.CertType, r.PolicyVersion, r.IssuedBy,
		r.IssuedAt, r.CreatedAt}
}

func templateVals(r dbgen.ItemTemplate) []any {
	return []any{r.TemplateID, r.PackID, r.CurrentVersionID, r.CreatedAt}
}

func templateVersionVals(r dbgen.ItemTemplateVersion) []any {
	return []any{r.TemplateVersionID, r.TemplateID, r.DslVersion, r.Spec, r.Status, r.CreatedAt}
}

func runVals(r dbgen.GateRun) []any {
	return []any{r.RunID, r.CertificateID, r.PolicyVersion, r.ValidatorID, r.ValidatorVersion,
		r.Verdict, r.Evidence, r.Confidence, r.CostMs, r.CostTokens, r.RunAt, r.CreatedAt}
}

func verdictVals(v dbgen.GateVerdict) []any {
	return []any{v.VerdictID, v.RunID, v.Detail, v.CreatedAt}
}

// ── 夹具值 ───────────────────────────────────────────────────────────────

var (
	fqTime  = time.Date(2026, 8, 29, 8, 0, 0, 0, time.UTC)
	fqTime2 = time.Date(2026, 8, 29, 9, 30, 0, 0, time.UTC)
)

func fqText(s string) pgtype.Text { return pgtype.Text{String: s, Valid: true} }

func fqTS(t time.Time) pgtype.Timestamptz { return pgtype.Timestamptz{Time: t, Valid: true} }

func fqNum(text string) pgtype.Numeric {
	var n pgtype.Numeric
	if err := n.ScanScientific(text); err != nil {
		panic("fakeq: 测试数值构造失败 " + text)
	}
	return n
}

func mustJSON(t *testing.T, v string) []byte {
	t.Helper()
	if !json.Valid([]byte(v)) {
		t.Fatalf("夹具 JSON 非法: %s", v)
	}
	return []byte(v)
}

// ── 用例 ─────────────────────────────────────────────────────────────────

func TestGetItem_FoundWithCurrentVersion(t *testing.T) {
	db := &fakeQueryDB{
		item: &dbgen.Item{
			ItemID: "item_1", PackID: "pack_math", Tier: dbgen.ItemTierEnumB,
			TemplateVersionID: fqText("tvd_1"), CurrentVersionID: fqText("iv_1"), CreatedAt: fqTS(fqTime),
		},
		itemVersion: &dbgen.ItemVersion{
			ItemVersionID: "iv_1", ItemID: "item_1", Status: dbgen.ItemVersionStatusEnumPublished,
			Objective: mustJSON(t, `{"o":1}`), InteractionRef: mustJSON(t, `{"ir":1}`),
			Content: mustJSON(t, `{"c":1}`), ScoringRef: mustJSON(t, `{"sr":1}`),
			ErrorBindings: mustJSON(t, `{"eb":1}`), Lineage: mustJSON(t, `{"tier":"B"}`),
			RenderedSnapshot: mustJSON(t, `{"html":"<b/>"}`), GateCertificateID: fqText("cert_1"),
			PublishedAt: fqTS(fqTime2), CreatedAt: fqTS(fqTime),
		},
	}
	svc := NewContentQueryService(db)
	got, err := svc.GetItem(context.Background(), "item_1")
	if err != nil {
		t.Fatalf("GetItem: %v", err)
	}
	if got.ItemID != "item_1" || got.PackID != "pack_math" || got.Tier != "B" {
		t.Fatalf("身份字段折叠错误: %+v", got)
	}
	if got.TemplateVersionID == nil || *got.TemplateVersionID != "tvd_1" {
		t.Fatalf("template_version_id 应解出 tvd_1: %v", got.TemplateVersionID)
	}
	if got.CreatedAt == nil || !got.CreatedAt.Equal(fqTime) {
		t.Fatalf("created_at 折叠错误: %v", got.CreatedAt)
	}
	cv := got.CurrentVersion
	if cv == nil || cv.ItemVersionID != "iv_1" || cv.Status != "published" {
		t.Fatalf("current_version 应解引用 iv_1/published: %+v", cv)
	}
	if cv.GateCertificateID == nil || *cv.GateCertificateID != "cert_1" {
		t.Fatalf("gate_certificate_id 折叠错误: %v", cv.GateCertificateID)
	}
	if !json.Valid(cv.Objective) || !json.Valid(cv.Lineage) || !json.Valid(cv.RenderedSnapshot) {
		t.Fatalf("六块/谱系/快照必须以原文 JSON 透传")
	}
}

func TestGetItem_WithoutPointer_ViewSerializesContractShape(t *testing.T) {
	db := &fakeQueryDB{
		item: &dbgen.Item{ItemID: "item_2", PackID: "pack_c", Tier: dbgen.ItemTierEnumC, CreatedAt: fqTS(fqTime)},
	}
	got, err := NewContentQueryService(db).GetItem(context.Background(), "item_2")
	if err != nil {
		t.Fatalf("GetItem: %v", err)
	}
	if got.CurrentVersion != nil || got.CurrentVersionID != nil || got.TemplateVersionID != nil {
		t.Fatalf("无指针字段应为 nil: %+v", got)
	}
	raw, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("序列化: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("反序列化: %v", err)
	}
	// 契约 additionalProperties:false：键面恰好七个，缺一多一即漂移.
	wantKeys := []string{"item_id", "pack_id", "tier", "template_version_id",
		"current_version_id", "created_at", "current_version"}
	if len(m) != len(wantKeys) {
		t.Fatalf("键数 = %d, want %d: %v", len(m), len(wantKeys), m)
	}
	for _, k := range wantKeys {
		if _, ok := m[k]; !ok {
			t.Fatalf("缺契约键 %q", k)
		}
	}
	if string(m["current_version"]) != "null" || string(m["current_version_id"]) != "null" {
		t.Fatalf("无指针字段必须显式 null（契约 Optional 语义）: %v", m)
	}
}

func TestGetItem_UnknownAndDangling(t *testing.T) {
	db := &fakeQueryDB{itemVersion: &dbgen.ItemVersion{ItemVersionID: "iv_x", ItemID: "item_9"}}
	svc := NewContentQueryService(db)

	if _, err := svc.GetItem(context.Background(), "item_missing"); !errors.Is(err, ErrUnknownItem) {
		t.Fatalf("无行应 ErrUnknownItem, got %v", err)
	}
	// 指针悬空：item 在而 current_version 无行——账面残缺 fail-loud（冻结版静默 null 的破坏点）.
	db.item = &dbgen.Item{ItemID: "item_9", PackID: "p", Tier: dbgen.ItemTierEnumC, CurrentVersionID: fqText("iv_missing")}
	if _, err := svc.GetItem(context.Background(), "item_9"); !errors.Is(err, ErrDanglingCurrentVersion) {
		t.Fatalf("指针悬空应 ErrDanglingCurrentVersion, got %v", err)
	}
}

func TestGetItemVersion_FoundUnknownAndRawPassThrough(t *testing.T) {
	db := &fakeQueryDB{
		itemVersion: &dbgen.ItemVersion{
			ItemVersionID: "iv_1", ItemID: "item_1", Status: dbgen.ItemVersionStatusEnumQuarantined,
			Objective: mustJSON(t, `{"kp":[]}`), InteractionRef: mustJSON(t, `{"id":"single_choice"}`),
			Content: mustJSON(t, `{"blocks":[]}`), ScoringRef: mustJSON(t, `{"scorer":"exact_match"}`),
			ErrorBindings: mustJSON(t, `{}`), Lineage: mustJSON(t, `{"tier":"C"}`),
			// quarantined 无渲染快照/证书/发布时刻：全可空列走 NULL.
			CreatedAt: fqTS(fqTime),
		},
	}
	svc := NewContentQueryService(db)
	got, err := svc.GetItemVersion(context.Background(), "iv_1")
	if err != nil {
		t.Fatalf("GetItemVersion: %v", err)
	}
	if got.RenderedSnapshot != nil || got.GateCertificateID != nil || got.PublishedAt != nil {
		t.Fatalf("NULL 列应为 nil 视图字段: %+v", got)
	}
	raw, _ := json.Marshal(got)
	if !strings.Contains(string(raw), `"rendered_snapshot":null`) {
		t.Fatalf("rendered_snapshot 必须序列化为 null: %s", raw)
	}
	if _, err := svc.GetItemVersion(context.Background(), "iv_missing"); !errors.Is(err, ErrUnknownItemVersion) {
		t.Fatalf("无行应 ErrUnknownItemVersion, got %v", err)
	}
}

func TestGetTemplate_FoundUnknownAndDangling(t *testing.T) {
	db := &fakeQueryDB{
		template: &dbgen.ItemTemplate{
			TemplateID: "tpl_1", PackID: "pack_e", CurrentVersionID: fqText("tv_1"), CreatedAt: fqTS(fqTime),
		},
		templateVersion: &dbgen.ItemTemplateVersion{
			TemplateVersionID: "tv_1", TemplateID: "tpl_1", DslVersion: "dsl-6",
			Spec: mustJSON(t, `{"blocks":[]}`), Status: dbgen.ItemTemplateVersionStatusEnumPublished,
			CreatedAt: fqTS(fqTime),
		},
	}
	svc := NewContentQueryService(db)
	got, err := svc.GetTemplate(context.Background(), "tpl_1")
	if err != nil {
		t.Fatalf("GetTemplate: %v", err)
	}
	cv := got.CurrentVersion
	if cv == nil || cv.TemplateVersionID != "tv_1" || cv.DslVersion != "dsl-6" || cv.Status != "published" {
		t.Fatalf("母题当前版本折叠错误: %+v", cv)
	}
	if !json.Valid(cv.Spec) {
		t.Fatalf("spec 必须原文透传")
	}
	if _, err := svc.GetTemplate(context.Background(), "tpl_missing"); !errors.Is(err, ErrUnknownTemplate) {
		t.Fatalf("无行应 ErrUnknownTemplate, got %v", err)
	}
	db.template.CurrentVersionID = fqText("tv_missing")
	if _, err := svc.GetTemplate(context.Background(), "tpl_1"); !errors.Is(err, ErrDanglingCurrentVersion) {
		t.Fatalf("母题指针悬空应 ErrDanglingCurrentVersion, got %v", err)
	}
}

func TestGetGateCertificate_WithRunsAndVerdicts(t *testing.T) {
	db := &fakeQueryDB{
		cert: &dbgen.GateCertificate{
			CertID: "cert_1", ArtifactRef: "iv_1", CertType: "publish", PolicyVersion: "pv-2026.1",
			IssuedBy: "workbench", IssuedAt: fqTS(fqTime), CreatedAt: fqTS(fqTime),
		},
		runs: []dbgen.GateRun{
			{
				RunID: "run_2", CertificateID: fqText("cert_1"), PolicyVersion: "pv-2026.1",
				ValidatorID: "v-schema", ValidatorVersion: "1.0", Verdict: dbgen.GateRunVerdictEnumPass,
				Evidence: mustJSON(t, `{"checked":6}`), Confidence: fqNum("0.999"),
				CostMs: 12, CostTokens: 0, RunAt: fqTS(fqTime2), CreatedAt: fqTS(fqTime2),
			},
			{
				RunID: "run_1", CertificateID: fqText("cert_1"), PolicyVersion: "pv-2026.1",
				ValidatorID: "v-digest", ValidatorVersion: "1.1", Verdict: dbgen.GateRunVerdictEnumFail,
				Evidence: mustJSON(t, `{"checked":3}`), Confidence: fqNum("0.5"),
				CostMs: 5, CostTokens: 7, RunAt: fqTS(fqTime), CreatedAt: fqTS(fqTime),
			},
		},
		verdicts: []dbgen.GateVerdict{
			{VerdictID: 3, RunID: "run_2", Detail: mustJSON(t, `{"d":3}`), CreatedAt: fqTS(fqTime2)},
			{VerdictID: 1, RunID: "run_1", Detail: mustJSON(t, `{"d":1}`), CreatedAt: fqTS(fqTime)},
			{VerdictID: 2, RunID: "run_1", Detail: mustJSON(t, `{"d":2}`), CreatedAt: fqTS(fqTime)},
		},
	}
	svc := NewContentQueryService(db)
	got, err := svc.GetGateCertificate(context.Background(), "cert_1")
	if err != nil {
		t.Fatalf("GetGateCertificate: %v", err)
	}
	if got.CertID != "cert_1" || got.CertType != "publish" || got.ArtifactRef != "iv_1" {
		t.Fatalf("证书字段折叠错误: %+v", got)
	}
	if len(got.Runs) != 2 {
		t.Fatalf("runs 应 2 条, got %d", len(got.Runs))
	}
	// run_at 升序：run_1 在前；判定明细按 run_id 归组且 verdict_id 升序.
	first := got.Runs[0]
	if first.RunID != "run_1" || first.Verdict != "fail" || first.Confidence != "0.5" {
		t.Fatalf("run_1 折叠错误: %+v", first)
	}
	if first.CostMs != 5 || first.CostTokens != 7 {
		t.Fatalf("cost 折叠错误: %+v", first)
	}
	if len(first.Verdicts) != 2 || first.Verdicts[0].VerdictID != 1 || first.Verdicts[1].VerdictID != 2 {
		t.Fatalf("run_1 判定归组/排序错误: %+v", first.Verdicts)
	}
	second := got.Runs[1]
	if second.RunID != "run_2" || second.Confidence != "0.999" || len(second.Verdicts) != 1 {
		t.Fatalf("run_2 折叠错误: %+v", second)
	}
	// NUMERIC 以十进制字符串过线（浮点口径漂移禁区）.
	raw, _ := json.Marshal(got)
	if !strings.Contains(string(raw), `"confidence":"0.999"`) {
		t.Fatalf("confidence 必须是字符串形态: %s", raw)
	}
}

func TestGetGateCertificate_NoRunsEmptyArrayAndUnknown(t *testing.T) {
	db := &fakeQueryDB{
		cert: &dbgen.GateCertificate{
			CertID: "cert_empty", ArtifactRef: "cv_9", CertType: "retire",
			PolicyVersion: "pv-2026.1", IssuedBy: "ops", IssuedAt: fqTS(fqTime), CreatedAt: fqTS(fqTime),
		},
	}
	svc := NewContentQueryService(db)
	got, err := svc.GetGateCertificate(context.Background(), "cert_empty")
	if err != nil {
		t.Fatalf("GetGateCertificate: %v", err)
	}
	if got.Runs == nil || len(got.Runs) != 0 {
		t.Fatalf("无 runs 应为空切片（序列化 [] 而非 null）: %v", got.Runs)
	}
	raw, _ := json.Marshal(got)
	if !strings.Contains(string(raw), `"runs":[]`) {
		t.Fatalf("空 runs 必须序列化 []: %s", raw)
	}
	if _, err := svc.GetGateCertificate(context.Background(), "cert_missing"); !errors.Is(err, ErrUnknownGateCertificate) {
		t.Fatalf("无行应 ErrUnknownGateCertificate, got %v", err)
	}
}

func TestGetGateCertificate_NullConfidenceFailsLoud(t *testing.T) {
	db := &fakeQueryDB{
		cert: &dbgen.GateCertificate{
			CertID: "cert_bad", ArtifactRef: "iv_1", CertType: "publish",
			PolicyVersion: "pv", IssuedBy: "x", IssuedAt: fqTS(fqTime), CreatedAt: fqTS(fqTime),
		},
		runs: []dbgen.GateRun{{
			RunID: "run_bad", CertificateID: fqText("cert_bad"), PolicyVersion: "pv",
			ValidatorID: "v", ValidatorVersion: "1", Verdict: dbgen.GateRunVerdictEnumPass,
			Evidence: mustJSON(t, `{}`), Confidence: pgtype.Numeric{}, // NOT NULL 列扫出 NULL：账面异常
			CostMs: 1, CostTokens: 1, RunAt: fqTS(fqTime), CreatedAt: fqTS(fqTime),
		}},
	}
	_, err := NewContentQueryService(db).GetGateCertificate(context.Background(), "cert_bad")
	if err == nil || errors.Is(err, ErrUnknownGateCertificate) {
		t.Fatalf("NULL confidence 必须 fail-loud 且不冒充无行: %v", err)
	}
}

func TestQueries_DriverErrorsPassThrough(t *testing.T) {
	svc := NewContentQueryService(&fakeQueryDB{failLookup: true})
	if _, err := svc.GetItem(context.Background(), "x"); !errors.Is(err, errLookupFailed) {
		t.Fatalf("取证故障应原样放行, got %v", err)
	}
	if _, err := svc.GetItemVersion(context.Background(), "x"); !errors.Is(err, errLookupFailed) {
		t.Fatalf("取证故障应原样放行, got %v", err)
	}
	if _, err := svc.GetTemplate(context.Background(), "x"); !errors.Is(err, errLookupFailed) {
		t.Fatalf("取证故障应原样放行, got %v", err)
	}
	if _, err := svc.GetGateCertificate(context.Background(), "x"); !errors.Is(err, errLookupFailed) {
		t.Fatalf("取证故障应原样放行, got %v", err)
	}

	// 列表面驱动故障：证书本体在、runs 列表炸——不得误报"证书不存在".
	listFail := &fakeQueryDB{
		cert: &dbgen.GateCertificate{CertID: "c", ArtifactRef: "a", CertType: "publish",
			PolicyVersion: "pv", IssuedBy: "i", IssuedAt: fqTS(fqTime), CreatedAt: fqTS(fqTime)},
		failList: true,
	}
	if _, err := NewContentQueryService(listFail).GetGateCertificate(context.Background(), "c"); !errors.Is(err, errListFailed) {
		t.Fatalf("列表故障应原样放行, got %v", err)
	}
}

func TestQueries_NilExecutorFailsClosed(t *testing.T) {
	var nilSvc *ContentQueryService
	if _, err := nilSvc.GetItem(context.Background(), "x"); !errors.Is(err, ErrNoExecutor) {
		t.Fatalf("nil 服务应 ErrNoExecutor, got %v", err)
	}
	unwired := NewContentQueryService(nil)
	for name, call := range map[string]func() error{
		"GetItem":            func() error { _, err := unwired.GetItem(context.Background(), "x"); return err },
		"GetItemVersion":     func() error { _, err := unwired.GetItemVersion(context.Background(), "x"); return err },
		"GetTemplate":        func() error { _, err := unwired.GetTemplate(context.Background(), "x"); return err },
		"GetGateCertificate": func() error { _, err := unwired.GetGateCertificate(context.Background(), "x"); return err },
	} {
		if err := call(); !errors.Is(err, ErrNoExecutor) {
			t.Fatalf("%s 未装配执行面应 ErrNoExecutor, got %v", name, err)
		}
	}
}
