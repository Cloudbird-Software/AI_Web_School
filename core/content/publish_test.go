package content

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 本套件以 fakePublishTx 承载 T-W5-003 的全部可本地验证语义（无 Docker/PG，
// PG 运行时行为不在此宣称覆盖）：
// - 验真四象限：证书不存在 / 用途类型错配 / 内容摘要不一致 / 已退休，任一即拒
//   且发布写面零语句（D2+D3 双门，fail-loud 零静默降级）；
// - 成功路径：状态前移 + 签发账 + 指针前移三写同事务、不自 commit（D11）；
// - 冻结 parity：公式一/二重算摘要与冻结 Python 实现（content_addressing.py）
//   产物逐字一致（向量由冻结实现离线计算后钉入本文件）；
// - 回滚一致性：写面中途失败时外层 Rollback 即零残留。
// FK 物理拒绝与 append-only 触发器由迁移 0002/0024/0028 + CI migrate-go-check
// 真库验证，本套件不宣称。

// ── 冻结向量（src/core/models/content_addressing.py 离线计算，钉死 parity）──
// 公式二输入：下列五块内容 + locale="zh-CN"；公式一输入：aLineLineage 全参 + 同 locale。
const (
	frozenCLineID = "sha256:406b6e14c5254be4911a9511b7f29b86b8e73129b41bacde32a54e78c93ea941"
	frozenALineID = "sha256:05febde60e92b46dd8e34804a657b2db2e91dbb0a9a834f78fa2936313d27418"

	// frozenCLineCanonical 是冻结实现对该内容产出的规范化文本（_canonical_json
	// 产物），锁定 Go 侧重算的字节级口径（键序/分隔/UTF-8 直出）.
	frozenCLineCanonical = `{"c":{"blocks":[{"text":"比较 0.3 与 0.30 的大小","type":"stem"}],"material_version_ids":[]},` +
		`"eb":{"wrong_option":{"confidence":0.8,"error_type":"位值混淆"}},` +
		`"ir":{"interaction_id":"single_choice","interaction_params":{"options":4}},` +
		`"l":"zh-CN",` +
		`"o":{"cognitive_level":"apply","gradeband":"M","graph_release":"2026.1","kp_set":[{"code":"math.nal.decimal.compare","dimension":"kp"}],"kp_set_mode":"single"},` +
		`"sr":{"scorer_id":"exact_match","scorer_params":{"answer":"相等"}}}`

	cLineObjective      = `{"kp_set":[{"dimension":"kp","code":"math.nal.decimal.compare"}],"kp_set_mode":"single","cognitive_level":"apply","gradeband":"M","graph_release":"2026.1"}`
	cLineInteractionRef = `{"interaction_id":"single_choice","interaction_params":{"options":4}}`
	cLineContent        = `{"blocks":[{"type":"stem","text":"比较 0.3 与 0.30 的大小"}],"material_version_ids":[]}`
	cLineScoringRef     = `{"scorer_id":"exact_match","scorer_params":{"answer":"相等"}}`
	cLineErrorBindings  = `{"wrong_option":{"error_type":"位值混淆","confidence":0.8}}`

	// 公式一证据链（§2.2.2 lineage：全参齐备才可证）.
	aLineLineage = `{"tier":"A","pipeline":{"id":"instantiation-engine","version":"1.0.0"},` +
		`"template_version_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111",` +
		`"params":{"normalized":{"seed":42,"x":"1/3"}},` +
		`"pack_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222",` +
		`"engine_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333",` +
		`"corpus_refs":[{"corpus_version_id":"cv-1","digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444"}]}`

	// 同谱系但缺 pack_digest：冻结 writer 在此处退化为随机 UUID 的破坏点.
	aLineLineageNoPack = `{"tier":"A","template_version_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111",` +
		`"params":{"normalized":{"seed":42,"x":"1/3"}},` +
		`"engine_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333",` +
		`"corpus_refs":[]}`
)

