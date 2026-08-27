package compliance

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// 测试以内存实现承载 T-W5-012 的全部可本地验证语义（go test -race 下运行）：
// - allow/deny 权限矩阵表驱动：fail-closed 判定序逐格锁定（未认证/角色缺失/
//   别名不匹配/别名非法/操作-角色错配一律 deny）；
// - 独立事务双走向（验收 #2/#3）：业务成功审计失败 → 身份照常交付且错误可
//   感知；业务失败审计成功 → failed 留痕存活；业务回滚（identities 快照恢复，
//   业务事务回滚的内存等效）不抹掉审计行；
// - deny 也是审计事实且不产生任何业务语句；同执行面双传即拒绝；
// - PII 纪律：密文账零明文、错误链零明文零密钥材料。
// PG 实现的运行时行为不在此宣称覆盖（无 Docker/PG），仅测错误分类等纯函数面
// （与 memory_test.go 同口径）。

var (
	testVaultKey = bytes.Repeat([]byte{0x5A}, vaultKeyBytes)

	// 测试主体：reader（绑定 studentA）/ writer / 未认证。
	readerPrincipal = VaultPrincipal{Name: "support-service", Role: RoleVaultReader}
	writerPrincipal = VaultPrincipal{Name: "intake-service", Role: RoleVaultWriter}
	anonPrincipal   VaultPrincipal

	testAliasA = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
	testAliasB = "9c858901-8a57-4791-81fe-4c455b099bc9"

	// 明文标记（断言「零明文出账/出错误」的探针——字符串足够独特防巧合命中）.
	markerName  = "王小明_unique_marker_name"
	markerPhone = "13800001111"
	markerAddr  = "广州市天河区_unique_marker_addr"
	markerPare  = "王母_unique_marker_parent"

	auditSinkDown = errors.New("audit sink down（测试注入的审计写失败）")
)

func mustVaultService(t *testing.T, store VaultStore) *VaultService {
	t.Helper()
	svc, err := NewVaultService(NewStaticVaultAccess(), store, testVaultKey)
	if err != nil {
		t.Fatalf("装配 vault 服务意外失败: %v", err)
	}
	return svc
}

// seedIdentity 以 writer 主体经服务写入一条直标识（走真实加密路径，而非
// 直插明文 fixture——X11 禁测试与实现互证）.
func seedIdentity(t *testing.T, svc *VaultService, q, auditQ Executor, alias string) {
	t.Helper()
	err := svc.WriteIdentity(context.Background(), q, auditQ, writerPrincipal, VaultWriteRequest{
		StudentAliasID: alias,
		Name:           markerName,
		Phone:          markerPhone,
		Address:        markerAddr,
		ParentContact:  markerPare,
		Accessor:       "test-intake",
		Purpose:        "test_seed",
	})
	if err != nil {
		t.Fatalf("种子直标识写入意外失败: %v", err)
	}
}

// ────────────────────────────────────────────────────────────────────
// 一、权限矩阵（纯判定面，表驱动）
// ────────────────────────────────────────────────────────────────────

func TestVaultAccessMatrix(t *testing.T) {
	access := NewStaticVaultAccess()

	cases := []struct {
		name      string
		p         VaultPrincipal
		op        VaultOperation
		alias     string
		wantAllow bool
		wantWhy   DenyReason
	}{
		{"reader 读未绑定 alias 放行", readerPrincipal, VaultOpReadIdentity, testAliasA, true, ""},
		{"writer 写放行", writerPrincipal, VaultOpWriteIdentity, testAliasA, true, ""},
		{"reader 写拒（角色-操作错配）", readerPrincipal, VaultOpWriteIdentity, testAliasA, false, DenyRoleMismatch},
		{"writer 读拒（角色-操作错配）", writerPrincipal, VaultOpReadIdentity, testAliasA, false, DenyRoleMismatch},
		{"未认证拒", anonPrincipal, VaultOpReadIdentity, testAliasA, false, DenyUnauthenticated},
		{"未认证优先于坏角色", VaultPrincipal{Role: "pii_vault_admin"}, VaultOpReadIdentity, testAliasA, false, DenyUnauthenticated},
		{"未知角色拒", VaultPrincipal{Name: "svc", Role: "pii_vault_admin"}, VaultOpReadIdentity, testAliasA, false, DenyRoleMissing},
		{"空角色拒", VaultPrincipal{Name: "svc"}, VaultOpWriteIdentity, testAliasA, false, DenyRoleMissing},
		{"未知操作值拒", readerPrincipal, VaultOperation("drop_tables"), testAliasA, false, DenyRoleMismatch},
		{"非法别名拒", readerPrincipal, VaultOpReadIdentity, "not-a-uuid", false, DenyAliasMalformed},
		{"空别名拒", readerPrincipal, VaultOpReadIdentity, "", false, DenyAliasMalformed},
		{"writer 遇非法别名同拒", writerPrincipal, VaultOpWriteIdentity, "student-x", false, DenyAliasMalformed},
		{"绑定主体触自身 alias 放行", VaultPrincipal{Name: "student-app", Role: RoleVaultReader, BoundAlias: testAliasA},
			VaultOpReadIdentity, testAliasA, true, ""},
		{"绑定主体触他人 alias 拒", VaultPrincipal{Name: "student-app", Role: RoleVaultReader, BoundAlias: testAliasA},
			VaultOpReadIdentity, testAliasB, false, DenyAliasMismatch},
		{"绑定主体写他人 alias 同拒（D9 读写同锚）", VaultPrincipal{Name: "student-app", Role: RoleVaultWriter, BoundAlias: testAliasA},
			VaultOpWriteIdentity, testAliasB, false, DenyAliasMismatch},
		{"非法绑定值 fail-closed 拒", VaultPrincipal{Name: "student-app", Role: RoleVaultReader, BoundAlias: "junk"},
			VaultOpReadIdentity, testAliasA, false, DenyAliasMismatch},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := access.Authorize(tc.p, tc.op, tc.alias)
			if got.Allowed != tc.wantAllow || got.Reason != tc.wantWhy {
				t.Fatalf("判定不符: got %+v, want allow=%v reason=%q", got, tc.wantAllow, tc.wantWhy)
			}
			if got.Allowed {
				return
			}
			// deny 必须是纯结论：反复判定同一输入结论不变（判定可复核的前提）.
			if again := access.Authorize(tc.p, tc.op, tc.alias); again != got {
				t.Fatalf("判定不确定: %+v vs %+v", again, got)
			}
		})
	}
}

