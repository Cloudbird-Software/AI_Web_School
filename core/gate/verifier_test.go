package gate

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 本套件以 fakeGateTx 承载 T-W5-002 的全部可本地验证语义（无 Docker/PG，
// PG 运行时行为不在此宣称覆盖）：
// - 验真：存在性 + 一致性（类型/绑定逐项对表），无效证书与错配证书在进入
//   内容写入前失败（D2 消费端）；契约违例先行于 IO；
// - 留痕：failure 是账面事实——Record 契约字段逐列映射、nil 证据记 '{}'、
//   失败行随外层事务同进同退（不自 commit，D11）。
// FK 物理拒绝（直写 cert_FAKE 被拒）与 append-only 触发器由迁移 0028 +
// CI make migrate-go-check 真库验证，本套件不宣称。

// errCertLookupFailed 扮演驱动层故障：与「假证」不同类，不许被归一吞并.
var errCertLookupFailed = errors.New("fake: 取证驱动故障")

// errStepFailed 注入的下游失败步：促使最外层调用方整体回滚.
var errStepFailed = errors.New("fake: 下游步骤失败替身")

// errTxClosed 模拟 pgx.Tx 终结后操作的失败语义.
var errTxClosed = errors.New("fake: 事务已终结（Commit/Rollback 之后）")

// stmtKind 已发出语句的分类.
type stmtKind string

const (
	kindInsertFailure  stmtKind = "insert_gate_failure"
	kindGetCertificate stmtKind = "get_gate_certificate"
	kindOther          stmtKind = "other"
)

type stmt struct {
	sql  string
	args []any
}

func (s stmt) kind() stmtKind {
	switch {
	case strings.Contains(s.sql, "INSERT INTO gate_failure"):
		return kindInsertFailure
	case strings.Contains(s.sql, "SELECT cert_id, artifact_ref"):
		return kindGetCertificate
	}
	return kindOther
}

// fakeRow 定格一条取证结果：vals 与 dbgen.GetGateCertificate 生成的 Scan 目标
// 七列序一一对应；err 非 nil 时 Scan 原样返回（模拟 ErrNoRows / 驱动故障）.
type fakeRow struct {
	vals []any
	err  error
}

func (r *fakeRow) Scan(dest ...any) error {
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

// fakeGateTx 以最小状态机模拟「最外层调用方持有的 pgx.Tx」：Exec/QueryRow 落入
// pending（未决、DB 外不可见），Commit 并入 applied，Rollback 丢弃 pending。
// certificateRow 为取证桩（下一旬 QueryRow 返回它）；nil 时 QueryRow 也入账但
// Scan 报缺桩错误。Commit/Rollback 在这里被调用，因为 fake 正是最外层调用方本人.
type fakeGateTx struct {
	mu             sync.Mutex
	pending        []stmt
	applied        []stmt
	certificateRow *fakeRow
	failNext       bool
	done           bool
	committed      bool
	rolledBack     bool
}

var _ Executor = (*fakeGateTx)(nil)

func (f *fakeGateTx) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return pgconn.CommandTag{}, errTxClosed
	}
	if f.failNext {
		f.failNext = false
		return pgconn.CommandTag{}, errStepFailed
	}
	cp := make([]any, len(args))
	copy(cp, args)
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	return pgconn.CommandTag{}, nil
}

func (f *fakeGateTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("gate fake: 本套件只走 QueryRow 取证与 Exec 写路径")
}

func (f *fakeGateTx) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]any, len(args))
	copy(cp, args)
	f.pending = append(f.pending, stmt{sql: sql, args: cp})
	return f.certificateRow
}

// Commit 最外层调用方提交：pending 并入 applied 账（事务终结后复用报错）.
func (f *fakeGateTx) Commit() error {
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
func (f *fakeGateTx) Rollback() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.done {
		return errTxClosed
	}
	f.done, f.rolledBack = true, true
	f.pending = nil
	return nil
}

func (f *fakeGateTx) countApplied(t *testing.T, want stmtKind) int {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, s := range f.applied {
		if s.kind() == want {
			n++
		}
	}
	return n
}

func (f *fakeGateTx) lastPending(t *testing.T) stmt {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.pending) == 0 {
		t.Fatal("pending 无语句可检查")
	}
	return f.pending[len(f.pending)-1]
}

// fixedIssuedAt 固定签发时刻，保证投影断言确定性.
var fixedIssuedAt = time.Date(2026, 8, 27, 8, 30, 0, 0, time.UTC)