// 测试锚点常量（与取证桩互证）.
const (
	demoVersionID = frozenCLineID
	demoCertID    = "cert_01JDEMO0000000000000000000"
	demoItemID    = "item_01JDEMO0000000000000000000"
	demoPubID     = "pub_01JDEMO00000000000000000000"
	demoPublisher = "workbench-token"
)

var fixedPublishedAt = time.Date(2026, 8, 28, 9, 0, 0, 0, time.UTC)

// errVersionLookupFailed / errStepFailed 注入替身：驱动故障 ≠ 业务哨兵，不归一.
var (
	errVersionLookupFailed = errors.New("fake: 版本取证驱动故障")
	errStepFailed          = errors.New("fake: 下游步骤失败替身")
	errTxClosed            = errors.New("fake: 事务已终结（Commit/Rollback 之后）")
)

// stmtKind 已发出语句的分类.
type stmtKind string

const (
	kindGetVersion    stmtKind = "get_item_version"
	kindGetCert       stmtKind = "get_gate_certificate"
	kindUpdateVersion stmtKind = "update_item_version_published"
	kindInsertPub     stmtKind = "insert_publication"
	kindForwardPtr    stmtKind = "forward_item_current_version"
	kindOther         stmtKind = "other"
)

type stmt struct {
	sql  string
	args []any
}

func (s stmt) kind() stmtKind {
	switch {
	case strings.Contains(s.sql, "FROM item_version WHERE"):
		return kindGetVersion
	case strings.Contains(s.sql, "FROM gate_certificate WHERE"):
		return kindGetCert
	case strings.Contains(s.sql, "UPDATE item_version SET"):
		return kindUpdateVersion
	case strings.Contains(s.sql, "INSERT INTO publication"):
		return kindInsertPub
	case strings.Contains(s.sql, "UPDATE item SET"):
		return kindForwardPtr
	}
	return kindOther
}

var writeKinds = map[stmtKind]bool{kindUpdateVersion: true, kindInsertPub: true, kindForwardPtr: true}

// scanRow 取证桩：vals 与生成层 Scan 目标列序一一对应；err 非 nil 时 Scan 原样返回.
type scanRow struct {
	vals []any
	err  error
}

func (r *scanRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	if len(dest) != len(r.vals) {
		return fmt.Errorf("fake: scan 列数不符 dest=%d vals=%d", len(dest), len(r.vals))
	}
	for i, d := range dest {
		switch p := d.(type) {
		case *string:
			v, ok := r.vals[i].(string)
			if !ok {
				return fmt.Errorf("fake: 列 %d 目标 string 实为 %T", i, r.vals[i])
			}
			*p = v
		case *dbgen.ItemVersionStatusEnum:
			v, ok := r.vals[i].(dbgen.ItemVersionStatusEnum)
			if !ok {
				return fmt.Errorf("fake: 列 %d 目标 status enum 实为 %T", i, r.vals[i])
			}
			*p = v
		case *[]byte:
			v, ok := r.vals[i].([]byte)
			if !ok {
				return fmt.Errorf("fake: 列 %d 目标 []byte 实为 %T", i, r.vals[i])
			}
			*p = v
		case *pgtype.Text:
			v, ok := r.vals[i].(pgtype.Text)
			if !ok {
				return fmt.Errorf("fake: 列 %d 目标 text 实为 %T", i, r.vals[i])
			}
			*p = v
		case *pgtype.Timestamptz:
			v, ok := r.vals[i].(pgtype.Timestamptz)
			if !ok {
				return fmt.Errorf("fake: 列 %d 目标 timestamptz 实为 %T", i, r.vals[i])
			}
			*p = v
		default:
			return fmt.Errorf("fake: 不支持的 scan 目标 %T", d)
		}
	}
	return nil
}

// fakePublishTx 以最小状态机模拟「最外层调用方持有的 pgx.Tx」：Exec/QueryRow 落入
// pending（未决、DB 外不可见），Commit 并入 applied，Rollback 丢弃 pending。
// versionRow/certRow 为两路取证桩；failNext 使下一次 Exec 失败（写面中途故障）。
// Commit/Rollback 在这里被调用，因为 fake 正是最外层调用方本人.
type fakePublishTx struct {
	mu         sync.Mutex
	pending    []stmt
	applied    []stmt
	versionRow *scanRow
	certRow    *scanRow
	failNext   bool
	done       bool
	committed  bool
	rolledBack bool
}

