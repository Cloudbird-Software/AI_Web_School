package compliance

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// ────────────────────────────────────────────────────────────────────
// T-W5-012：PII 保险库权限模型与审计独立事务（Python 冻结实现
// src/core/compliance/pii_encryption.py 读写面的 Go 重锚定）。
//
// 本卡终结冻结实现的两个缺陷：
//
//  1. 权限模型缺位：冻结实现把「有没有权限」完全交给 DB 角色授权，应用层
//     无显式判定面——主体是谁、持什么角色、能否碰这个 alias，在 Go 进程内
//     不可判定也不可审计。本卡把 vault 访问面收敛为 VaultAccess 显式角色
//     判定：principal → (operation, alias) → allow/deny，fail-closed——
//     未认证/角色缺失/别名不匹配/别名非法一律 deny；deny 也是审计事实
//     （D7「每次访问必留痕」含被拒的访问；X12 精神：合规失败绝无降级放行）。
//
//  2. 审计与业务同事务：冻结实现 read_identity 在业务会话内写 access_log，
//     业务回滚时「每次读必留痕」的 D7 承诺静默失效。本卡以双 Executor 注入
//     把两条事务边界在类型签名上分开：
//     业务面 q（读身份/写身份）与审计面 auditQ（access_log 追加）必须是两个
//     独立事务（独立连接/独立 begin）。事务边界方向与主链（D11 单事务）相反，
//     为什么：
//     - 主链单事务为的是业务一致性——半写状态不可见（作答事件与会话同进退）；
//     - vault 审计为的是合规留痕——「有人试图/成功访问了 PII」是独立于业务
//       结果的事实，业务回滚改变不了「访问发生过」，留痕必须活过业务回滚；
//     - 因此审计写入绝不在业务事务内（否则随业务回滚一起蒸发），业务失败
//       也照样留痕（failed 行）；反过来审计写失败不回滚业务（本层不持有
//       事务、永不 Rollback），但错误必须上交调用方（X12：禁止静默吞），
//       读取场景表现为「身份与错误同时非 nil」的显式双面语义。
//
// 宪法 A5/X6：本包是核心域，禁止 import 任何学科/学段包。
// ────────────────────────────────────────────────────────────────────

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配（异常不泄漏；全部
// 错误文本零 PII——直标识明文永不进错误链，alias 为非 PII 匿名锚点可出现）.
var (
	// ErrVaultAccessDenied 表示 VaultAccess 判定拒绝（D9 最小权限，fail-closed）。
	// 拒因枚举见 DenyReason；拒绝同时已落审计（deny 行），调用方无需也无法重试
	// 绕过——换主体/换角色是唯一正道.
	ErrVaultAccessDenied = errors.New("compliance: PII 保险库访问被拒")

	// ErrAuditNotIndependent 表示业务面与审计面收到同一个执行面——审计将随
	// 业务事务同生共死，正是本卡要终结的冻结实现耦合缺陷。能检则拒：同类型
	// 且可比较的执行面相等即拒绝（不同连接/不同事务天然不等）.
	ErrAuditNotIndependent = errors.New("compliance: 业务与审计收到同一执行面（审计必须走独立事务/独立连接）")

	// ErrIdentityNotFound 表示 pii_vault.student_identity 无该 alias 的直标识
	// 记录。对应冻结实现 StudentIdentityNotFoundError.
	ErrIdentityNotFound = errors.New("compliance: PII 保险库中无该 student_alias_id 的直标识记录")

	// ErrIdentityExists 表示直标识已存在（PK 冲突 23505）：PII 记录一次写入后
	// 不可改写，变更走新 alias（与冻结实现 write_identity 的 PK 冲突语义同构，
	// 在出 Go 进程前给出可判定错误而非让驱动异常穿透）.
	ErrIdentityExists = errors.New("compliance: PII 直标识已存在（一次写入后不可改写，变更走新 alias）")

	// ErrAuditDurability 表示审计写入失败（X12：审计失败必须可感知，禁止静默
	// 吞掉）。业务结果不受其回滚（本层不持有业务事务），但返回值必然携带本
	// 错误——调用方必须告警/重试补偿，而不是当成功继续.
	ErrAuditDurability = errors.New("compliance: PII 访问审计写入失败")

	// ErrVaultServiceInvalid 表示 VaultService 装配参数缺失（判定面/存储面/
	// 主密钥任一缺失即拒绝装配——权限模型缺位的服务不许存在）.
	ErrVaultServiceInvalid = errors.New("compliance: PII 保险库服务装配参数非法")
)

