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

// PlatformRegistry 构造并装入平台通用验证器注册表。src 为查重摘要登记源；
// W6 由 DB 适配（*_version.content_digest 列）提供生产实现后传入同型接口。
func PlatformRegistry(src DigestSource) (*registry.Registry[Validator], error) {
	r := registry.New[Validator]()
	if err := r.Register(DuplicateValidatorID, NewDuplicateValidator(src)); err != nil {
		return nil, fmt.Errorf("validators: 登记平台验证器失败: %w", err)
	}
	return r, nil
}