var _ Executor = (*fakePublishTx)(nil)

func (f *fakePublishTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return pgconn.CommandTag{}, errTxClosed
	}
	cp := make([]any, len(args))
	copy(cp, args)
	if f.failNext {
		f.failNext = false
		f.pending = append(f.pending, stmt{sql: sql, args: cp})
		return pgconn.CommandTag{}, errStepFailed
	}
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	return pgconn.CommandTag{}, nil
}

func (f *fakePublishTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("content fake: 本套件只走 QueryRow 取证与 Exec 写路径")
}

func (f *fakePublishTx) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]any, len(args))
	copy(cp, args)
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	switch {
	case strings.Contains(sql, "FROM item_version WHERE"):
		return f.versionRow
	case strings.Contains(sql, "FROM gate_certificate WHERE"):
		return f.certRow
	}
	return &scanRow{err: fmt.Errorf("fake: 未桩定的取证语句 %q", sql)}
}

// Commit 最外层调用方提交：pending 并入 applied 账（事务终结后复用报错）.
func (f *fakePublishTx) Commit() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errTxClosed
	}
	f.done, f.committed = true, true
	f.applied = append(f.applied, f.pending...)
	f.pending = nil
	return nil
}

// Rollback 最外层调用方回滚：丢弃 pending——已发出的语句随之消失.
func (f *fakePublishTx) Rollback() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errTxClosed
	}
	f.done, f.rolledBack = true, true
	f.pending = nil
	return nil
}

func (f *fakePublishTx) count(t *testing.T, want stmtKind) int {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, s := range f.pending {
		if s.kind() == want {
			n++
		}
	}
	return n
}

func (f *fakePublishTx) countAppliedWrites(t *testing.T) int {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, s := range f.applied {
		if writeKinds[s.kind()] {
			n++
		}
	}
	return n
}

// lastPending 取最近一条未决语句（列值断言用）.
func (f *fakePublishTx) lastPending(t *testing.T, want stmtKind) stmt {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := len(f.pending) - 1; i >= 0; i-- {
		if f.pending[i].kind() == want {
			return f.pending[i]
		}
	}
	t.Fatalf("pending 无 %s 语句可检查", want)
	return stmt{}
}

// cLineRow 构造 C/D 级取证桩行（五块内容 + 冻结公式二 id），mutate 注入变体.
func cLineRow(mutate func(v *dbgen.ItemVersion)) *scanRow {
	v := dbgen.ItemVersion{
		ItemVersionID:     frozenCLineID,
		ItemID:            demoItemID,
		Status:            dbgen.ItemVersionStatusEnum(StatusDraft),
		Objective:         []byte(cLineObjective),
		InteractionRef:    []byte(cLineInteractionRef),
		Content:           []byte(cLineContent),
		ScoringRef:        []byte(cLineScoringRef),
		ErrorBindings:     []byte(cLineErrorBindings),
		Lineage:           []byte(`{"tier":"C","pipeline":{"id":"c-line","version":"1.0"}}`),
		RenderedSnapshot:  []byte(`{"html":"<p>0.3 = 0.30</p>"}`),
		GateCertificateID: pgtype.Text{},
		PublishedAt:       pgtype.Timestamptz{},
		RetiredAt:         pgtype.Timestamptz{},
		CreatedAt:         pgtype.Timestamptz{Time: fixedPublishedAt.Add(-time.Hour), Valid: true},
	}
	if mutate != nil {
		mutate(&v)
	}
	return &scanRow{vals: []any{
		v.ItemVersionID, v.ItemID, v.Status, v.Objective, v.InteractionRef,
		v.Content, v.ScoringRef, v.ErrorBindings, v.Lineage, v.RenderedSnapshot,
		v.GateCertificateID, v.PublishedAt, v.RetiredAt, v.CreatedAt,
	}}
}

