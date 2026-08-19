// Package webutil 承载 Web 层通用纯函数（T-W5-031 骨架的第一块核心域）。
//
// SafeNext 是 T-W0-010 Python 版 _safe_next 的语义等价移植（CodeQL
// py/url-redirection 的 Go 侧同源防线）：登录回跳地址只接受站内相对路径。
package webutil

import "strings"

// DefaultNext 是非法回跳地址的回落值（工作台首页）。
const DefaultNext = "/items"

// SafeNext 白名单化登录回跳地址：仅接受以单个 "/" 开头（排除 "//host"
// 协议相对跳转与绝对 URL / 反斜杠变体），否则回落 DefaultNext。
func SafeNext(next string) string {
	if strings.HasPrefix(next, "/") && !strings.HasPrefix(next, "//") {
		return next
	}
	return DefaultNext
}
