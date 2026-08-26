// Package estimator 承载估计器版本指针域：每场景（purpose_scope）同一时刻
// 至多一个活跃估计器版本，切换必须并发安全且留痕（宪法 D6 / A9，S4）。
//
// 为什么必须有本包：历史报告永远引用「当时」活跃的估计器版本（D6 可替换、
// 可重放）。指针切换是「退役旧行 + 登记新行」的两步写，若无单一临界区约束，
// 并发切换可能产生两条活跃指针或唯一索引冲突异常泄漏（Python 冻结实现
// src/core/data/active_model_pointer.py 的已知缺陷，任务卡 T-W5-019）。
//
// 并发语义由三层共同保证：
//  1. DB：偏唯一索引 uq_estimator_run_one_active_per_scope（0016 迁移，
//     purpose_scope WHERE retired_at IS NULL）——不变量的最终防线；
//  2. 应用：切换在调用方显式单事务内先取 per-scope advisory xact lock 再经
//     sqlc 生成的类型安全查询读改写（db/queries/estimator.sql → db/gen，
//     core/estimator/pg.go 编排；内存实现以互斥锁等效），杜绝「读旧→退役→
//     插新」竞态交错；
//  3. 测试：go test -race 下 N goroutine 并发切换 → 恰好一条活跃、账目与
//     切换一一对应（memory_test.go）。
//
// 事务纪律（S4/D11）：领域服务不自 commit——PG 实现的方法显式接收调用方
// 持有的 pgx.Tx，内存实现无持久化事务面；事务边界一律在最外层调用方。
//
// 宪法 A5/X6：本包是核心域，禁止 import 任何学科/学段包（packs/*）。
package estimator