// VaultRole 是 vault 访问主体的角色二值域（与 0030 迁移创建的 DB 角色
// pii_vault_reader / pii_vault_writer 一一对应：应用层判定面是 DB 授权的
// 镜像而非替代——两层任一拒绝即不可达，应用层先拒可审计、DB 层兜底防绕过）.
type VaultRole string

const (
	// RoleVaultReader 读身份（SELECT student_identity + SELECT access_log 审计复核）.
	RoleVaultReader VaultRole = "pii_vault_reader"
	// RoleVaultWriter 写身份+写审计（INSERT student_identity / INSERT access_log）.
	RoleVaultWriter VaultRole = "pii_vault_writer"
)

// VaultOperation 是 vault 访问操作二值域.
type VaultOperation string

const (
	// VaultOpReadIdentity 读取直标识（含解密；每次调用必落一条审计）.
	VaultOpReadIdentity VaultOperation = "read_identity"
	// VaultOpWriteIdentity 写入直标识（加密落库；每次调用必落一条审计）.
	VaultOpWriteIdentity VaultOperation = "write_identity"
)

// DenyReason 是机器可判的拒因枚举（审计 purpose 结构化文本的语料，只含枚举
// 词不含主体数据——审计行本身必须零 PII）.
type DenyReason string

const (
	// DenyUnauthenticated 未认证（主体名为空——D9：无主体不存在数据访问）.
	DenyUnauthenticated DenyReason = "unauthenticated"
	// DenyRoleMissing 未持任何 vault 角色（角色为空/未知值）.
	DenyRoleMissing DenyReason = "role_missing"
	// DenyRoleMismatch 角色与操作不匹配（reader 不能写 / writer 不能读）.
	DenyRoleMismatch DenyReason = "role_not_authorized"
	// DenyAliasMismatch 别名与主体绑定不符（D9：学生主体只能触达自身 alias）.
	DenyAliasMismatch DenyReason = "alias_mismatch"
	// DenyAliasMalformed 别名不是合法 UUID（格式违例在判定层拦截，不给
	// 存储层错误晚到机会）.
	DenyAliasMalformed DenyReason = "alias_malformed"
)

// VaultDecision 是一次权限判定的结论：Allowed=false 时 Reason 给出机器可判
// 拒因（判定只输出结论与枚举，不回显主体数据）.
type VaultDecision struct {
	Allowed bool
	Reason  DenyReason
}

// VaultPrincipal 是经认证的 vault 访问主体（D9：主体 + 角色 + 可选别名绑定）.
type VaultPrincipal struct {
	// Name 认证主体标识（服务账号/运维身份）；空即未认证.
	Name string
	// Role 主体持有的 vault 角色（装配层按 DB membership 注入）；空/未知即无角色.
	Role VaultRole
	// BoundAlias 学生主体绑定的自身 alias（D9 最小权限）；空表示角色主体
	// （服务/运维）不限于特定学生。绑定值非法（非 UUID）时对任何 alias 都
	// 判 mismatch——fail-closed 不猜.
	BoundAlias string
}

// VaultAccess 是 vault 权限判定的语义端口：principal → (operation, alias) →
// allow/deny。实现必须 fail-closed——一切无法确证允许的情形（含输入缺失、
// 未知角色、未知操作）一律 deny；判定必须纯函数化（无 IO 无随机），同一输入
// 永远同一结论，deny 结论因此可被测试与审计复核.
type VaultAccess interface {
	Authorize(p VaultPrincipal, op VaultOperation, alias string) VaultDecision
}

// 编译期锚定：默认实现必须兑现 VaultAccess 契约.
var _ VaultAccess = (*StaticVaultAccess)(nil)