// certRow 构造一份合法的取证桩行（publish 类证书，绑定 item-v-1），mutate 注入变体.
func certRow(mutate func(c *dbgen.GateCertificate)) *fakeRow {
	c := dbgen.GateCertificate{
		CertID:        "cert_01JDEMO0000000000000000000",
		ArtifactRef:   "item-v-1",
		CertType:      string(CertPublish),
		PolicyVersion: "policy-2026w35",
		IssuedBy:      "system",
		IssuedAt:      pgtype.Timestamptz{Time: fixedIssuedAt, Valid: true},
		CreatedAt:     pgtype.Timestamptz{Time: fixedIssuedAt.Add(time.Minute), Valid: true},
	}
	if mutate != nil {
		mutate(&c)
	}
	return &fakeRow{vals: []any{
		c.CertID, c.ArtifactRef, c.CertType, c.PolicyVersion,
		c.IssuedBy, c.IssuedAt, c.CreatedAt,
	}}
}

// sampleWant 构造合法使用声明（与 certRow 缺省值互证），mutate 注入变体.
func sampleWant(mutate func(r *Requirement)) Requirement {
	want := Requirement{ArtifactRef: "item-v-1", CertType: CertPublish}
	if mutate != nil {
		mutate(&want)
	}
	return want
}

// mustVerify 验真成功即返回证书投影；任何失败都视为用例缺陷.
func mustVerify(t *testing.T, v *CertificateVerifier, certID string, want Requirement) *Certificate {
	t.Helper()
	cert, err := v.Verify(context.Background(), certID, want)
	if err != nil {
		t.Fatalf("Verify 意外失败: %v", err)
	}
	return cert
}

// TestVerifyWithoutExplicitTransactionIsRejected 是 fail-closed 面：三种「无显式
// 事务执行面」形态的全部验真调用都直接 ErrNoTransaction.
func TestVerifyWithoutExplicitTransactionIsRejected(t *testing.T) {
	cases := []struct {
		name string
		v    *CertificateVerifier
	}{
		{"NewCertificateVerifier(nil)", NewCertificateVerifier(nil)},
		{"零值 Verifier", &CertificateVerifier{}},
		{"nil Verifier", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cert, err := tc.v.Verify(context.Background(), "cert_x", sampleWant(nil))
			if !errors.Is(err, ErrNoTransaction) {
				t.Fatalf("err = %v, want ErrNoTransaction", err)
			}
			if cert != nil {
				t.Fatal("fail-closed 失败不得返回证书")
			}
		})
	}
}

// TestVerifyRejectsInvalidRequirementBeforeIO 锁定判定序：声明违例在进程内拦截，
// 一条 SQL 都不发（不烧事务语句、不给 PG 报错晚到）.
func TestVerifyRejectsInvalidRequirementBeforeIO(t *testing.T) {
	cases := []struct {
		name   string
		certID string
		want   Requirement
	}{
		{"空 cert_id", "", sampleWant(nil)},
		{"空 artifact_ref", "cert_01JDEMO0000000000000000000", sampleWant(func(r *Requirement) { r.ArtifactRef = "" })},
		{"空 cert_type", "cert_01JDEMO0000000000000000000", sampleWant(func(r *Requirement) { r.CertType = "" })},
		{"越域 cert_type", "cert_01JDEMO0000000000000000000", sampleWant(func(r *Requirement) { r.CertType = CertType("grant") })},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakeGateTx{}
			_, err := NewCertificateVerifier(tx).Verify(context.Background(), tc.certID, tc.want)
			if !errors.Is(err, ErrInvalidRequirement) {
				t.Fatalf("err = %v, want ErrInvalidRequirement", err)
			}
			tx.mu.Lock()
			n := len(tx.pending)
			tx.mu.Unlock()
			if n != 0 {
				t.Fatalf("声明违例不得发出 SQL：pending=%d", n)
			}
		})
	}
}

