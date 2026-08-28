// mask.go 承载 T-W5-007 统一 mask 层在中间件侧的接线：凭证登记面
// （core/auth.CredentialRegistry）在启动装配期注入一次，此后本包全部
// 日志出口对动态文本经 Mask 收口——错误详情、panic 值等可能夹带敏感
// 键值对的文本，落日志前把已登记凭证值与敏感键名值打码为 ***。
//
// 为什么是包级注入而非每请求传递：mask 面是进程级事实（这台进程持有哪些
// 凭证），不是请求派生态；与 log 标准库的全局 logger 同属进程级装配物。
// 注入点唯一（SetCredentialRegistry，main 装配期调用），未注入时 Mask 为
// 恒等函数——既有单字段脱敏语义不因本层缺席而改变（防御纵深只增不改）。
package middleware

import (
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

var (
	maskMu       sync.RWMutex
	registryMask *auth.CredentialRegistry
)

// SetCredentialRegistry 注入凭证登记面（main 装配期一次；nil = 恢复恒等
// 缺省）。重复注入以最后一次为准——装配期顺序问题，不是请求期竞争。
func SetCredentialRegistry(r *auth.CredentialRegistry) {
	maskMu.Lock()
	defer maskMu.Unlock()
	registryMask = r
}

// Mask 对动态文本应用已注入登记面的统一打码（值打码面 + 键名打码层，
// 语义见 CredentialRegistry.Mask）；未注入登记面时原样返回。
func Mask(text string) string {
	maskMu.RLock()
	r := registryMask
	maskMu.RUnlock()
	if r == nil {
		return text
	}
	return r.Mask(text)
}