// StaticVaultAccess 是 VaultAccess 的默认实现：固定角色矩阵
// （reader→读 / writer→写），与 0030 的 DB 授权形态逐条同构.
//
// 判定序（确定性优先级，从身份到资源）：
// 未认证 → 角色缺失 → 别名非法 → 别名不匹配 → 操作-角色矩阵。
// 任何一步不满足即返回 deny，绝不回落放行.
type StaticVaultAccess struct{}

// NewStaticVaultAccess 构造默认角色矩阵判定器.
func NewStaticVaultAccess() *StaticVaultAccess { return &StaticVaultAccess{} }

// Authorize 实现 VaultAccess：固定角色矩阵 + fail-closed（判定序见类型注释）.
func (StaticVaultAccess) Authorize(p VaultPrincipal, op VaultOperation, alias string) VaultDecision {
	if p.Name == "" {
		return VaultDecision{Reason: DenyUnauthenticated}
	}
	switch p.Role {
	case RoleVaultReader, RoleVaultWriter:
		// 角色在域内，继续.
	default:
		return VaultDecision{Reason: DenyRoleMissing}
	}
	aliasBytes, ok := parseVaultAlias(alias)
	if !ok {
		return VaultDecision{Reason: DenyAliasMalformed}
	}
	if p.BoundAlias != "" {
		bound, ok := parseVaultAlias(p.BoundAlias)
		// 绑定值非法不猜（fail-closed）：视为与任何请求别名都不匹配.
		if !ok || bound != aliasBytes {
			return VaultDecision{Reason: DenyAliasMismatch}
		}
	}
	switch {
	case op == VaultOpReadIdentity && p.Role == RoleVaultReader:
		return VaultDecision{Allowed: true}
	case op == VaultOpWriteIdentity && p.Role == RoleVaultWriter:
		return VaultDecision{Allowed: true}
	default:
		// 含未知操作值：没有任何角色承载，一律拒.
		return VaultDecision{Reason: DenyRoleMismatch}
	}
}

// parseVaultAlias 校验并解析 alias（UUID 十六字节）：存储参数与权限判定共用
// 同一实现——「别名合法」的判据单一来源，两处不可能漂移.
func parseVaultAlias(s string) ([16]byte, bool) {
	var u pgtype.UUID
	if err := u.Scan(s); err != nil || !u.Valid {
		return [16]byte{}, false
	}
	return u.Bytes, true
}

// ────────────────────────────────────────────────────────────────────
// 存储契约（密文面）：明文只住在 VaultService 的加解密瞬间，存储层只见密文.
// ────────────────────────────────────────────────────────────────────

// IdentityCiphertext 是 pii_vault.student_identity 一行的密文形态（明文
// 不进存储层：加密由 VaultService 在写入前完成，解密在读出后完成）.
type IdentityCiphertext struct {
	StudentAliasID          [16]byte
	NameCiphertext          []byte
	NameNonce               []byte
	PhoneCiphertext         []byte
	PhoneNonce              []byte
	AddressCiphertext       []byte
	AddressNonce            []byte
	ParentContactCiphertext []byte
	ParentContactNonce      []byte
	// CreatedAt 仅内存实现承载（PG 由列默认 now() 填充，读出时带回）.
	CreatedAt time.Time
}

// AccessLogEntry 是 pii_vault.access_log 一行（审计五列同构）。Purpose 语义：
// 允许行 = 调用方申报用途；拒绝/失败行 = 结构化文本「deny:<op>:<拒因>」/
// 「failed:<op>:<细节>」（零 DDL 承载审计结论，格式常量见 auditPurpose）.
type AccessLogEntry struct {
	AccessID       [16]byte
	StudentAliasID [16]byte
	Accessor       string
	AccessedAt     time.Time
	Purpose        string
}

// VaultStore 是 PII 保险库的存储契约（密文面）。两实现（内存/PG）对同一输入
// 必然给出同一条哨兵错误（判据单一来源），方法集只有 INSERT/SELECT——
// append-only 无更新面可写.
type VaultStore interface {
	// WriteIdentity 追加直标识密文行；alias 已存在时报 ErrIdentityExists.
	WriteIdentity(ctx context.Context, q Executor, row IdentityCiphertext) error
	// ReadIdentity 取直标识密文行；无记录时报 ErrIdentityNotFound.
	ReadIdentity(ctx context.Context, q Executor, alias [16]byte) (*IdentityCiphertext, error)
	// AppendAccessLog 追加一条访问审计（审计独立事务执行面上调用）.
	AppendAccessLog(ctx context.Context, q Executor, entry AccessLogEntry) error
	// ListAccessLog 按 accessed_at 升序返回该 alias 的审计只读投影.
	ListAccessLog(ctx context.Context, q Executor, alias [16]byte) ([]AccessLogEntry, error)
}

