package auth

import (
	"crypto/rand"
	"fmt"
)

// 环境变量名与生产环境标识。常量集中在此而非散落 os.Getenv 调用点：
// 密钥来源是审计关注的配置面，收口一处才可被检索与追责。
const (
	// EnvEnvironment 是运行环境变量（SCHOOL_ENV）；值等于
	// EnvironmentProduction 视为生产，其余任何值（含未设置）视为开发。
	EnvEnvironment = "SCHOOL_ENV"
	// EnvVarAuthKey 是令牌签名密钥的环境变量名。密钥绝不进仓库、绝不进
	// 日志、绝不经 API 回传（D9/AR-3）。
	EnvVarAuthKey = "SCHOOL_AUTH_SECRET"
	// EnvironmentProduction 是 SCHOOL_ENV 的生产值。
	EnvironmentProduction = "production"
)

// ResolveSecret 按 D9 fail-closed 规则解析签名密钥。
//
//   - 生产模式且未配置：返回错误——缺密钥的服务宁可拒绝启动，也不能
//     用默认/空密钥签出可伪造的令牌（fail-closed）。
//   - 开发模式且显式配置了密钥：允许，但必须经调用方把返回的告警打进
//     启动日志——"可用"不能静默，防止开发密钥悄悄流窜到生产。
//   - 开发模式且未配置：生成进程内随机临时密钥并告警；重启即全量失效，
//     这对本地调试无害，同时保证不存在"空密钥也能跑通"的路径。
//
// 返回的 warnings 必须由调用方写日志（本包不持有 logger 依赖，保持核心域
// 零外部耦合）。
func ResolveSecret(environment, configured string) (secret []byte, warnings []string, err error) {
	if environment == EnvironmentProduction {
		if configured == "" {
			return nil, nil, fmt.Errorf(
				"auth: %s=production 但未配置 %s，按 D9 fail-closed 拒绝启动", EnvEnvironment, EnvVarAuthKey)
		}
		return []byte(configured), nil, nil
	}
	if configured == "" {
		buf := make([]byte, minSecretLen)
		// crypto/rand.Read 失败意味着熵源不可用：此时无法承诺密钥强度，
		// 与生产缺密钥同样处置——拒绝启动而不是退化到固定密钥。
		if _, err := rand.Read(buf); err != nil {
			return nil, nil, fmt.Errorf("auth: 系统熵源不可用，无法生成临时开发密钥: %w", err)
		}
		warnings = append(warnings,
			fmt.Sprintf("auth: 未配置 %s，已生成进程内随机临时密钥（重启后全部已签发令牌失效，仅限开发）", EnvVarAuthKey))
		return buf, warnings, nil
	}
	warnings = append(warnings,
		fmt.Sprintf("auth: 检测到显式 %s 开发密钥：仅限本地开发，严禁携带至生产环境", EnvVarAuthKey))
	return []byte(configured), warnings, nil
}

// EnsureSigner 组合 ResolveSecret 与 NewSigner：入口进程在装配期一次拿到
// 可用的 Signer；错误直接阻断启动，warnings 仍由调用方落日志。
func EnsureSigner(environment, configured string) (*Signer, []string, error) {
	secret, warnings, err := ResolveSecret(environment, configured)
	if err != nil {
		return nil, nil, err
	}
	s, err := NewSigner(secret)
	if err != nil {
		return nil, nil, err
	}
	return s, warnings, nil
}