// ────────────────────────────────────────────────────────────────────
// 二、独立事务双走向（验收 #2/#3 核心）——spy 注入失败与执行面记账
// ────────────────────────────────────────────────────────────────────

// fakeExecutor 是 Executor 的空壳实现：Exec/Query 按配置返回错误，仅用于
// 验证「服务把哪个执行面交给了哪个存储面」（路由记账靠 vaultStoreSpy）.
type fakeExecutor struct {
	name    string
	execErr error
}

func (f *fakeExecutor) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, f.execErr
}

func (f *fakeExecutor) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, f.execErr
}

func (f *fakeExecutor) QueryRow(context.Context, string, ...any) pgx.Row {
	return failingRow{err: f.execErr}
}

// failingRow 是恒失败的 pgx.Row（无行场景/驱动错误注入）.
type failingRow struct{ err error }

func (r failingRow) Scan(...any) error { return r.err }

// vaultStoreSpy 包装内存实现：记录每个存储面调用收到的执行面（路由记账），
// 并可按面注入失败——独立事务双走向的验证锚点.
type vaultStoreSpy struct {
	*MemoryVaultStore

	mu            sync.Mutex
	businessFaces []Executor // 业务面调用（WriteIdentity/ReadIdentity）收到的执行面
	auditFaces    []Executor // 审计面调用（AppendAccessLog）收到的执行面

	failAudit    bool // 下一次审计追加强制失败（一次性注入）
	failAuditAll bool // 持续失败模式（审计面整体宕机）
	corruptNext  bool // 下一次读返回篡改后的密文（解密失败注入）
}

func newVaultStoreSpy() *vaultStoreSpy {
	return &vaultStoreSpy{MemoryVaultStore: NewMemoryVaultStore()}
}

func (s *vaultStoreSpy) WriteIdentity(ctx context.Context, q Executor, row IdentityCiphertext) error {
	s.mu.Lock()
	s.businessFaces = append(s.businessFaces, q)
	s.mu.Unlock()
	return s.MemoryVaultStore.WriteIdentity(ctx, q, row)
}

func (s *vaultStoreSpy) ReadIdentity(ctx context.Context, q Executor, alias [16]byte) (*IdentityCiphertext, error) {
	s.mu.Lock()
	s.businessFaces = append(s.businessFaces, q)
	corrupt := s.corruptNext
	s.mu.Unlock()
	row, rerr := s.MemoryVaultStore.ReadIdentity(ctx, q, alias)
	if rerr != nil || !corrupt {
		return row, rerr
	}
	// 篡改密文首字节（GCM 认证必失败）——解密失败路径的注入点.
	row.NameCiphertext[0] ^= 0xFF
	return row, nil
}

func (s *vaultStoreSpy) AppendAccessLog(ctx context.Context, q Executor, entry AccessLogEntry) error {
	s.mu.Lock()
	s.auditFaces = append(s.auditFaces, q)
	fail := s.failAudit || s.failAuditAll
	s.mu.Unlock()
	if fail {
		return auditSinkDown
	}
	return s.MemoryVaultStore.AppendAccessLog(ctx, q, entry)
}

func (s *vaultStoreSpy) auditCount(t *testing.T) int {
	t.Helper()
	return len(s.AllAudit(t))
}

func (s *vaultStoreSpy) recordedBusinessFaces() []Executor {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Executor, len(s.businessFaces))
	copy(out, s.businessFaces)
	return out
}

func (s *vaultStoreSpy) recordedAuditFaces() []Executor {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Executor, len(s.auditFaces))
	copy(out, s.auditFaces)
	return out
}

