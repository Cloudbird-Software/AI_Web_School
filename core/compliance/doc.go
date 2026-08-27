// Package compliance 承载家长授权合规域：每个 (student_alias_id, purpose)
// 授权链是只增不改（append-only）的版本化事件账，版本分配必须并发安全，
// 「当前是否有效授权」永远由链顶（最新版本）唯一确定（宪法 D7 / D11 / A9）。
//
// 为什么必须有本包：Python 冻结实现 src/core/compliance/parental_consent.py
// 用 MAX(version)+1 读后写分配且 DB 侧无唯一约束——并发 grant/revoke 会产生
// 同版本号双行，check_consent 取到哪条不确定；这是合规账最不该出现的
// 不确定性（任务卡 T-W5-011）。Go 重锚定以三层保证收敛：
//
//  1. DB：唯一索引 uq_parental_consent_version_per_purpose（0027 迁移，
//     student_alias_id + scope->>'purpose' + version）——版本链全局无重的
//     最终防线，最新版本因此恰一行、确定性可判；
//  2. 应用：写入在调用方显式单事务内先取 per-chain advisory xact lock
//     （含「首事件无行可锁」的首插竞态），再经 sqlc 生成的类型安全查询
//     读链顶→算版本→插新事件（db/queries/consent.sql → db/gen，
//     core/compliance/pg.go 编排；内存实现以互斥锁等效）；
//  3. 测试：go test -race 下 N goroutine 并发写入同链 → 版本连续无重复、
//     链顶状态与事件账逐条自洽（memory_test.go），不依赖 sleep 制造顺序。
//
// append-only 留痕语义：撤回/再授权都是追加新版本行而非改写旧行——链上每行
// 自带「谁」（recorded_by，0027 列）、「何时」（created_at）、「从哪版到哪版」
// （version 单调序 n → n+1）；History 即账本的只读投影。旧版本失效时刻由后续
// 事件的 created_at 隐式承载（Python 冻结实现的既定口径），从不 UPDATE 旧行。
//
// 幂等口径说明（A9/D11）：与指针切换不同，每次授权调用都是一个**新的审计事实**
// （重复授予产生新版本行本身就是留痕），包层不做合并去重——对外写入端点的幂等
// 键由上层 API 承担，本包提供不可抵赖的事件流。撤回的前置校验（无有效授权即拒）
// 在同一临界区内完成，失败路径零副作用、不烧版本号。
//
// 事务纪律（S4/D11）：领域服务不自 commit——PG 实现的方法显式接收调用方持有
// 的已 begin 事务执行面（Executor），内存实现无持久化事务面；提交/回滚一律在
// 最外层调用方。
//
// 宪法 A5/X6：本包是核心域，禁止 import 任何学科/学段包（packs/*）。
package compliance