// 编译期锚定：两种实现都必须兑现 VaultStore 契约（与 ConsentStore 同惯例）.
var (
	_ VaultStore = (*MemoryVaultStore)(nil)
	_ VaultStore = (*PGVaultStore)(nil)
)

// ────────────────────────────────────────────────────────────────────
// 请求/输出面
// ────────────────────────────────────────────────────────────────────

// 审计字段回落值（与冻结实现 read_identity 的默认参同值：accessor="unknown"、
// purpose="unspecified"）——审计两列必非空，留痕不存在无主行.
const (
	defaultAccessor = "unknown"
	defaultPurpose  = "unspecified"
)

// VaultReadRequest 是一次直标识读取请求（主体经参数显式传入，不藏在请求里）.
type VaultReadRequest struct {
	// StudentAliasID 目标学生的匿名别名（非 PII 锚点，可进错误/审计）.
	StudentAliasID string
	// Accessor 调用方服务/主体标识（审计「谁」）；空回落 defaultAccessor.
	Accessor string
	// Purpose 本次访问用途（审计「为何」，如 "support_call"/"parent_report"）；
	// 空回落 defaultPurpose.
	Purpose string
}

// VaultWriteRequest 是一次直标识写入请求（明文字段只在服务内存在到加密完成）.
type VaultWriteRequest struct {
	StudentAliasID string
	// Name/Phone/Address/ParentContact 直标识明文（D7：只允许经 AES-256-GCM
	// 加密后落库；禁止出现在任何日志/错误/prompt，X3）.
	Name          string
	Phone         string
	Address       string
	ParentContact string
	Accessor      string
	Purpose       string
}

// StudentIdentity 是学生直标识明文 DTO（仅内存短暂存在：出服务即由调用方
// 按需使用，禁止序列化落盘/写日志/进 LLM prompt——D7/X3）.
type StudentIdentity struct {
	StudentAliasID string
	Name           string
	Phone          string
	Address        string
	ParentContact  string
	CreatedAt      time.Time
}

// ────────────────────────────────────────────────────────────────────
// VaultService：权限判定 → 业务读写 → 独立事务审计 的唯一编排面
// ────────────────────────────────────────────────────────────────────

// VaultService 是 PII 保险库的领域服务：每次 vault 访问先经 VaultAccess
// fail-closed 判定，业务语句走业务执行面 q，审计留痕走独立审计执行面
// auditQ（双 Executor 注入；两执行面必须属于两个独立事务）。
//
// 审计独立事务语义（本卡验收核心，与主链 D11 单事务边界方向相反，理由见
// 文件头注释）：
//   - 业务成功 → 审计 allow 行落 auditQ；审计写失败不回滚业务（本层永不
//     Rollback），但错误以 ErrAuditDurability 上交——读取场景返回值身份与
//     错误同时非 nil，调用方必须检查 err；
//   - 业务失败（无记录/密文损坏/写入冲突）→ failed 审计行照样落 auditQ
//     （「业务失败审计仍留痕」），业务错误与审计错误 errors.Join 上交；
//   - deny → 不产生任何业务语句，deny 审计行落 auditQ，拒绝不可被审计故障
//     翻转（fail-closed：审计失败也返回 ErrVaultAccessDenied）。
//
// 事务纪律：本类型不持有连接、不自 begin/commit/rollback；两个执行面的
// 事务边界都归最外层调用方。
type VaultService struct {
	access VaultAccess
	store  VaultStore
	// key 主密钥（装配时深拷贝隔离，外部改不动判定所用的密钥字节）.
	key   []byte
	now   func() time.Time
	newID func() ([16]byte, error)
}