// TestReadBusinessSuccessAuditFailureNotRolledBack 是双走向之一（验收 #2）：
// 业务读成功 + 审计写失败 → 身份照常交付（审计失败不回滚业务）且错误以
// ErrAuditDurability 可感知（X12 禁静默吞）；执行面路由为业务=业务面、
// 审计=独立审计面.
func TestReadBusinessSuccessAuditFailureNotRolledBack(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)

	bizFace := &fakeExecutor{name: "reader-tx"}
	auditFace := &fakeExecutor{name: "audit-writer-tx"}
	ctx := context.Background()
	seedIdentity(t, svc, bizFace, auditFace, testAliasA)
	baseAudit := spy.auditCount(t) // 种子写入自身也留痕（每次访问一条）

	spy.mu.Lock()
	spy.failAudit = true
	spy.mu.Unlock()

	identity, err := svc.ReadIdentity(ctx, bizFace, auditFace, readerPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA, Accessor: "support", Purpose: "parent_phone_lookup",
	})

	// 双非空：身份交付（审计失败不回滚业务）+ 错误可感知（X12）.
	if identity == nil {
		t.Fatal("审计失败不得吞掉业务结果（返回值身份应为非 nil）")
	}
	if identity.Name != markerName || identity.Phone != markerPhone {
		t.Fatalf("交付的明文不符: %+v", identity)
	}
	if !errors.Is(err, ErrAuditDurability) {
		t.Fatalf("审计失败须以 ErrAuditDurability 可感知: %v", err)
	}
	if !errors.Is(err, auditSinkDown) {
		t.Fatalf("原始审计故障证据链不得被吞: %v", err)
	}

	// 业务账完好：密文行仍在（本层没回滚业务——提交/回滚归调用方）.
	if _, err := spy.ReadIdentity(ctx, bizFace, testAliasUUID(t, testAliasA)); err != nil {
		t.Fatalf("业务密文行应原样存在: %v", err)
	}
	// 失败的审计不得伪装成功：审计账没有新行（仍是种子那一行）.
	if got := spy.auditCount(t); got != baseAudit {
		t.Fatalf("失败的审计不得入账: %d → %d", baseAudit, got)
	}
	// 路由记账：业务调用全部拿到业务面，审计调用全部拿到独立审计面.
	for i, f := range spy.recordedBusinessFaces() {
		if f != Executor(bizFace) {
			t.Fatalf("业务调用 %d 收到错误执行面: %v", i, f)
		}
	}
	for i, f := range spy.recordedAuditFaces() {
		if f != Executor(auditFace) {
			t.Fatalf("审计调用 %d 未走独立审计面: %v", i, f)
		}
	}
}

// TestReadBusinessFailureStillAudits 是双走向之二（验收 #3）：业务读失败
// （无此 alias）+ 审计写成功 → 业务错误上交且 failed 留痕存活.
func TestReadBusinessFailureStillAudits(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)

	bizFace := &fakeExecutor{name: "reader-tx"}
	auditFace := &fakeExecutor{name: "audit-writer-tx"}
	ctx := context.Background()

	_, err := svc.ReadIdentity(ctx, bizFace, auditFace, readerPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA, Accessor: "support", Purpose: "parent_phone_lookup",
	})
	if !errors.Is(err, ErrIdentityNotFound) {
		t.Fatalf("无记录应报 ErrIdentityNotFound: %v", err)
	}

	entries := spy.MustListAudit(t, testAliasA)
	if len(entries) != 1 {
		t.Fatalf("业务失败也必须恰好留痕一条: %d", len(entries))
	}
	if entries[0].Purpose != "failed:read_identity:not_found" {
		t.Fatalf("failed 留痕 purpose 结构不符: %q", entries[0].Purpose)
	}
	if entries[0].Accessor != "support" {
		t.Fatalf("留痕「谁」缺失: %+v", entries[0])
	}
}

// TestWriteDuplicateBusinessFailureStillAudits：写路径业务失败（重复 alias
// PK 冲突）同样留 failed 痕——「业务失败审计仍留痕」不分读写.
func TestWriteDuplicateBusinessFailureStillAudits(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	bizFace := &fakeExecutor{name: "writer-tx"}
	auditFace := &fakeExecutor{name: "audit-writer-tx"}

	seedIdentity(t, svc, bizFace, auditFace, testAliasA)
	base := len(spy.MemoryVaultStore.MustListAudit(t, testAliasUUID(t, testAliasA)))

	err := svc.WriteIdentity(ctx, bizFace, auditFace, writerPrincipal, VaultWriteRequest{
		StudentAliasID: testAliasA, Name: "x", Phone: "y", Address: "z", ParentContact: "w",
		Accessor: "intake", Purpose: "re_enroll",
	})
	if !errors.Is(err, ErrIdentityExists) {
		t.Fatalf("重复写入应报 ErrIdentityExists: %v", err)
	}

	entries := spy.MemoryVaultStore.MustListAudit(t, testAliasUUID(t, testAliasA))
	if len(entries) != base+1 {
		t.Fatalf("业务失败应新增恰好一条留痕: %d → %d", base, len(entries))
	}
	// 按结论过滤而非按序取尾：同刻多行的时间序依赖时钟粒度，不可靠.
	var failed *AccessLogEntry
	for i := range entries {
		if entries[i].Purpose == "failed:write_identity:already_exists" {
			failed = &entries[i]
		}
	}
	if failed == nil {
		t.Fatalf("failed 留痕缺失: %+v", entries)
	}
	if failed.Accessor != "intake" {
		t.Fatalf("留痕「谁」缺失: %+v", failed)
	}
}

