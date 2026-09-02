package compliance

// alias.go 承载卡 #185 的学生 alias 命名空间校验：合成学生（sim_engine 产出）
// 走独立 alias 命名空间，与真实学生隔离，支撑 source 分账纪律。
//
// 命名空间规则（对齐 D5 分账精神 + DESIGN_NOTES.md §3）：
//   - 合成学生 alias 必须以 sim_ 前缀开头（sim_{batch}_{seq}）
//   - 真实学生 alias 禁止 sim_ 前缀（防合成身份污染真实数据）
//
// 校验原语是纯函数，供 core/session 提交路径与 cmd/school 装配面调用；本文件
// 只做判定，不做持久化——写入端点必须幂等且对并发加锁（S4/D11）是调用方的责任.

import (
	"fmt"
	"strings"
)

// SimAliasPrefix 是合成学生 alias 的强制前缀（DESIGN_NOTES.md §3.1）.
const SimAliasPrefix = "sim_"

// 哨兵错误：调用方按 errors.Is 分支处理（异常不泄漏）.
var (
	// ErrInvalidAliasNamespace 表示 alias 违反命名空间纪律：真实学生使用
	// sim_ 前缀，或合成学生缺少 sim_ 前缀。细分原因见 wrap 文本.
	ErrInvalidAliasNamespace = fmt.Errorf("compliance: alias 违反 sim_ 命名空间纪律")

	// ErrEmptyAlias 表示 alias 为空串（UUID 格式校验在调用方，本包只判命名空间）.
	ErrEmptyAlias = fmt.Errorf("compliance: alias 为空")
)

// IsSimAlias 报告 alias 是否位于合成命名空间（sim_ 前缀）.
// 空串返回 false（不属任何命名空间）.
func IsSimAlias(alias string) bool {
	return strings.HasPrefix(alias, SimAliasPrefix)
}

// ValidateSimAlias 校验合成学生 alias：必须非空且以 sim_ 前缀开头。
// 供模拟学生引擎提交前自检（合成身份必须落 sim_ 命名空间）.
func ValidateSimAlias(alias string) error {
	if alias == "" {
		return fmt.Errorf("%w: 合成学生 alias 不能为空", ErrEmptyAlias)
	}
	if !IsSimAlias(alias) {
		return fmt.Errorf("%w: 合成学生 alias %q 必须以 %q 前缀开头（分账隔离）",
			ErrInvalidAliasNamespace, alias, SimAliasPrefix)
	}
	return nil
}

// ValidateRealAlias 校验真实学生 alias：必须非空且不得使用 sim_ 前缀。
// 供真实学生会话创建/提交前守卫（防合成身份污染真实数据）.
func ValidateRealAlias(alias string) error {
	if alias == "" {
		return fmt.Errorf("%w: 真实学生 alias 不能为空", ErrEmptyAlias)
	}
	if IsSimAlias(alias) {
		return fmt.Errorf("%w: 真实学生 alias %q 不得使用 %q 前缀（合成命名空间保留）",
			ErrInvalidAliasNamespace, alias, SimAliasPrefix)
	}
	return nil
}

// ClassifyAlias 按前缀判定 alias 归属命名空间（供路由/分账分支）.
type AliasClass string

const (
	ClassReal AliasClass = "real" // 真实学生
	ClassSim  AliasClass = "sim"  // 合成学生
)

// Classify 按 sim_ 前缀判定 alias 类别。空串返回空类别（调用方前置拒绝）.
func Classify(alias string) AliasClass {
	if alias == "" {
		return ""
	}
	if IsSimAlias(alias) {
		return ClassSim
	}
	return ClassReal
}