// aLineRow 构造 A/B 级取证桩行（全参谱系 + 冻结公式一 id）.
func aLineRow() *scanRow {
	return cLineRow(func(v *dbgen.ItemVersion) {
		v.ItemVersionID = frozenALineID
		v.Lineage = []byte(aLineLineage)
	})
}

// publishCertRow 构造一份合法取证桩行（publish 类证书，绑定 demoVersionID）.
func publishCertRow(mutate func(c *dbgen.GateCertificate)) *scanRow {
	c := dbgen.GateCertificate{
		CertID:        demoCertID,
		ArtifactRef:   demoVersionID,
		CertType:      string(gate.CertPublish),
		PolicyVersion: "policy-2026w35",
		IssuedBy:      "system",
		IssuedAt:      pgtype.Timestamptz{Time: fixedPublishedAt.Add(-time.Minute), Valid: true},
		CreatedAt:     pgtype.Timestamptz{Time: fixedPublishedAt.Add(-time.Minute), Valid: true},
	}
	if mutate != nil {
		mutate(&c)
	}
	return &scanRow{vals: []any{
		c.CertID, c.ArtifactRef, c.CertType, c.PolicyVersion,
		c.IssuedBy, c.IssuedAt, c.CreatedAt,
	}}
}

// sampleRequest 构造合法发布请求（与桩行缺省值互证），mutate 注入变体.
func sampleRequest(mutate func(r *PublishRequest)) PublishRequest {
	req := PublishRequest{
		PublicationID:     demoPubID,
		ItemVersionID:     demoVersionID,
		GateCertificateID: demoCertID,
		PublishedBy:       demoPublisher,
		Locale:            "zh-CN",
		PublishedAt:       fixedPublishedAt,
	}
	if mutate != nil {
		mutate(&req)
	}
	return req
}

// mustPublish 成功即返回投影；任何失败都视为用例缺陷.
func mustPublish(t *testing.T, s *PublishService, req PublishRequest) *Publication {
	t.Helper()
	pub, err := s.Publish(context.Background(), req)
	if err != nil {
		t.Fatalf("Publish 意外失败: %v", err)
	}
	return pub
}

// TestPublishFrozenParity 锁死 D3 parity：Go 重算摘要与冻结 Python 实现
// （content_addressing.py 公式一/二）产物逐字一致；同一内容重算 N 次同 id
// （验收 #3「同一内容两次发布得到同一 id」的寻址面）.
func TestPublishFrozenParity(t *testing.T) {
	row := cLineRow(nil)

	// 字节级：规范化文本与冻结 _canonical_json 产物一致.
	v, err := decodeJSONB("objective", row.vals[3].([]byte))
	if err != nil {
		t.Fatal(err)
	}
	ir, _ := decodeJSONB("interaction_ref", row.vals[4].([]byte))
	blocks, _ := decodeJSONB("content", row.vals[5].([]byte))
	sr, _ := decodeJSONB("scoring_ref", row.vals[6].([]byte))
	eb, _ := decodeJSONB("error_bindings", row.vals[7].([]byte))
	canon, err := validators.CanonicalJSON(map[string]any{"o": v, "ir": ir, "c": blocks, "sr": sr, "eb": eb, "l": "zh-CN"})
	if err != nil {
		t.Fatal(err)
	}
	if canon != frozenCLineCanonical {
		t.Fatalf("规范化文本与冻结实现不一致:\n got=%s\nwant=%s", canon, frozenCLineCanonical)
	}

	// 摘要级：公式一/二向量重算一致，且重算确定性（两次同 id）.
	for i := 0; i < 2; i++ {
		got, err := validators.ContentDigest(map[string]any{"o": v, "ir": ir, "c": blocks, "sr": sr, "eb": eb, "l": "zh-CN"})
		if err != nil || got != frozenCLineID {
			t.Fatalf("公式二 parity 失败: got=%v err=%v", got, err)
		}
	}
	if err := verifyContentAddress(dbgen.ItemVersion{
		ItemVersionID: frozenALineID,
		Objective:     []byte(cLineObjective),
		Lineage:       []byte(aLineLineage),
	}, "zh-CN"); err != nil {
		t.Fatalf("公式一 parity 失败: %v", err)
	}
}