// TestBusinessRollbackKeepsAudit 是验收 #3 的字面语义：读取身份后回滚业务
// 事务 → access_log 仍存在。内存等效模型：identities 快照恢复=业务事务回滚，
// auditLog 独立账=独立事务执行面——恢复不可能波及审计行（结构性保证，
// 非实现巧合）.
func TestBusinessRollbackKeepsAudit(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	bizFace := &fakeExecutor{name: "reader-tx"}
	auditFace := &fakeExecutor{name: "audit-writer-tx"}

	seedIdentity(t, svc, bizFace, auditFace, testAliasA)

	// 业务事务内读取（审计走独立面落账）.
	identity, err := svc.ReadIdentity(ctx, bizFace, auditFace, readerPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA, Accessor: "support", Purpose: "parent_phone_lookup",
	})
	if err != nil || identity == nil {
		t.Fatalf("读取意外失败: %v / %+v", err, identity)
	}

	// 回滚业务事务：identities 清空恢复（比快照更强的回滚模拟——业务侧一切
	// 归零，含种子行）. 审计账（独立事务面）不被触碰——结构性保证.
	spy.mu.Lock()
	spy.restoreIdentitiesLocked(map[[16]byte]IdentityCiphertext{})
	spy.mu.Unlock()

	// 审计留痕不随业务回滚消失：access_log 仍有两条（写入 allow + 读取 allow）.
	entries := spy.MemoryVaultStore.MustListAudit(t, testAliasUUID(t, testAliasA))
	if len(entries) != 2 {
		t.Fatalf("业务回滚后审计必须仍存在: %d 行", len(entries))
	}
	for _, e := range entries {
		if e.Purpose != "test_seed" && e.Purpose != "parent_phone_lookup" {
			t.Fatalf("留痕 purpose 不符: %q", e.Purpose)
		}
	}
}

// TestDenyIsAuditedFactNoBusinessStatement：deny 也是审计事实（fail-closed）——
// 拒绝不产生任何业务语句，deny 行落审计且活过业务回滚；审计故障不翻转拒绝.
func TestDenyIsAuditedFactNoBusinessStatement(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	bizFace := &fakeExecutor{name: "reader-tx"}
	auditFace := &fakeExecutor{name: "audit-writer-tx"}

	seedIdentity(t, svc, bizFace, auditFace, testAliasA)
	spy.auditFaces = nil
	bizBefore := len(spy.recordedBusinessFaces())

	_, err := svc.ReadIdentity(ctx, bizFace, auditFace, anonPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA, Accessor: "unknown-caller", Purpose: "sneak",
	})
	if !errors.Is(err, ErrVaultAccessDenied) {
		t.Fatalf("未认证读取应被拒: %v", err)
	}
	if !strings.Contains(err.Error(), "unauthenticated") {
		t.Fatalf("拒因应可判读: %v", err)
	}

	// 零业务语句：拒绝没有触碰密文账.
	if got := len(spy.recordedBusinessFaces()); got != bizBefore {
		t.Fatalf("拒绝不得产生业务语句: %d → %d", bizBefore, got)
	}
	// deny 行在独立审计面落账（种子写入自带一条 allow 留痕，按结论过滤）.
	var denies int
	for _, e := range spy.MustListAudit(t, testAliasA) {
		if strings.HasPrefix(e.Purpose, "deny:") {
			denies++
			if e.Purpose != "deny:read_identity:unauthenticated" {
				t.Fatalf("deny 留痕 purpose 不符: %q", e.Purpose)
			}
			if e.Accessor != "unknown-caller" {
				t.Fatalf("deny 留痕「谁」缺失: %+v", e)
			}
		}
	}
	if denies != 1 {
		t.Fatalf("deny 必须是审计事实（恰好一条）: %d", denies)
	}

	// 非法别名拒绝同样留痕（锚定零值 UUID——无可信锚点的显式记号）.
	if _, err := svc.ReadIdentity(ctx, bizFace, auditFace, readerPrincipal, VaultReadRequest{
		StudentAliasID: "not-a-uuid", Accessor: "support",
	}); !errors.Is(err, ErrVaultAccessDenied) {
		t.Fatalf("非法别名应被拒: %v", err)
	}
	zeroEntries := spy.MemoryVaultStore.MustListAudit(t, [16]byte{})
	if len(zeroEntries) != 1 || zeroEntries[0].Purpose != "deny:read_identity:alias_malformed" {
		t.Fatalf("非法别名拒绝留痕不符: %+v", zeroEntries)
	}

	// 审计故障不翻转拒绝（fail-closed）：门不因审计挂了变绿.
	spy.mu.Lock()
	spy.failAuditAll = true
	spy.mu.Unlock()
	if _, err := svc.ReadIdentity(ctx, bizFace, auditFace, anonPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA,
	}); !errors.Is(err, ErrVaultAccessDenied) || !errors.Is(err, ErrAuditDurability) {
		t.Fatalf("审计故障下拒绝仍须成立且审计失败可感知: %v", err)
	}
}

