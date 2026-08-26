// Package auth 承载认证与主体绑定框架（T-W5-005，宪法 D9 的框架层实证）。
//
// 职责边界（tasks/w5/REANCHORING.md §三）：只提供四类主体模型
// （student/staff/ops/service）、短期 HMAC 令牌的签发与校验、以及两个
// 授权判定原语 RequireAuth 中间件（api/middleware）与 AssertOwnsAlias；
// 不接入任何业务路由（全端点接入是 T-W5-006），不做 OAuth/微信等登录
// 流程（non_goals）。
//
// 为什么不用 JWT 第三方库：标准库 crypto/hmac + crypto/sha256 完全可替代，
// 且固定单一签名方案从格式上消灭了 JWT 的 alg 协商攻击面（新依赖须 owner
// 批，标准库可替代就不得引入）。
//
// 令牌格式（自研，非 JWT）：`v1.<base64url(payload)>.<base64url(HMAC-SHA256)>`，
// 版本前缀为将来密钥轮换/算法演进留位。负载含主体类型、主体 id、
// （student 时）student_alias_id、签发时间、过期时间。
//
// 错误脱敏惯例（对齐 api/api.go）：对外的错误响应只暴露 error_class 语义，
// 具体拒绝原因（签名错/过期/类型不符）只进服务端日志——不向攻击者回传
// 可用于区分校验分支的内部细节。
package auth