// NewVaultService 装配 vault 服务：判定面/存储面/主密钥任一缺失即拒绝装配
// （ErrVaultServiceInvalid/ErrVaultKey）——权限模型缺位的服务不许存在.
func NewVaultService(access VaultAccess, store VaultStore, key []byte) (*VaultService, error) {
	if access == nil || store == nil {
		return nil, fmt.Errorf("%w: 权限判定面与存储面缺一不可", ErrVaultServiceInvalid)
	}
	if len(key) != vaultKeyBytes {
		return nil, fmt.Errorf("%w: 装配密钥长度 %d 字节，预期 %d（AES-256）", ErrVaultKey, len(key), vaultKeyBytes)
	}
	keyCopy := make([]byte, len(key))
	copy(keyCopy, key)
	return &VaultService{
		access: access,
		store:  store,
		key:    keyCopy,
		now:    time.Now,
		newID:  randomUUIDV4,
	}, nil
}

// ReadIdentity 读取并解密直标识（每次调用恰好落一条审计：allow/deny/failed）。
//
// q 为业务执行面（读身份；0030 后对应 pii_vault_reader 角色连接的已 begin
// 事务），auditQ 为独立审计执行面（pii_vault_writer 角色连接的独立事务）。
// 两面缺一（nil）返回 ErrNoTransaction；同面双传返回 ErrAuditNotIndependent。
//
// 返回值双面语义：业务成功且审计也成功 → (identity, nil)；业务成功但审计
// 失败 → (identity, ErrAuditDurability wrap)——身份照常交付（审计失败不回滚
// 业务）但调用方必须感知（X12）；业务失败 → (nil, 业务错误[+审计错误]).
func (s *VaultService) ReadIdentity(ctx context.Context, q, auditQ Executor, p VaultPrincipal, req VaultReadRequest) (*StudentIdentity, error) {
	if err := s.checkFaces(q, auditQ); err != nil {
		return nil, err
	}
	alias, _ := parseVaultAlias(req.StudentAliasID)
	decision := s.access.Authorize(p, VaultOpReadIdentity, req.StudentAliasID)
	if !decision.Allowed {
		return nil, s.deny(ctx, auditQ, alias, req.StudentAliasID, req.Accessor, VaultOpReadIdentity, decision)
	}

	row, err := s.store.ReadIdentity(ctx, q, alias)
	if err != nil {
		return nil, s.failAudit(ctx, auditQ, alias, req.Accessor, VaultOpReadIdentity, failCodeOfRead(err), err)
	}
	identity, err := s.decryptRow(alias, row)
	if err != nil {
		// 密文损坏也是一次「读已发生」：failed 留痕后整体失败，无部分明文.
		return nil, s.failAudit(ctx, auditQ, alias, req.Accessor, VaultOpReadIdentity, "ciphertext_tampered", err)
	}

	// 业务成功 → allow 审计；审计失败不回滚业务：身份照常交付，错误同时上交
	// （调用方必须检查 err——双非空是本卡的显式契约而非 Go 反模式）.
	if err := s.appendAudit(ctx, auditQ, alias, req.Accessor, purposeOf(req.Purpose)); err != nil {
		return identity, err
	}
	return identity, nil
}

// WriteIdentity 加密并写入直标识（每次调用恰好落一条审计）。
//
// q 为业务执行面（写身份；pii_vault_writer 角色连接的已 begin 事务），auditQ
// 为独立审计执行面（独立事务）。业务插入失败（含重复 alias 的
// ErrIdentityExists）照样留 failed 痕再上交；业务成功后审计失败返回
// ErrAuditDurability wrap（业务写入不被本层回滚，提交决策归调用方）.
func (s *VaultService) WriteIdentity(ctx context.Context, q, auditQ Executor, p VaultPrincipal, req VaultWriteRequest) error {
	if err := s.checkFaces(q, auditQ); err != nil {
		return err
	}
	alias, _ := parseVaultAlias(req.StudentAliasID)
	decision := s.access.Authorize(p, VaultOpWriteIdentity, req.StudentAliasID)
	if !decision.Allowed {
		return s.deny(ctx, auditQ, alias, req.StudentAliasID, req.Accessor, VaultOpWriteIdentity, decision)
	}

	row, err := s.encryptRow(alias, req)
	if err != nil {
		return s.failAudit(ctx, auditQ, alias, req.Accessor, VaultOpWriteIdentity, "encrypt_error", err)
	}
	if err := s.store.WriteIdentity(ctx, q, row); err != nil {
		return s.failAudit(ctx, auditQ, alias, req.Accessor, VaultOpWriteIdentity, failCodeOfWrite(err), err)
	}
	// 业务成功 → allow 审计；审计失败可感知但不回滚业务.
	return s.appendAudit(ctx, auditQ, alias, req.Accessor, purposeOf(req.Purpose))
}