// TestAuditSurvivesRollbackForDeny：deny 留痕同样活过业务回滚（拒绝事实不因
// 业务侧任何操作蒸发）.
func TestAuditSurvivesRollbackForDeny(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	auditFace := &fakeExecutor{name: "audit-writer-tx"}

	if _, err := svc.ReadIdentity(ctx, &fakeExecutor{name: "reader-tx"}, auditFace, anonPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA,
	}); !errors.Is(err, ErrVaultAccessDenied) {
		t.Fatalf("前置拒绝缺失: %v", err)
	}

	spy.mu.Lock()
	spy.restoreIdentitiesLocked(spy.snapshotIdentitiesLocked()) // 任意业务回滚
	spy.mu.Unlock()

	if got := spy.auditCount(t); got != 1 {
		t.Fatalf("deny 留痕必须活过业务回滚: %d", got)
	}
}

// TestFacesMustBeIndependentAndPresent：双执行面前置——任一缺失 fail-closed；
// 同执行面双传即审计耦合缺陷，能检则拒.
func TestFacesMustBeIndependentAndPresent(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	face := &fakeExecutor{name: "one-and-only"}
	req := VaultReadRequest{StudentAliasID: testAliasA}

	if _, err := svc.ReadIdentity(ctx, nil, face, readerPrincipal, req); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("缺业务面须拒: %v", err)
	}
	if _, err := svc.ReadIdentity(ctx, face, nil, readerPrincipal, req); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("缺审计面须拒（无审计面不可访问——留痕没有例外入口）: %v", err)
	}
	if _, err := svc.ReadIdentity(ctx, face, face, readerPrincipal, req); !errors.Is(err, ErrAuditNotIndependent) {
		t.Fatalf("同面双传须拒（审计随业务同生共死=本卡终结的耦合缺陷）: %v", err)
	}
	if err := svc.WriteIdentity(ctx, face, face, writerPrincipal, VaultWriteRequest{StudentAliasID: testAliasA}); !errors.Is(err, ErrAuditNotIndependent) {
		t.Fatalf("写路径同面双传同拒: %v", err)
	}
	// 前置拒绝发生在任何存储语句之前.
	if got := len(spy.recordedBusinessFaces()); got != 0 {
		t.Fatalf("执行面前置失败不得触碰存储: %d", got)
	}
}

// ────────────────────────────────────────────────────────────────────
// 三、加密面：明文不落账、密文损坏 fail-closed、密钥纪律
// ────────────────────────────────────────────────────────────────────

func TestPlaintextNeverAtRestNorInErrors(t *testing.T) {
	spy := newVaultStoreSpy()
	svc := mustVaultService(t, spy)
	ctx := context.Background()
	bizFace := &fakeExecutor{name: "faces"}
	auditFace := &fakeExecutor{name: "audit"}

	seedIdentity(t, svc, bizFace, auditFace, testAliasA)

	// 密文账零明文（test_plaintext_not_on_disk 的内存等效断言）.
	row, err := spy.ReadIdentity(ctx, bizFace, testAliasUUID(t, testAliasA))
	if err != nil {
		t.Fatal(err)
	}
	for _, part := range [][]byte{
		row.NameCiphertext, row.NameNonce, row.PhoneCiphertext, row.PhoneNonce,
		row.AddressCiphertext, row.AddressNonce,
		row.ParentContactCiphertext, row.ParentContactNonce,
	} {
		for _, marker := range []string{markerName, markerPhone, markerAddr, markerPare} {
			if bytes.Contains(part, []byte(marker)) {
				t.Fatal("明文出现在密文账（D7 破防）")
			}
		}
	}

	// 密文被篡改：整体失败 + failed 留痕，错误链零明文零密文材料.
	spy.mu.Lock()
	spy.corruptNext = true
	spy.mu.Unlock()
	_, err = svc.ReadIdentity(ctx, bizFace, auditFace, readerPrincipal, VaultReadRequest{
		StudentAliasID: testAliasA, Accessor: "support", Purpose: "tamper_probe",
	})
	if !errors.Is(err, ErrCiphertextTampered) {
		t.Fatalf("密文损坏须报 ErrCiphertextTampered: %v", err)
	}
	for _, marker := range []string{markerName, markerPhone, markerAddr, markerPare} {
		if strings.Contains(err.Error(), marker) {
			t.Fatalf("明文泄漏进错误链: %v", err)
		}
	}
	entries := spy.MemoryVaultStore.MustListAudit(t, testAliasUUID(t, testAliasA))
	if len(entries) == 0 {
		t.Fatal("密文损坏必须留 failed 痕")
	}
	var tamperAudited bool
	for _, e := range entries {
		if e.Purpose == "failed:read_identity:ciphertext_tampered" {
			tamperAudited = true
		}
	}
	if !tamperAudited {
		t.Fatalf("密文损坏必须留 failed 痕: %+v", entries)
	}

	// 全部审计行零明文（审计行本身必须零 PII）.
	all := spy.AllAudit(t)
	if len(all) < 2 {
		t.Fatalf("审计行数异常: %d", len(all))
	}
	for _, e := range all {
		for _, marker := range []string{markerName, markerPhone, markerAddr, markerPare} {
			if strings.Contains(e.Purpose+e.Accessor, marker) {
				t.Fatalf("审计行携带明文: %+v", e)
			}
		}
	}
}

