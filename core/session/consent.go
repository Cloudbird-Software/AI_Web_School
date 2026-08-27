package session

import (
	"context"
	"errors"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// RequireOnlinePracticeConsent 是在线练习会话入口的家长授权门
// （T-W5-010；宪法红线「家长授权前置」/ X12 fail-closed）：
// 学生 alias 在 PurposeOnlinePractice 下必须处于 granted 态才允许创建会话。
//
// 为什么在 core/session 而不在 api：「开练习会话需要什么授权」是会话域的
// 业务规则，协议层只应做错误→HTTP 的映射（ADR-0004 §三：api 只做协议层，
// 业务语义全部下沉 core/）。purpose 的选择也属业务知识，收敛在本函数——
// api 永远不写裸 purpose 串。
//
// 三类失败严格分型（调用方均不得放行，但映射不同）：
//   - store 为 nil：装配破坏，返回非 ErrConsentRequired 的装配错误
//     （协议层落 500 internal，向运维暴露「授权账未接线」而非向学生伪装
//     成「未授权」）；
//   - store 返回错误（DB 不可达等）：原样透传——RequireGranted 不吞基础设施
//     故障，协议层落 500 internal；绝不因账本读不到而放行（fail-closed）；
//   - 状态非 granted：*compliance.ConsentRequiredError（哨兵
//     ErrConsentRequired），协议层落 403 forbidden；三态细分只进审计日志.
//
// Executor 形参传 nil 的说明：会话骨架期尚无请求级事务执行面（W6 服务化
// 接线）；MemoryStore 不需要执行面，PGStore 收到 nil 会以 ErrNoTransaction
// fail-closed——生产在 W6 前不可能出现「账本存在却绕过校验」的中间态.
func RequireOnlinePracticeConsent(ctx context.Context, store compliance.ConsentStore, studentAliasID string) error {
	if store == nil {
		return errors.New("session: 家长授权账未装配（装配错误，在线入口 fail-closed）")
	}
	status, err := store.CheckConsent(ctx, nil, studentAliasID, compliance.PurposeOnlinePractice, nil)
	if err != nil {
		return fmt.Errorf("session: 授权账读取失败（fail-closed，不放行）: %w", err)
	}
	return compliance.RequireGranted(status, err)
}
