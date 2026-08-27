// 验证器注册挂接（宪法 D4 对照：门侧验证器同走平台注册表，
// 学科包只复用与参数化、禁止私造查重/判定结构；W6 扩展点在此收口）。
//
// 挂接形态完全参照 registry/ 既有条目：泛型 registry.Registry[Validator] +
// Register（id 冲突返回 registry.ErrDuplicate，禁止静默覆盖）+ Get。
// 平台通用验证器由 InstallPlatform 装入指定注册表；api 装配与 W6 编排器
// 自注册表取用。本包不持有可变全局单例，避免 init 注册的隐式耦合。
package validators

import (
	"context"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// Validator 是门侧验证器的注册表接口：一切过门判定都来自这里登记的实现。
type Validator interface {
	// Entry 是版本化条目身份（可审计 §八：validator_id + validator_version）。
	Entry() registry.Entry
	// Validate 对候选实例产出三值判定；实现必须 fail-closed 且并发安全。
	Validate(ctx context.Context, c Candidate) Result
}

// PlatformRegistry 构造并装入平台通用验证器注册表。src 为查重摘要登记源，
// facts 为语篇事实核查登记源；两者允许为 nil（对应验证器一律 review 置信 0，
// 不宣称已查证）。judge 为语篇事实核查的语义判定面（FactJudge，W6 接 BAML
// harness 后注入；本卡不接任何 LLM 实现，传 nil 时语义事实落 review）。
// W6 由 DB 适配（*_version.content_digest 列 / 事实登记表）提供登记源生产
// 实现。阻断性不在本装配面——验证器只产出三值 verdict，链上阻断性由策略
// 矩阵（W6 编排器读链配置）决定。
func PlatformRegistry(src DigestSource, facts FactSource, judge FactJudge) (*registry.Registry[Validator], error) {
	r := registry.New[Validator]()
	if err := r.Register(DuplicateValidatorID, NewDuplicateValidator(src)); err != nil {
		return nil, fmt.Errorf("validators: 登记平台验证器失败: %w", err)
	}
	if err := r.Register(FactCheckValidatorID, NewFactCheckValidator(facts, judge)); err != nil {
		return nil, fmt.Errorf("validators: 登记平台验证器失败: %w", err)
	}
	return r, nil
}
