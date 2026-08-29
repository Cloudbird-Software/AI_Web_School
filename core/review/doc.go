// Package review 承载复习排程核心域（W3 S6；Python 冻结实现 src/core/review/
// 的 Go 重锚定）：间隔重复排程纯函数核 + 派生队列条目类型。
//
// 架构定位（架构 §4.4）：纯函数策略接口 + 版本化 → 队列可重建。全部排程
// 函数无副作用、无 IO：输入 = 按时间序的作答事件视图 + 固定间隔表，输出 =
// 每题一条的队列状态机。同一事件流 + 同一策略版本重放必得同态，这是
// 「队列版本可重建」（R-Z-07 / 架构 §4.4）的实现根基。
//
// v1 策略 = 固定间隔表 [1, 3, 7, 21] 天（迁移 0010 内置种子
// fixed-interval/1.0.0）；FSRS 等 v2 策略另起 policy_id，不影响本核。
//
// 状态机语义（每 学生×题目 一条）：
//   - 答错（含错误推断的事件）→ 入队或重置：stage=0，due = 事件时刻 + intervals[0]
//   - 答对（在队 pending）→ 推进：stage+1；越过最后一个间隔 → done（出队）
//   - 答对但不在队 / 已 done → 忽略（不重新入队——答对不是错题）
//   - 对错无法判定（correct=nil）→ 忽略（评分轨迹缺 correctness 且无任何
//     错误推断时，v1 不做猜测性归因——宁可不排程也不伪造证据）
//
// IO 面（显式留白）：DB 读取事件流、策略间隔表加载与队列条目 upsert
// （Python service.py 的 AsyncSession 面）不在 Go 纯函数核内——W6 服务化
// 接线时由 PG 执行面消费本包的 RebuildQueue / DueReviews 纯产物，SQL 文本
// 住在 db/queries（SQL-2：不在 Go 拼 SQL）。
package review