// TestVerifyExistenceAndConsistency 是 D2 消费端主矩阵：存在性（无行即拒）与
// 一致性（类型/绑定逐项对表）四象限——有证且相符才放行.
func TestVerifyExistenceAndConsistency(t *testing.T) {
	const demoCert = "cert_01JDEMO0000000000000000000"

	cases := []struct {
		name    string
		row     func() *fakeRow
		want    Requirement
		wantErr error
		assert  func(t *testing.T, err error)
	}{
		{
			name: "持证且类型绑定相符",
			row:  func() *fakeRow { return certRow(nil) },
			want: sampleWant(nil),
		},
		{
			name: "retire 用途与 retire 证书相符",
			row: func() *fakeRow {
				return certRow(func(c *dbgen.GateCertificate) { c.CertType = string(CertRetire) })
			},
			want: sampleWant(func(r *Requirement) { r.CertType = CertRetire }),
		},
		{
			name:    "无此证书（存在性拒绝）",
			row:     func() *fakeRow { return &fakeRow{err: pgx.ErrNoRows} },
			want:    sampleWant(nil),
			wantErr: ErrUnknownCertificate,
			assert: func(t *testing.T, err error) {
				if !strings.Contains(err.Error(), demoCert) {
					t.Fatalf("错误文本应带 cert_id 定位： %v", err)
				}
			},
		},
		{
			name: "证书存在但用途类型错配",
			row: func() *fakeRow {
				return certRow(func(c *dbgen.GateCertificate) { c.CertType = string(CertPublish) })
			},
			want:    sampleWant(func(r *Requirement) { r.CertType = CertRetire }),
			wantErr: ErrCertificateMismatch,
			assert: func(t *testing.T, err error) {
				if !strings.Contains(err.Error(), `"publish"`) || !strings.Contains(err.Error(), `"retire"`) {
					t.Fatalf("错误文本应带两侧类型实况: %v", err)
				}
			},
		},
		{
			name: "证书存在但绑定的是别的产物",
			row: func() *fakeRow {
				return certRow(func(c *dbgen.GateCertificate) { c.ArtifactRef = "item-v-other" })
			},
			want:    sampleWant(nil),
			wantErr: ErrCertificateMismatch,
			assert: func(t *testing.T, err error) {
				if !strings.Contains(err.Error(), "item-v-other") || !strings.Contains(err.Error(), "item-v-1") {
					t.Fatalf("错误文本应带两侧产物实况: %v", err)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakeGateTx{certificateRow: tc.row()}
			cert, err := NewCertificateVerifier(tx).Verify(context.Background(), demoCert, tc.want)
			if tc.wantErr == nil {
				if err != nil {
					t.Fatalf("Verify 意外失败: %v", err)
				}
				if cert == nil || cert.CertID != demoCert || cert.ArtifactRef != tc.want.ArtifactRef ||
					cert.CertType != tc.want.CertType || cert.PolicyVersion != "policy-2026w35" ||
					cert.IssuedBy != "system" || !cert.IssuedAt.Equal(fixedIssuedAt) {
					t.Fatalf("证书投影失真: %+v", cert)
				}
				return
			}
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			if cert != nil {
				t.Fatal("验真失败不得返回证书")
			}
			if tc.assert != nil {
				tc.assert(t, err)
			}
		})
	}
}

// TestVerifyDriverErrorsAreNotSwallowed 驱动故障 ≠ 假证：底层错误原样 wrap 放行，
// 两类失败的处置路径不同（假证拦发布、故障走运维），归一混报即事故放大器.
func TestVerifyDriverErrorsAreNotSwallowed(t *testing.T) {
	tx := &fakeGateTx{certificateRow: &fakeRow{err: errCertLookupFailed}}
	_, err := NewCertificateVerifier(tx).Verify(context.Background(), "cert_x", sampleWant(nil))
	if !errors.Is(err, errCertLookupFailed) {
		t.Fatalf("驱动错误应原样可见: %v", err)
	}
	if errors.Is(err, ErrUnknownCertificate) || errors.Is(err, ErrCertificateMismatch) {
		t.Fatalf("驱动故障不得被归一为业务哨兵: %v", err)
	}
}

// TestGateSourcesNeverIssueTransactionControlStatements 对取证语句的头词做运行时
// 投影断言：门域发出的每条语句都不是事务控制语句（D11 包级红线；静态面见
// guard_test.go）.
func TestGateSourcesNeverIssueTransactionControlStatements(t *testing.T) {
	tx := &fakeGateTx{certificateRow: certRow(nil)}
	mustVerify(t, NewCertificateVerifier(tx), "cert_01JDEMO0000000000000000000", sampleWant(nil))
	for _, s := range tx.pending {
		head := strings.ToUpper(strings.TrimSpace(strings.SplitN(s.sql, " ", 2)[0]))
		if head == "BEGIN" || head == "COMMIT" || head == "ROLLBACK" || head == "SAVEPOINT" {
			t.Fatalf("门域发出了事务控制语句 %q（D11 违例）", head)
		}
	}
}