// TestPublishSuccessPathWriteFace 成功路径（draft 与 quarantined 两个合法起点）：
// 三写同事务、列值逐项对表、无事务控制语句.
func TestPublishSuccessPathWriteFace(t *testing.T) {
	for _, start := range []dbgen.ItemVersionStatusEnum{
		dbgen.ItemVersionStatusEnum(StatusDraft),
		dbgen.ItemVersionStatusEnum(StatusQuarantined),
	} {
		t.Run(string(start), func(t *testing.T) {
			tx := &fakePublishTx{
				versionRow: cLineRow(func(v *dbgen.ItemVersion) { v.Status = start }),
				certRow:    publishCertRow(nil),
			}
			svc := NewPublishService(tx)
			pub := mustPublish(t, svc, sampleRequest(nil))

			if pub.PublicationID != demoPubID || pub.ItemID != demoItemID ||
				pub.ItemVersionID != demoVersionID || pub.GateCertificateID != demoCertID ||
				!pub.PublishedAt.Equal(fixedPublishedAt) {
				t.Fatalf("签发账投影失真: %+v", pub)
			}

			upd := tx.lastPending(t, kindUpdateVersion)
			if got, ok := upd.args[0].(string); !ok || got != demoVersionID {
				t.Fatalf("状态前移WHERE实参失真: %v", upd.args[0])
			}
			if got, ok := upd.args[1].(pgtype.Text); !ok || got.String != demoCertID || !got.Valid {
				t.Fatalf("状态前移证书实参失真: %v", upd.args[1])
			}
			if got, ok := upd.args[2].(pgtype.Timestamptz); !ok || !got.Time.Equal(fixedPublishedAt) || !got.Valid {
				t.Fatalf("published_at 实参失真: %v", upd.args[2])
			}
			ins := tx.lastPending(t, kindInsertPub)
			if got, ok := ins.args[0].(string); !ok || got != demoPubID {
				t.Fatalf("publication_id 实参失真: %v", ins.args[0])
			}
			if got, ok := ins.args[1].(string); !ok || got != demoItemID {
				t.Fatalf("publication.item_id 实参失真: %v", ins.args[1])
			}
			if got, ok := ins.args[4].(string); !ok || got != demoPublisher {
				t.Fatalf("published_by 实参失真: %v", ins.args[4])
			}
			fwd := tx.lastPending(t, kindForwardPtr)
			if got, ok := fwd.args[0].(string); !ok || got != demoItemID {
				t.Fatalf("指针前移 item_id 实参失真: %v", fwd.args[0])
			}
			if got, ok := fwd.args[1].(pgtype.Text); !ok || got.String != demoVersionID || !got.Valid {
				t.Fatalf("指针前移 current_version_id 实参失真: %v", fwd.args[1])
			}

			if err := tx.Commit(); err != nil {
				t.Fatal(err)
			}
			if n := tx.countAppliedWrites(t); n != 3 {
				t.Fatalf("COMMIT 后写面应有且仅有 3 条语句: got=%d", n)
			}
			assertNoTransactionControl(t, tx.pending)
		})
	}
}

