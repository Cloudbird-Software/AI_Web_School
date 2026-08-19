// Package webutil 承载 Web 层通用纯函数（T-W5-031 骨架的第一块核心域）。
//
// SafeNext 是 T-W0-010/011 Python 版 _safe_next 的语义等价移植（CodeQL
// py/url-redirection 的 Go 侧同源防线）：登录回跳地址只接受站内相对路径。
package webutil

import "strings"

// DefaultNext 是非法回跳地址的回落值（工作台首页）。
const DefaultNext = "/items"

// SafeNext 白名单化登录回跳地址：仅接受以单个 "/" 开头，且第二个字符
// 不得是 "/" 或 "\"（WHATWG URL 规范把权威段位置的反斜杠归一化为 "/"，
// "/\evil.com" 在浏览器里等于 "//evil.com"，是跨域跳转），否则回落
// DefaultNext。
func SafeNext(next string) string {
	if !strings.HasPrefix(next, "/") {
		return DefaultNext
	}
	if len(next) >= 2 {
		if second := next[1]; second == '/' || second == '\\' {
			return DefaultNext
		}
	}
	return next
}
