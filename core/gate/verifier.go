package gate

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏）.
var (
	// ErrNoTransaction 表示调用没有显式事务执行面。D11 fail-closed：门域的证书
	// 验真与失败留痕只接受外层已 begin 的事务，绝不在无事务面上「先验先得」.
	ErrNoTransaction = errors.New("gate: 无显式事务执行面（D11 fail-closed：门域只接受外层已 begin 的事务）")

	// ErrInvalidRequirement 表示发布事务声明的证书用途（持什么证、为哪个产物）
	// 本身违反契约，细分原因见 wrap 文本。契约违例在出 Go 进程前拦截，不烧
	// 事务语句、不给 PG 报错晚到.
	ErrInvalidRequirement = errors.New("gate: 证书验真的使用声明违反契约")

	// ErrUnknownCertificate 表示 cert_id 在 gate_certificate 里无行——无效证书
	// 试图通过发布事务（D2：持合法门证书是发布的物理前提，此路径必须失败）.
	ErrUnknownCertificate = errors.New("gate: 门证书不存在")

	// ErrCertificateMismatch 表示证书存在但与使用声明不一致：证不是签给这个
	// 产物的，或证书类型与本次用途（publish/retire）不符——「有证」不等于
	// 「这张证对这个产物有效」，一致性校验是 D2 的第二半（W6 策略链核验的挂载点）.
	ErrCertificateMismatch = errors.New("gate: 门证书与使用声明不一致")
)

// CertType 证书类型二值域（迁移 0004 ck_gc_cert_type_domain 的同值投影；
// publish=发布放行，retire=退役放行）.
type CertType string

// 证书类型二值（与 DB CHECK 同域，DB 物理约束兜底）.
const (
	CertPublish CertType = "publish"
	CertRetire  CertType = "retire"
)

// ValidCertType 报告 t 是否在证书类型二值域内.
func ValidCertType(t CertType) bool {
	return t == CertPublish || t == CertRetire
}

// Executor 是门域读写所需语句执行面的最小抽象，方法集与生成层 dbgen.DBTX 同构
// （与本仓 core/events、core/estimator 的同名接口同形）。
//
// 为什么不复用他域接口而本地重声明：领域端口按需各自声明最小依赖面，六边形
// 核心域之间不为一个三方法接口建立编译耦合；两者方法集一致，pgx.Tx 与连接池
// 事务面天然同时满足。全部语句文本只住在 db/queries/gate.sql（SQL-2：不在
// Go 拼 SQL），经 sqlc 生成为类型安全的 dbgen 方法，本包仅作调用方——取证只有
// SELECT、留痕只有 INSERT，本包源码不可能发出 UPDATE/DELETE（append-only 三账
// 无查询面可写）.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// 编译期锚定一：pgx.Tx 必须满足 Executor（W6 装配直通的假设防线）.
var _ Executor = (pgx.Tx)(nil)

// 编译期锚定二：Executor 必须满足生成层执行面 dbgen.DBTX——WithTx 内部用
// dbgen.New(tx) 构造类型安全查询器；sqlc 升级改形状时在此第一时间红.
var _ dbgen.DBTX = Executor(nil)

// Verifier 是证书验真的领域端口：门编排（W6 接入）面向本接口编程，不由具体
// 取证实现定型。T-W5-002 落骨架——存在性 + 绑定一致性；W6 扩展点：签名核验、
// 策略链复核等以新实现或装饰实现接入同一接口，调用面零改动.
type Verifier interface {
	Verify(ctx context.Context, certID string, want Requirement) (*Certificate, error)
}

// Requirement 是一次发布/退役事务对所持证书的使用声明：哪张证、给哪个产物、
// 作什么用途。验真即把声明与 gate_certificate 行逐项对表.
type Requirement struct {
	// ArtifactRef 声明该证书服务的目标产物引用（如 item_version_id）；必填.
	ArtifactRef string
	// CertType 声明用途：publish / retire 必须与签发时的 cert_type 一致.
	CertType CertType
}

// Certificate 是验真成功后返回的证书事实投影（发布方据此把 cert_id 写入内容表，
// 外键 fk_*_gate_certificate 在 COMMIT 时验证引用真实存在——0028 补建的物理面）.
type Certificate struct {
	CertID        string
	ArtifactRef   string
	CertType      CertType
	PolicyVersion string
	IssuedBy      string
	IssuedAt      time.Time
}