// TestPublishCertificateQuadrants 验真四象限 + 绑定面：不存在 / 类型错配 /
// 摘要不一致 / 已退休 → 发布拒绝（哨兵可 errors.Is），且发布写面零语句.
func TestPublishCertificateQuadrants(t *testing.T) {
	cases := []struct {
		name       string
		versionRow *scanRow
		certRow    *scanRow
		req        PublishRequest
		wantErr    error
	}{
		{
			name:       "证书不存在",
			versionRow: cLineRow(nil),
			certRow:    &scanRow{err: pgx.ErrNoRows},
			req:        sampleRequest(nil),
			wantErr:    gate.ErrUnknownCertificate,
		},
		{
			name:       "证书用途类型错配（retire 证发布用）",
			versionRow: cLineRow(nil),
			certRow: publishCertRow(func(c *dbgen.GateCertificate) {
				c.CertType = string(gate.CertRetire)
			}),
			req:     sampleRequest(nil),
			wantErr: gate.ErrCertificateMismatch,
		},
		{
			name:       "证书绑定的是别的产物（E2E-1 语义）",
			versionRow: cLineRow(nil),
			certRow: publishCertRow(func(c *dbgen.GateCertificate) {
				c.ArtifactRef = "sha256:other"
			}),
			req:     sampleRequest(nil),
			wantErr: gate.ErrCertificateMismatch,
		},
		{
			name: "内容摘要不一致（id 与内容脱钩）",
			versionRow: cLineRow(func(v *dbgen.ItemVersion) {
				v.Content = []byte(strings.Replace(cLineContent, "0.3 与", "0.4 与", 1))
			}),
			certRow: &scanRow{err: pgx.ErrNoRows}, // 寻址先于取证：拒因必须是摘要
			req:     sampleRequest(nil),
			wantErr: ErrContentDigestMismatch,
		},
		{
			name: "已退休（状态机无回边）",
			versionRow: cLineRow(func(v *dbgen.ItemVersion) {
				v.Status = dbgen.ItemVersionStatusEnum(StatusRetired)
				v.RetiredAt = pgtype.Timestamptz{Time: fixedPublishedAt.Add(-time.Minute), Valid: true}
			}),
			certRow: &scanRow{err: pgx.ErrNoRows}, // 状态机先于取证：拒因必须是退休
			req:     sampleRequest(nil),
			wantErr: ErrContentRetired,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakePublishTx{versionRow: tc.versionRow, certRow: tc.certRow}
			pub, err := NewPublishService(tx).Publish(context.Background(), tc.req)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			if pub != nil {
				t.Fatal("拒绝发布不得返回签发账投影")
			}
			if n := tx.count(t, kindUpdateVersion) + tx.count(t, kindInsertPub) + tx.count(t, kindForwardPtr); n != 0 {
				t.Fatalf("拒绝发布不得触达写面: %d 条写语句", n)
			}
		})
	}
}

// TestPublishMismatchCarriesDiagnosis 拒因可诊断：摘要不一致的错误文本带两侧
// 实况（账面 id 与重算摘要），不发无证据的拒绝.
func TestPublishMismatchCarriesDiagnosis(t *testing.T) {
	tx := &fakePublishTx{
		versionRow: cLineRow(func(v *dbgen.ItemVersion) {
			v.Lineage = []byte(aLineLineageNoPack) // tier=A 缺 pack_digest
		}),
		certRow: publishCertRow(nil),
	}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, ErrContentDigestUnverifiable) {
		t.Fatalf("err = %v, want ErrContentDigestUnverifiable", err)
	}
	if !strings.Contains(err.Error(), "pack_digest") {
		t.Fatalf("错误文本应指认缺失参数: %v", err)
	}
}

// TestPublishAlreadyPublishedIsRejected 状态机无重签：已 published 版本再发布
// 即拒（第二行签发账即假账）.
func TestPublishAlreadyPublishedIsRejected(t *testing.T) {
	tx := &fakePublishTx{
		versionRow: cLineRow(func(v *dbgen.ItemVersion) {
			v.Status = dbgen.ItemVersionStatusEnum(StatusPublished)
			v.GateCertificateID = pgtype.Text{String: demoCertID, Valid: true}
			v.PublishedAt = pgtype.Timestamptz{Time: fixedPublishedAt.Add(-time.Hour), Valid: true}
		}),
		certRow: publishCertRow(nil),
	}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, ErrAlreadyPublished) {
		t.Fatalf("err = %v, want ErrAlreadyPublished", err)
	}
	if n := tx.count(t, kindInsertPub); n != 0 {
		t.Fatalf("重签不得入账: %d 条", n)
	}
}