func TestLoadMasterKeyMatrix(t *testing.T) {
	valid := base64Of(t, testVaultKey)
	cases := []struct {
		name   string
		raw    string
		set    bool
		wantOK bool
	}{
		{"缺失", "", false, false},
		{"非 base64", "!!not-base64!!", true, false},
		{"长度非 32 字节", base64Of(t, bytes.Repeat([]byte{1}, 16)), true, false},
		{"合法 32 字节", valid, true, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			getenv := func(string) string {
				if !tc.set {
					return ""
				}
				return tc.raw
			}
			key, err := LoadMasterKey(getenv)
			if tc.wantOK {
				if err != nil || !bytes.Equal(key, testVaultKey) {
					t.Fatalf("合法密钥加载失败: %v", err)
				}
				return
			}
			if !errors.Is(err, ErrVaultKey) {
				t.Fatalf("配置错误应报 ErrVaultKey: %v", err)
			}
			// 密钥材料不进错误信息（X3）：raw 值本身不得出现在错误文本.
			if tc.raw != "" && strings.Contains(err.Error(), tc.raw) {
				t.Fatalf("密钥材料泄漏进错误: %v", err)
			}
		})
	}

	// 运维初始化辅助：生成的密钥必能通过加载（round trip）.
	generated, err := GenerateMasterKey()
	if err != nil {
		t.Fatal(err)
	}
	key, err := LoadMasterKey(func(string) string { return generated })
	if err != nil || len(key) != vaultKeyBytes {
		t.Fatalf("GenerateMasterKey 产物不可回载: %v", err)
	}
}

func TestCipherRoundTripAndTamper(t *testing.T) {
	plaintext := "round-trip-小明-13800"
	ct, nonce, err := encryptField(plaintext, testVaultKey)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(ct, []byte(plaintext)) {
		t.Fatal("密文含明文")
	}
	got, err := decryptField(ct, nonce, testVaultKey)
	if err != nil || got != plaintext {
		t.Fatalf("round trip 失败: %q / %v", got, err)
	}

	// 篡改一位即整体失败.
	ct[0] ^= 0x01
	if _, err := decryptField(ct, nonce, testVaultKey); !errors.Is(err, ErrCiphertextTampered) {
		t.Fatalf("篡改密文应报 ErrCiphertextTampered: %v", err)
	}
	ct[0] ^= 0x01 // 还原

	// nonce 错 / 密钥错：同罪（认证失败），无部分明文.
	if _, err := decryptField(ct, bytes.Repeat([]byte{9}, vaultNonceBytes), testVaultKey); !errors.Is(err, ErrCiphertextTampered) {
		t.Fatalf("错误 nonce 应报 ErrCiphertextTampered: %v", err)
	}
	if _, err := decryptField(ct, nonce, bytes.Repeat([]byte{7}, vaultKeyBytes)); !errors.Is(err, ErrCiphertextTampered) {
		t.Fatalf("错误密钥应报 ErrCiphertextTampered: %v", err)
	}
	if _, _, err := encryptField(plaintext, bytes.Repeat([]byte{7}, 8)); !errors.Is(err, ErrVaultKey) {
		t.Fatalf("短密钥加密应报 ErrVaultKey: %v", err)
	}
}

// ────────────────────────────────────────────────────────────────────
// 四、装配守卫与 PG 面错误分类（纯函数口径，无 PG）
// ────────────────────────────────────────────────────────────────────