// CertificateVerifier 是绑定显式事务的门证书验真服务（冻结基线语义：
// src/core/gate/certifier/service.py 只在 pass 时签发证书、发布侧必须持证——
// 本服务承接消费端：验出假证/错证在进入内容写入前失败）。
//
// 存在性校验 = cert_id 在 gate_certificate 有行；一致性校验 = 行的 artifact_ref /
// cert_type 与 Requirement 逐项一致（W2 冻结实现的 issue_certificate 把这两项
// 定型进证书行，因此绑定比对在消费端是完备的验真下界）。
//
// 事务纪律（S4/D11）：Verifier 不持有连接、不自 begin、永不 Commit/Rollback；
// 只读取证可与后续内容写入共享同一外层事务，保证「验真 → 引用」间无并发篡位窗口.
type CertificateVerifier struct {
	tx Executor // 外层已 begin 的执行面；nil 即非事务上下文（fail-closed 拒绝）
	qs *dbgen.Queries
}

// NewCertificateVerifier 把调用方已 begin 的显式事务执行面绑定为证书验真器。
// tx 允许 nil——构造不报错，但所有 Verify 调用立即返回 ErrNoTransaction：
// fail-closed 落在验证路径而非构造路径（与 core/events.WithTx 同惯例）.
func NewCertificateVerifier(tx Executor) *CertificateVerifier {
	return &CertificateVerifier{tx: tx, qs: dbgen.New(tx)}
}

// 编译期锚定：实现即端口（W6 门编排可无损替换/装饰）.
var _ Verifier = (*CertificateVerifier)(nil)

// Verify 按 cert_id 取证书行并对照 want 做存在性 + 一致性验真，通过即返回证书
// 事实投影。任何失败返回 nil 证书与非 nil 错误——调用方必须放弃本次发布/退役.
//
// 判定序（契约违例先行于 IO，测试面按此序锁定）：
//  1. 无显式事务面 → ErrNoTransaction；
//  2. 声明本身非法（certID 空 / ArtifactRef 空 / CertType 出域）→ ErrInvalidRequirement；
//  3. 无证书行 → ErrUnknownCertificate（驱动 ErrNoRows 归一）；
//  4. 类型不一致 / 产物绑定不一致 → ErrCertificateMismatch（wrap 文本带两侧实况）.
//
// 其余驱动错误原样 wrap 放行——DB 层故障与「假证」两类失败的处置路径不同，
// 不许吞并混报.
func (v *CertificateVerifier) Verify(ctx context.Context, certID string, want Requirement) (*Certificate, error) {
	if v == nil || v.tx == nil {
		return nil, ErrNoTransaction
	}
	if err := want.validate(certID); err != nil {
		return nil, err
	}
	row, err := v.qs.GetGateCertificate(ctx, certID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("%w: cert_id=%q 在 gate_certificate 无行（D2：无效证书不得进入发布事务）",
				ErrUnknownCertificate, certID)
		}
		return nil, fmt.Errorf("gate: get gate_certificate: %w", err)
	}

	got := Certificate{
		CertID:        row.CertID,
		ArtifactRef:   row.ArtifactRef,
		CertType:      CertType(row.CertType),
		PolicyVersion: row.PolicyVersion,
		IssuedBy:      row.IssuedBy,
		IssuedAt:      issuedAt(row.IssuedAt),
	}
	if got.CertType != want.CertType {
		return nil, fmt.Errorf("%w: cert_id=%q 类型 %q ≠ 用途 %q", ErrCertificateMismatch,
			certID, got.CertType, want.CertType)
	}
	if got.ArtifactRef != want.ArtifactRef {
		return nil, fmt.Errorf("%w: cert_id=%q 绑定产物 %q ≠ 声明产物 %q（证不是签给这个产物的）",
			ErrCertificateMismatch, certID, got.ArtifactRef, want.ArtifactRef)
	}
	return &got, nil
}

// validate 前置拦截使用声明违例：空引用与出域类型在本进程内失败，不发 SQL.
func (r Requirement) validate(certID string) error {
	if certID == "" {
		return fmt.Errorf("%w: cert_id 不能为空", ErrInvalidRequirement)
	}
	if r.ArtifactRef == "" {
		return fmt.Errorf("%w: artifact_ref 不能为空", ErrInvalidRequirement)
	}
	if !ValidCertType(r.CertType) {
		return fmt.Errorf("%w: cert_type %q 不在 publish/retire 二值域内", ErrInvalidRequirement, r.CertType)
	}
	return nil
}

// issuedAt 收敛生成层时间戳：NULL/零值统一为零时刻——0004 迁移对 issued_at 挂了
// DEFAULT now() NOT NULL，Valid=false 只可能出现在测试替身里，不为它编造语义.
func issuedAt(ts pgtype.Timestamptz) time.Time {
	if !ts.Valid {
		return time.Time{}
	}
	return ts.Time
}