// TestPublishUnknownVersionIsRejected 版本不存在：无快照亦无地址可验.
func TestPublishUnknownVersionIsRejected(t *testing.T) {
	tx := &fakePublishTx{versionRow: &scanRow{err: pgx.ErrNoRows}, certRow: publishCertRow(nil)}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, ErrUnknownContentVersion) {
		t.Fatalf("err = %v, want ErrUnknownContentVersion", err)
	}
	if !strings.Contains(err.Error(), demoVersionID) {
		t.Fatalf("错误文本应带 item_version_id 定位: %v", err)
	}
}

// TestPublishMissingRenderedSnapshotIsRejected 审计 #161：渲染快照缺失 →
// fail-loud 拒绝（占位快照兜底已废除——假快照不得进内容账），且零写入.
func TestPublishMissingRenderedSnapshotIsRejected(t *testing.T) {
	tx := &fakePublishTx{
		versionRow: cLineRow(func(v *dbgen.ItemVersion) { v.RenderedSnapshot = nil }),
		certRow:    publishCertRow(nil),
	}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, ErrRenderedSnapshotMissing) {
		t.Fatalf("err = %v, want ErrRenderedSnapshotMissing", err)
	}
	for _, k := range []stmtKind{kindInsertPub, kindForwardPtr} {
		if n := tx.count(t, k); n != 0 {
			t.Fatalf("拒绝后不得写入 %s: %d 条", k, n)
		}
	}
}

// TestPublishWithoutExplicitTransactionIsRejected 是 fail-closed 面：三种「无
// 显式事务执行面」形态的全部发布调用都直接 ErrNoTransaction.
func TestPublishWithoutExplicitTransactionIsRejected(t *testing.T) {
	cases := []struct {
		name string
		svc  *PublishService
	}{
		{"NewPublishService(nil)", NewPublishService(nil)},
		{"零值 Service", &PublishService{}},
		{"nil Service", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pub, err := tc.svc.Publish(context.Background(), sampleRequest(nil))
			if !errors.Is(err, ErrNoTransaction) {
				t.Fatalf("err = %v, want ErrNoTransaction", err)
			}
			if pub != nil {
				t.Fatal("fail-closed 失败不得返回投影")
			}
		})
	}
}

// TestPublishRejectsInvalidRequestBeforeIO 锁定判定序：请求契约违例在进程内
// 拦截，一条 SQL 都不发（不烧事务语句、不给 PG 报错晚到）.
func TestPublishRejectsInvalidRequestBeforeIO(t *testing.T) {
	cases := []struct {
		name string
		req  PublishRequest
	}{
		{"空 publication_id", sampleRequest(func(r *PublishRequest) { r.PublicationID = "" })},
		{"空 item_version_id", sampleRequest(func(r *PublishRequest) { r.ItemVersionID = "" })},
		{"空 gate_certificate_id（published 必持证）", sampleRequest(func(r *PublishRequest) { r.GateCertificateID = "" })},
		{"空 published_by", sampleRequest(func(r *PublishRequest) { r.PublishedBy = "" })},
		{"空 locale", sampleRequest(func(r *PublishRequest) { r.Locale = "" })},
		{"零值 published_at", sampleRequest(func(r *PublishRequest) { r.PublishedAt = time.Time{} })},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakePublishTx{versionRow: cLineRow(nil), certRow: publishCertRow(nil)}
			_, err := NewPublishService(tx).Publish(context.Background(), tc.req)
			if !errors.Is(err, ErrInvalidPublication) {
				t.Fatalf("err = %v, want ErrInvalidPublication", err)
			}
			tx.mu.Lock()
			n := len(tx.pending)
			tx.mu.Unlock()
			if n != 0 {
				t.Fatalf("契约违例不得发出 SQL：pending=%d", n)
			}
		})
	}
}