func TestVaultServiceAssemblyGuards(t *testing.T) {
	if _, err := NewVaultService(nil, NewMemoryVaultStore(), testVaultKey); !errors.Is(err, ErrVaultServiceInvalid) {
		t.Fatalf("判定面缺失须拒装配: %v", err)
	}
	if _, err := NewVaultService(NewStaticVaultAccess(), nil, testVaultKey); !errors.Is(err, ErrVaultServiceInvalid) {
		t.Fatalf("存储面缺失须拒装配: %v", err)
	}
	if _, err := NewVaultService(NewStaticVaultAccess(), NewMemoryVaultStore(), bytes.Repeat([]byte{1}, 16)); !errors.Is(err, ErrVaultKey) {
		t.Fatalf("坏密钥须拒装配: %v", err)
	}
	// 装配密钥深拷贝：外部改写原切片不得影响服务所持密钥.
	mutated := bytes.Repeat([]byte{0x5A}, vaultKeyBytes)
	svc2, err := NewVaultService(NewStaticVaultAccess(), NewMemoryVaultStore(), mutated)
	if err != nil {
		t.Fatal(err)
	}
	for i := range mutated {
		mutated[i] = 0
	}
	if !bytes.Equal(svc2.key, testVaultKey) {
		t.Fatalf("装配密钥未隔离（外部可改写服务密钥）: %v", svc2.key)
	}
}

// errRow / errExecutor 已由 failingRow/fakeExecutor 承担；此处补 nil 执行面与
// 23505/ErrNoRows 的 PG 面错误分类（生成层语句不出网，仅测翻译层）.
func TestPGVaultStoreErrorClassification(t *testing.T) {
	store := NewPGVaultStore()
	ctx := context.Background()

	// nil 执行面 fail-closed.
	if err := store.WriteIdentity(ctx, nil, IdentityCiphertext{}); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("PG 写缺执行面须拒: %v", err)
	}
	if _, err := store.ReadIdentity(ctx, nil, [16]byte{}); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("PG 读缺执行面须拒: %v", err)
	}
	if err := store.AppendAccessLog(ctx, nil, AccessLogEntry{}); !errors.Is(err, ErrNoTransaction) {
		t.Fatalf("PG 审计缺执行面须拒: %v", err)
	}

	// 23505 → ErrIdentityExists（原始 PgError 证据链保留）.
	pkErr := &pgconn.PgError{Code: sqlStateUniqueViolation, Message: "duplicate key"}
	werr := store.WriteIdentity(ctx, &fakeExecutor{execErr: pkErr}, IdentityCiphertext{})
	if !errors.Is(werr, ErrIdentityExists) {
		t.Fatalf("23505 应映射 ErrIdentityExists: %v", werr)
	}
	var recovered *pgconn.PgError
	if !errors.As(werr, &recovered) || recovered.Code != sqlStateUniqueViolation {
		t.Fatalf("原始 23505 证据链断裂: %v", werr)
	}

	// 非唯一冲突原样放行，不误报.
	other := store.WriteIdentity(ctx, &fakeExecutor{execErr: &pgconn.PgError{Code: "42P01"}}, IdentityCiphertext{})
	if errors.Is(other, ErrIdentityExists) {
		t.Fatal("非唯一冲突不得误报为已存在")
	}

	// pgx.ErrNoRows → ErrIdentityNotFound（不让驱动哨兵穿透）.
	_, gerr := store.ReadIdentity(ctx, &fakeExecutor{execErr: pgx.ErrNoRows}, [16]byte{})
	if !errors.Is(gerr, ErrIdentityNotFound) {
		t.Fatalf("无行应映射 ErrIdentityNotFound: %v", gerr)
	}
	// 其他查询错误原样 wrap.
	if _, err := store.ReadIdentity(ctx, &fakeExecutor{execErr: &pgconn.PgError{Code: "42501"}}, [16]byte{}); errors.Is(err, ErrIdentityNotFound) {
		t.Fatal("权限错误不得误报为无记录")
	}
}

// ────────────────────────────────────────────────────────────────────
// 五、并发（-race）：每次访问恰好一条留痕
// ────────────────────────────────────────────────────────────────────

