// fsrs.go 承载 FSRS 复习策略（policy_id=fsrs / policy_version=1.0.0），
// 与固定间隔 legacy 策略（scheduler.go）并列。
//
// 设计要点：
//   - 调度纯函数核：输入 = 按时间序的作答事件视图 + FSRS 策略参数，输出 = 每题
//     一条的 EntryState。同一事件流 + 同一策略重放必得同态。可重放性由 go-fsrs
//     自身保证（v1.2.1 为纯确定性算法：无 fuzz、无全局随机源、无 wall-clock
//     依赖——与 py-fsrs 消费全局 random 不同，Go 侧无需额外 seed 纪律）。
//   - 队列入口纪律与 legacy 一致：答错（Again）→ 入队/重置；答对（Good）→ 已在
//     队则推进，不在队则忽略；对错未知 → 忽略（宁可不排程也不伪造证据）。
//   - Stage = 累计成功复习次数（Good/Easy 计数），单调非减、可重放。FSRS 无固定
//     "毕业"间隔，卡片入队后保持 pending，到期日随稳定性增长自然推远；
//     DueReviews 的 due_at <= now 过滤承担"何时该复习"的职责。
package review

import (
	fsrs "github.com/open-spaced-repetition/go-fsrs"
)

// FSRS 策略标识（与 fixed-interval/1.0.0 并列，双字段无枚举约束）.
const (
	FSRSPolicyID               = "fsrs"
	FSRSPolicyVersion          = "1.0.0"
	FSRSPolicyVersionOptimized = "1.1.0-optimized"
)

// FSRSPolicy 携带 FSRS 调度参数（请求保持率 + 21 权重 + 最大间隔等）.
type FSRSPolicy struct {
	Params fsrs.Parameters
}

// NewFSRSPolicy 返回默认参数的 FSRS 策略.
func NewFSRSPolicy() FSRSPolicy {
	return FSRSPolicy{Params: fsrs.DefaultParam()}
}

// NewFSRSPolicyOptimized 返回合成数据优化参数的 FSRS 策略（fsrs/1.1.0-optimized）。
// 参数来源：fsrs-opt/params_optimized.json（合成 sim-student 数据，非真实学生作答标定）。
// 仅显式 opt-in 使用，不影响 v1.0.0 默认行为。
func NewFSRSPolicyOptimized() FSRSPolicy {
	return FSRSPolicy{
		Params: fsrs.Parameters{
			RequestRetention: 0.9,
			MaximumInterval:  36500,
			W: fsrs.Weights{
				0.12386, 1.2931, 2.3065, 8.2956, 1.0, 0.001,
				1.742965, 0.001, 0.030614, 0.574502, 0.291703, 0.666051,
				0.129001, 0.269923, 2.87329, 0.264142, 2.496415,
			},
			Decay:  0.166158,
			Factor: 0.885322,
		},
	}
}

// ratingFromEvent 将事件对错映射为 FSRS 评分.
//   - correct=false → Again（答错：短间隔 / 重置稳定性）
//   - correct=true  → Good（答对：推进间隔）
func ratingFromEvent(correct bool) fsrs.Rating {
	if !correct {
		return fsrs.Again
	}
	return fsrs.Good
}

// RebuildQueueFSRS 事件流全量重放 → 每题队列状态（FSRS 策略版的可重建实现）.
//
// 与 RebuildQueue（固定间隔）并列：签名接受 FSRSPolicy 而非 []int 间隔表，
// 内部以 go-fsrs 的 Parameters.Repeat 计算下次到期。
//
// 返回 {item_version_id: EntryState}——只含曾在队的题（答对/未知不产生条目，
// 与 legacy 入口纪律一致）.
func RebuildQueueFSRS(events []ReviewEventView, policy FSRSPolicy) (map[string]EntryState, error) {
	states := map[string]EntryState{}
	cards := map[string]fsrs.Card{} // 在队题目的 FSRS 卡片状态（纯中间态）
	success := map[string]int{}     // 题目累计成功复习次数（Stage 源）

	for _, event := range events {
		// 对错未知 → 不迁移（与 legacy 一致：宁可不排程也不伪造证据）.
		if event.Correct == nil {
			continue
		}

		id := event.ItemVersionID
		card, inQueue := cards[id]

		// 入口纪律：答对且不在队 → 忽略（答对不是错题，不重新入队）.
		if *event.Correct && !inQueue {
			continue
		}

		// 不在队且答错 → 新建 FSRS 卡片.
		if !inQueue {
			card = fsrs.NewCard()
		}

		rating := ratingFromEvent(*event.Correct)
		sched := policy.Params.Repeat(card, event.CreatedAt)[rating]
		card = sched.Card
		cards[id] = card

		// 更新成功计数（Stage 源）.
		if rating == fsrs.Good || rating == fsrs.Easy {
			success[id]++
		}

		// 装配 EntryState.
		state := EntryState{
			Stage:       success[id],
			Status:      StatusPending,
			DueAt:       card.Due,
			LastEventID: event.EventID,
		}
		if inQueue {
			// 重置/推进：保留首次入队时刻与既有归因.
			state.EnqueuedAt = states[id].EnqueuedAt
			state.SourceErrorTypeID = states[id].SourceErrorTypeID
		} else {
			// 首次入队.
			state.EnqueuedAt = event.CreatedAt
			if len(event.ErrorTypeIDs) > 0 {
				state.SourceErrorTypeID = event.ErrorTypeIDs[0]
			}
		}
		// 答错事件刷新归因（与 legacy 一致：取首个错误推断）.
		if !*event.Correct && len(event.ErrorTypeIDs) > 0 {
			state.SourceErrorTypeID = event.ErrorTypeIDs[0]
		}

		states[id] = state
	}

	return states, nil
}
