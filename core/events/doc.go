// Package events 承载作答事件账的 append-only 写入域——三本只增不改的账之一
// （宪法 D1），数据飞轮入水口（A3/A4）。语义与字段契约冻结于
// specs/contracts/events/response_event.md v1.0.0，schema 由迁移 0003 承载
// （append-only 触发器 + 按月分区），本包不改 schema。
//
// ── D11 事务纪律（T-W5-017 包级红线）────────────────────────────────────────
// 本域禁止 Commit/Rollback 调用：写入服务不自行终结任何事务。一次业务写入是一个
// 事务（作答事件与会话状态同进同退），事务边界由最外层调用方（API 依赖/作业）
// 显式持有并统一 Commit/Rollback；本域只把「已 begin 的执行面」绑定为写入口。
// 守卫是双层的：
//  1. 构造型防线（fail-closed）：Record 在无显式事务执行面时直接返回
//     ErrNoTransaction——非事务上下文里事件写不进去，而非「先写先得」；
//  2. 静态防线：guard_test.go 用 go/parser 扫描本包源码文件，出现 .Commit( /
//     .Rollback( 调用即红（白名单：无）；测试文件豁免——单测中的 fake 事务以
//     Commit/Rollback 扮演最外层调用方，那正是边界归属的正确示范。
package events