// ListAccessLog 审计账只读投影（读身份角色即可复核审计完整性——0030 补的
// SELECT access_log 能力的应用面）。属于审计复核面，本身不再留痕.
func (s *VaultService) ListAccessLog(ctx context.Context, q Executor, alias [16]byte) ([]AccessLogEntry, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	return s.store.ListAccessLog(ctx, q, alias)
}

// checkFaces 双执行面前置：任一缺失 fail-closed 拒绝（无审计面的访问不可
// 发生——「每次访问必留痕」没有例外入口）；同面双传即审计耦合缺陷，能检则拒.
func (s *VaultService) checkFaces(q, auditQ Executor) error {
	if q == nil || auditQ == nil {
		return fmt.Errorf("%w: 业务执行面与审计执行面缺一不可", ErrNoTransaction)
	}
	if sameExecutor(q, auditQ) {
		return ErrAuditNotIndependent
	}
	return nil
}

// sameExecutor 判定两个执行面是否同一对象：仅当动态类型一致且可比较时做
// 相等比较（指针型事务面天然满足），不可比较类型不猜不比较——守卫只拦能
// 确证的耦合，绝不 panic.
func sameExecutor(a, b Executor) bool {
	ta, tb := reflect.TypeOf(a), reflect.TypeOf(b)
	if ta != tb || ta == nil || !ta.Comparable() {
		return false
	}
	return a == b
}

// deny 拒绝路径：先落 deny 审计（拒绝本身是审计事实），再返回拒绝错误。
// 审计失败不翻转拒绝结论（fail-closed：审计故障只会让错误信息更响，不会让
// 门变绿），两错误 errors.Join 同交.
func (s *VaultService) deny(ctx context.Context, auditQ Executor, alias [16]byte, rawAlias, accessor string, op VaultOperation, d VaultDecision) error {
	// 错误回显调用方原始书写（非 PII 匿名锚点可进错误）；别名非法时审计行
	// 锚定零值 UUID（parseVaultAlias 失败即零值）——「无可信锚点」的显式记号，
	// 结构化 purpose 已携带拒因.
	denyErr := fmt.Errorf("%w: op=%s reason=%s alias=%s", ErrVaultAccessDenied, op, d.Reason, rawAlias)
	aerr := s.appendAudit(ctx, auditQ, alias, accessor, auditPurpose("deny", op, string(d.Reason)))
	return errors.Join(denyErr, aerr)
}

// failAudit 业务失败路径：failed 审计照样落（「业务失败审计仍留痕」），业务
// 错误与审计错误 Join 上交——aerr 为 nil 时 Join 只返回业务错误.
func (s *VaultService) failAudit(ctx context.Context, auditQ Executor, alias [16]byte, accessor string, op VaultOperation, code string, bizErr error) error {
	aerr := s.appendAudit(ctx, auditQ, alias, accessor, auditPurpose("failed", op, code))
	return errors.Join(bizErr, aerr)
}

// appendAudit 在独立审计执行面上落一条留痕。失败以 ErrAuditDurability wrap
// 上交（X12：禁止静默吞）；发号失败同罪——无 id 即无法落账，等同写失败.
func (s *VaultService) appendAudit(ctx context.Context, auditQ Executor, alias [16]byte, accessor, purpose string) error {
	id, err := s.newID()
	if err != nil {
		return fmt.Errorf("%w: 审计 id 发号失败: %w", ErrAuditDurability, err)
	}
	entry := AccessLogEntry{
		AccessID:       id,
		StudentAliasID: alias,
		Accessor:       accessorOf(accessor),
		AccessedAt:     s.now().UTC(),
		Purpose:        purpose,
	}
	if err := s.store.AppendAccessLog(ctx, auditQ, entry); err != nil {
		// 双 %w：哨兵可 errors.Is 分支，原始存储错误证据链不斩断.
		return fmt.Errorf("%w: %w", ErrAuditDurability, err)
	}
	return nil
}