// TestVaultConcurrentOpsEveryAccessAudited：读/写/拒三路并发洪峰下，每个
// vault 访问恰好落一条审计（「每次访问必留痕」在并发下无洞），密文账与
// 审计账账实一致.
func TestVaultConcurrentOpsEveryAccessAudited(t *testing.T) {
	store := NewMemoryVaultStore()
	svc := mustVaultService(t, store)
	ctx := context.Background()
	auditFace := &fakeExecutor{name: "audit-writer-tx"}

	base := time.Date(2026, 8, 27, 10, 0, 0, 0, time.UTC)
	seq := 0
	var seqMu sync.Mutex
	svc.now = func() time.Time {
		seqMu.Lock()
		defer seqMu.Unlock()
		seq++
		return base.Add(time.Duration(seq) * time.Millisecond)
	}

	seedIdentity(t, svc, &fakeExecutor{name: "seed"}, auditFace, testAliasA)

	const readers, deniers, writers = 16, 16, 12
	var eg errgroup.Group
	for i := range readers {
		i := i
		eg.Go(func() error {
			id, err := svc.ReadIdentity(ctx, &fakeExecutor{name: fmt.Sprintf("reader-%02d", i)}, auditFace,
				readerPrincipal, VaultReadRequest{StudentAliasID: testAliasA, Accessor: "support", Purpose: "lookup"})
			if err != nil || id == nil {
				return fmt.Errorf("并发读取意外失败: %w", err)
			}
			return nil
		})
	}
	for i := range deniers {
		eg.Go(func() error {
			if _, err := svc.ReadIdentity(ctx, &fakeExecutor{name: fmt.Sprintf("anon-%02d", i)}, auditFace,
				anonPrincipal, VaultReadRequest{StudentAliasID: testAliasA}); !errors.Is(err, ErrVaultAccessDenied) {
				return fmt.Errorf("未认证并发读取应被拒: %w", err)
			}
			return nil
		})
	}
	for i := range writers {
		i := i
		alias := fmt.Sprintf("10ef10ef-58cc-4372-a567-0e02b2c3d4%02x", i)
		eg.Go(func() error {
			err := svc.WriteIdentity(ctx, &fakeExecutor{name: fmt.Sprintf("writer-%02d", i)}, auditFace,
				writerPrincipal, VaultWriteRequest{
					StudentAliasID: alias, Name: markerName, Phone: markerPhone,
					Address: markerAddr, ParentContact: markerPare, Accessor: "intake", Purpose: "enroll",
				})
			if err != nil && !errors.Is(err, ErrIdentityExists) {
				return fmt.Errorf("并发写入意外失败: %w", err)
			}
			return nil
		})
	}
	if err := eg.Wait(); err != nil {
		t.Fatal(err)
	}

	total := readers + deniers + writers
	all := store.AllAudit(t)
	if len(all) != total+1 { // +1：种子写入
		t.Fatalf("每次访问必须恰好一条留痕: got %d want %d", len(all), total+1)
	}
	denies, allows, faileds := 0, 0, 0
	for _, e := range all {
		if e.Accessor == "" || e.Purpose == "" {
			t.Fatalf("审计行存在无主/无由行: %+v", e)
		}
		switch {
		case strings.HasPrefix(e.Purpose, "deny:"):
			denies++
		case strings.HasPrefix(e.Purpose, "failed:"):
			faileds++
		default:
			allows++
		}
	}
	if denies != deniers || faileds != 0 {
		t.Fatalf("留痕结论分布不符: deny=%d failed=%d", denies, faileds)
	}
	// 允许行 = 种子 + 读取 + 写入（重复写入的业务失败行已计入 failed 口径之外
	// 的已存在失败——此处写入别名互异，全部成功）.
	if allows != 1+readers+writers {
		t.Fatalf("allow 行数不符: %d", allows)
	}
	// 密文账：种子 + writers 个新 alias.
	if n := store.identityCount(t); n != 1+writers {
		t.Fatalf("密文账行数不符: %d", n)
	}
	// 读取路径时间线：审计按 accessed_at 升序可还原.
	entries := store.MustListAudit(t, testAliasUUID(t, testAliasA))
	for i := 1; i < len(entries); i++ {
		if entries[i-1].AccessedAt.After(entries[i].AccessedAt) {
			t.Fatal("审计投影时间线乱序")
		}
	}
}

// TestBothVaultImplementationsSatisfyContract 与 ConsentStore 同惯例：两实现
// 调用形态一致，W6 接线可无缝换装.
func TestBothVaultImplementationsSatisfyContract(t *testing.T) {
	var _ VaultStore = (*MemoryVaultStore)(nil)
	var _ VaultStore = (*PGVaultStore)(nil)
}

// ────────────────────────────────────────────────────────────────────
// 测试辅助
// ────────────────────────────────────────────────────────────────────

func testAliasUUID(t *testing.T, s string) [16]byte {
	t.Helper()
	b, ok := parseVaultAlias(s)
	if !ok {
		t.Fatalf("测试别名非法: %q", s)
	}
	return b
}

// MustListAudit 读某 alias 的审计投影（测试断言面）.
func (m *MemoryVaultStore) MustListAudit(t *testing.T, alias [16]byte) []AccessLogEntry {
	t.Helper()
	entries, err := m.ListAccessLog(context.Background(), nil, alias)
	if err != nil {
		t.Fatalf("审计投影读取失败: %v", err)
	}
	return entries
}

// MustListAudit（字符串别名重载 convenience）.
func (s *vaultStoreSpy) MustListAudit(t *testing.T, alias string) []AccessLogEntry {
	t.Helper()
	return s.MemoryVaultStore.MustListAudit(t, testAliasUUID(t, alias))
}

// AllAudit 读全量审计账（同包测试面）.
func (m *MemoryVaultStore) AllAudit(t *testing.T) []AccessLogEntry {
	t.Helper()
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]AccessLogEntry, len(m.auditLog))
	copy(out, m.auditLog)
	return out
}

func (s *vaultStoreSpy) AllAudit(t *testing.T) []AccessLogEntry {
	t.Helper()
	return s.MemoryVaultStore.AllAudit(t)
}

func (m *MemoryVaultStore) identityCount(t *testing.T) int {
	t.Helper()
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.identities)
}

// base64Of 测试辅助：字节面 → 标准 base64.
func base64Of(t *testing.T, b []byte) string {
	t.Helper()
	return base64.StdEncoding.EncodeToString(b)
}