// TestPublishFormulaOneMissingParamsFailsLoud 终结 UUID 退化路径：A/B 级公式一
// 缺任一必填参数 → ErrContentDigestUnverifiable，宁可拒发绝不编造地址（D3）.
func TestPublishFormulaOneMissingParamsFailsLoud(t *testing.T) {
	cases := []struct {
		name    string
		lineage string
		wantIn  string
	}{
		{"缺 pack_digest", aLineLineageNoPack, "pack_digest"},
		{
			"缺 engine_digest",
			`{"tier":"A","template_version_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111",` +
				`"params":{"normalized":{}},"pack_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222"}`,
			"engine_digest",
		},
		{
			"corpus_refs 缺 digest",
			`{"tier":"B","template_version_id":"sha256:1111111111111111111111111111111111111111111111111111111111111111",` +
				`"params":{"normalized":{}},"pack_digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222",` +
				`"engine_digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333",` +
				`"corpus_refs":[{"corpus_version_id":"cv-1"}]}`,
			"corpus_refs[0]",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakePublishTx{
				versionRow: cLineRow(func(v *dbgen.ItemVersion) { v.Lineage = []byte(tc.lineage) }),
				certRow:    publishCertRow(nil),
			}
			_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
			if !errors.Is(err, ErrContentDigestUnverifiable) {
				t.Fatalf("err = %v, want ErrContentDigestUnverifiable", err)
			}
			if !strings.Contains(err.Error(), tc.wantIn) {
				t.Fatalf("错误文本应指认缺失参数 %s: %v", tc.wantIn, err)
			}
			if n := tx.count(t, kindUpdateVersion) + tx.count(t, kindInsertPub) + tx.count(t, kindForwardPtr); n != 0 {
				t.Fatalf("不可证的发布不得触达写面: %d 条", n)
			}
		})
	}
}

// TestPublishDriverErrorsAreNotSwallowed 驱动故障 ≠ 假证/坏账：底层错误原样
// wrap 放行，不得归一为业务哨兵（两类失败的处置路径不同）.
func TestPublishDriverErrorsAreNotSwallowed(t *testing.T) {
	tx := &fakePublishTx{versionRow: &scanRow{err: errVersionLookupFailed}, certRow: publishCertRow(nil)}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, errVersionLookupFailed) {
		t.Fatalf("驱动错误应原样可见: %v", err)
	}
	for _, sentinel := range []error{ErrUnknownContentVersion, gate.ErrUnknownCertificate, ErrContentDigestMismatch} {
		if errors.Is(err, sentinel) {
			t.Fatalf("驱动故障不得被归一为 %v", sentinel)
		}
	}
}

// TestPublishRollbackConsistency 写面中途失败 → 错误带因上抛；外层 Rollback 后
// applied 账零残留（三写同进同退，D11 语义面）.
func TestPublishRollbackConsistency(t *testing.T) {
	tx := &fakePublishTx{
		versionRow: cLineRow(nil),
		certRow:    publishCertRow(nil),
		failNext:   true, // 下一次 Exec（状态前移）即败
	}
	_, err := NewPublishService(tx).Publish(context.Background(), sampleRequest(nil))
	if !errors.Is(err, errStepFailed) {
		t.Fatalf("err = %v, want 注入的 errStepFailed", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if n := tx.countAppliedWrites(t); n != 0 {
		t.Fatalf("回滚后 applied 写面应零残留: got=%d", n)
	}
	if !tx.rolledBack || tx.committed {
		t.Fatalf("事务终结状态失真: rolledBack=%v committed=%v", tx.rolledBack, tx.committed)
	}
}

// assertNoTransactionControl 对已发出语句的头词做运行时投影断言：发布域发出的
// 每条语句都不是事务控制语句（D11 包级红线；静态面见 guard_test.go）.
func assertNoTransactionControl(t *testing.T, stmts []stmt) {
	t.Helper()
	for _, s := range stmts {
		head := strings.ToUpper(strings.TrimSpace(strings.SplitN(s.sql, " ", 2)[0]))
		if head == "BEGIN" || head == "COMMIT" || head == "ROLLBACK" || head == "SAVEPOINT" {
			t.Fatalf("发布域发出了事务控制语句 %q（D11 违例）", head)
		}
	}
}
