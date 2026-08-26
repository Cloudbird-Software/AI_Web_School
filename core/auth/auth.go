package auth

import (
	"crypto/subtle"
	"errors"
	"fmt"
	"time"
)

// Role 是主体类型。D9 要求四类主体权限独立、可审计、可撤销：
// 类型必须是显式值而非隐式推断，因此用强类型字符串而不是 bool/int 标志位。
type Role string

const (
	// RoleStudent 学生主体：绑定 student_alias_id，只能读写自身 alias 关联数据（D9）。
	RoleStudent Role = "student"
	// RoleStaff 教研主体：教研端权限与学生完全隔离。
	RoleStaff Role = "staff"
	// RoleOps 运维主体：运维端权限独立、可审计、可撤销（D9）。
	RoleOps Role = "ops"
	// RoleService 内部作业主体：批处理/调度等系统调用方，无学生身份语义。
	RoleService Role = "service"
)

// maxIDLen 是 subject id / alias id 的长度上限。alias 在库中是 UUID
// （36 字符），上限仅用于防御异常巨大的令牌载荷被无谓地保存进上下文，
// 正常业务标识远小于该值。
const maxIDLen = 512

// Valid 报告主体类型是否为四类合法值之一。
func (r Role) Valid() bool {
	switch r {
	case RoleStudent, RoleStaff, RoleOps, RoleService:
		return true
	default:
		return false
	}
}

// Principal 是一次已认证的主体表示。ID 用 string（不透明标识符）承载：
// student_alias_id 在数据库中是 UUID，且 D7 要求主库/核心域不接触真实
// 身份——对核心域而言它只是不可解释的串，绝不是 int 自增键或姓名。
type Principal struct {
	// Role 主体类型；零值 "" 非法（防止静默放行，见 AssertOwnsAlias）。
	Role Role
	// SubjectID 主体 id（staff/ops/service 的账号标识）。
	SubjectID string
	// AliasID 仅 student 主体携带的 student_alias_id；其余主体必须为空，
	// 这是 D9 最小权限的结构保证：非学生令牌不存在"顺手带个 alias"的可能。
	AliasID string
	// IssuedAt 签发时间（UTC）；随令牌负载存在以满足审计要求（可撤销的基础）。
	IssuedAt time.Time
	// ExpiresAt 过期时间（UTC）；短期令牌：过期即拒绝，不做宽限。
	ExpiresAt time.Time
}

// validatePrincipal 校验主体模型的内在一致性。Issue 与 Verify 双侧都调
// 它：即使签发侧有 bug 或密钥泄漏后被伪造，校验侧仍拒绝"类型与 alias
// 组合非法"的令牌——fail-closed，不信任上游。
func validatePrincipal(p Principal) error {
	if !p.Role.Valid() {
		return fmt.Errorf("%w: role=%q", ErrInvalidSubject, p.Role)
	}
	if p.SubjectID == "" {
		return fmt.Errorf("%w: SubjectID 为空", ErrInvalidSubject)
	}
	if len(p.SubjectID) > maxIDLen {
		return fmt.Errorf("%w: SubjectID 超长", ErrInvalidSubject)
	}
	switch p.Role {
	case RoleStudent:
		if p.AliasID == "" {
			return fmt.Errorf("%w: 学生主体必须绑定 student_alias_id", ErrInvalidSubject)
		}
		if len(p.AliasID) > maxIDLen {
			return fmt.Errorf("%w: AliasID 超长", ErrInvalidSubject)
		}
	default:
		if p.AliasID != "" {
			return fmt.Errorf("%w: 非 student 主体不得携带 AliasID", ErrInvalidSubject)
		}
	}
	return nil
}

// AssertOwnsAlias 判定主体是否可访问某 student_alias 关联的数据（授权原语，
// T-W5-005 验收 #3）：student 主体强制 alias 相等；staff/ops/service 的
// 授权由调用点显式判断（本原语返回 nil 不代表放行到数据，路由层仍需按
// 业务角色裁决）。任何非法主体类型一律报错——绝不因"未识别的类型"而
// 默认放行（fail-closed）。
//
// 为什么 student 用常量时间比较：代价为零，而比较的对象是访问控制判定
// 的输入；侧信道卫生应当是默认习惯而非事后补丁。
func AssertOwnsAlias(p Principal, aliasID string) error {
	if !p.Role.Valid() {
		return fmt.Errorf("%w: role=%q", ErrInvalidSubject, p.Role)
	}
	if p.Role == RoleStudent {
		if subtle.ConstantTimeCompare([]byte(p.AliasID), []byte(aliasID)) != 1 {
			return ErrAliasNotOwned
		}
	}
	return nil
}

var (
	// ErrNoToken 请求未携带凭证。
	ErrNoToken = errors.New("auth: 缺少Bearer令牌")
	// ErrMalformedToken 令牌结构无法解析（版本前缀错/base64 错/段数错/超长）。
	ErrMalformedToken = errors.New("auth: 令牌格式非法")
	// ErrBadSignature HMAC 校验失败（密钥不符或载荷被篡改）。
	ErrBadSignature = errors.New("auth: 令牌签名校验失败")
	// ErrExpiredToken 令牌已过过期时间。
	ErrExpiredToken = errors.New("auth: 令牌已过期")
	// ErrInvalidClaims 载荷语义非法（时间字段缺失/倒挂、类型与 alias 组合矛盾）。
	ErrInvalidClaims = errors.New("auth: 令牌载荷非法")
	// ErrInvalidSubject 主体模型非法（类型未知/id 空/alias 绑定关系违反最小权限）。
	ErrInvalidSubject = errors.New("auth: 主体模型非法")
	// ErrRoleDenied 已认证但角色不在允许集合内（403 语义）。
	ErrRoleDenied = errors.New("auth: 角色不足")
	// ErrAliasNotOwned student 主体试图访问他人 alias 数据（403 语义，D9 核心）。
	ErrAliasNotOwned = errors.New("auth: 无权访问该别名数据")
	// ErrInvalidSecret 密钥缺失或弱于安全下限（启动期装配错误）。
	ErrInvalidSecret = errors.New("auth: 签名密钥非法")
)

// IsAuthenticationError 判定 err 是否属于"身份认证失败"类（对应 HTTP 401）：
// 没有可信主体，请求根本不该继续。中间件据此统一脱敏映射。
func IsAuthenticationError(err error) bool {
	return errors.Is(err, ErrNoToken) ||
		errors.Is(err, ErrMalformedToken) ||
		errors.Is(err, ErrBadSignature) ||
		errors.Is(err, ErrExpiredToken) ||
		errors.Is(err, ErrInvalidClaims)
}

// IsAuthorizationError 判定 err 是否属于"已认证但越权"类（对应 HTTP 403）。
func IsAuthorizationError(err error) bool {
	return errors.Is(err, ErrRoleDenied) || errors.Is(err, ErrAliasNotOwned) ||
		errors.Is(err, ErrInvalidSubject)
}