// decryptRow 把密文行解密为明文 DTO（任一字段认证失败即整体失败——不返回
// 部分明文；CreatedAt 为存储侧见证时刻）.
func (s *VaultService) decryptRow(alias [16]byte, row *IdentityCiphertext) (*StudentIdentity, error) {
	name, err := decryptField(row.NameCiphertext, row.NameNonce, s.key)
	if err != nil {
		return nil, err
	}
	phone, err := decryptField(row.PhoneCiphertext, row.PhoneNonce, s.key)
	if err != nil {
		return nil, err
	}
	address, err := decryptField(row.AddressCiphertext, row.AddressNonce, s.key)
	if err != nil {
		return nil, err
	}
	parentContact, err := decryptField(row.ParentContactCiphertext, row.ParentContactNonce, s.key)
	if err != nil {
		return nil, err
	}
	return &StudentIdentity{
		StudentAliasID: formatUUID(alias),
		Name:           name,
		Phone:          phone,
		Address:        address,
		ParentContact:  parentContact,
		CreatedAt:      row.CreatedAt,
	}, nil
}

// encryptRow 把明文请求加密为密文行（四字段各自独立随机 nonce；明文引用
// 只存活到本函数返回前）.
func (s *VaultService) encryptRow(alias [16]byte, req VaultWriteRequest) (IdentityCiphertext, error) {
	nameCT, nameN, err := encryptField(req.Name, s.key)
	if err != nil {
		return IdentityCiphertext{}, err
	}
	phoneCT, phoneN, err := encryptField(req.Phone, s.key)
	if err != nil {
		return IdentityCiphertext{}, err
	}
	addrCT, addrN, err := encryptField(req.Address, s.key)
	if err != nil {
		return IdentityCiphertext{}, err
	}
	parentCT, parentN, err := encryptField(req.ParentContact, s.key)
	if err != nil {
		return IdentityCiphertext{}, err
	}
	return IdentityCiphertext{
		StudentAliasID:          alias,
		NameCiphertext:          nameCT,
		NameNonce:               nameN,
		PhoneCiphertext:         phoneCT,
		PhoneNonce:              phoneN,
		AddressCiphertext:       addrCT,
		AddressNonce:            addrN,
		ParentContactCiphertext: parentCT,
		ParentContactNonce:      parentN,
	}, nil
}

// purposeOf 归一调用方申报用途（空回落 defaultPurpose，冻结实现同默认）.
func purposeOf(purpose string) string {
	if purpose == "" {
		return defaultPurpose
	}
	return purpose
}

// accessorOf 归一审计主体（空回落 defaultAccessor，冻结实现同默认）——
// 审计「谁」列必非空，留痕不存在无主行.
func accessorOf(accessor string) string {
	if accessor == "" {
		return defaultAccessor
	}
	return accessor
}

// auditPurpose 结构化审计 purpose 文本：拒绝/失败行以「<outcome>:<op>:<code>」
// 承载审计结论（枚举词零 PII），允许行直接是调用方申报用途。无 DDL 新列的
// 前提下让 access_log 可判读「这次访问到底放行没有」.
func auditPurpose(outcome string, op VaultOperation, code string) string {
	return outcome + ":" + string(op) + ":" + code
}

// failCodeOfRead 把读路径业务错误归一为审计细节枚举（只认自家哨兵，未知
// 错误一律 store_error——审计语料有界，不反射任意错误文本）.
func failCodeOfRead(err error) string {
	switch {
	case errors.Is(err, ErrIdentityNotFound):
		return "not_found"
	default:
		return "store_error"
	}
}

// failCodeOfWrite 同上（写路径）.
func failCodeOfWrite(err error) string {
	switch {
	case errors.Is(err, ErrIdentityExists):
		return "already_exists"
	default:
		return "store_error"
	}
}
