// consent.go 承载 T-W5-010 的授权判定出口：把「链顶状态」折叠为「放行 /
// 403 领域错误」二值结论，供在线业务入口（会话创建）调用。
//
// 为什么在 compliance 而不在协议层判定：状态 → 是否放行是合规域语义
// （X12 fail-closed 的主体），api 只做协议映射；折叠函数纯函数化后，
// 「非 granted 一律拒绝」的判定只有一个真相源，协议层不再各自解读 State.
package compliance

import (
	"errors"
	"fmt"
	"strconv"
)

// PurposeOnlinePractice 是在线练习会话入口（冻结契约 v1 的 POST /sessions
// 「开始练习」）对应的授权 scope 主键语义键：授权链的第二级身份
// （见 GrantInput.Purpose）。宪法红线「家长授权前置」在在线链路的落点即
// 以本键记账——学生开练习会话前，其 alias 在本 purpose 下必须处于 granted.
//
// 与账面既有 purpose（practice/diagnosis/measurement 等业务场景键）的关系：
// 本键描述「在线作答入口」这一入口级授权，不与内容场景键混用（D5 同款
// 分场景纪律）；后续诊断/测量在线入口落地时按同一惯例各立常量，禁止裸串.
const PurposeOnlinePractice = "online_practice"

// ErrConsentRequired 是「无有效家长授权」的哨兵错误：missing/revoked/expired
// 三态的统一领域结论（协议层据此映射 403，见 middleware.MapError）。具体是
// 哪一态由 ConsentRequiredError.State 细分——细粒度只进服务端审计日志，
// 对外保持粗粒度（errmap.go 的脱敏纪律：对外暴露的分支越细，探测者的
// 可区分反馈越多）.
var ErrConsentRequired = errors.New("compliance: 无有效家长授权（在线入口 fail-closed，X12）")

// ConsentRequiredError 是授权检查失败的结构化载体（验收 #4 可审计）：
// alias / purpose（scope 主键）/ state 三元组齐备，Error() 文本即审计行来源.
// 不携带 PII——student_alias_id 是 D7 口径的假名标识，主库/日志合法形态.
type ConsentRequiredError struct {
	StudentAliasID string
	Purpose        string
	State          State
}

// Error 实现 error。alias 走 strconv.Quote：日志行的消费者是人，但写入面
// 要按不可信输入防御——Quote 转义控制字符，令牌载荷里的别名即便被塞入
// 换行也只落成 \n 字面量（CodeQL go/log-injection 同源纪律）.
func (e *ConsentRequiredError) Error() string {
	return fmt.Sprintf("%s: alias=%s purpose=%q state=%q", ErrConsentRequired.Error(),
		strconv.Quote(e.StudentAliasID), e.Purpose, e.State)
}

// Unwrap 锚定哨兵：errors.Is(err, ErrConsentRequired) 对本类型恒真，协议层
// 的映射矩阵因此可以只依赖哨兵而不依赖具体类型.
func (e *ConsentRequiredError) Unwrap() error { return ErrConsentRequired }

// RequireGranted 把 CheckConsent 的输出折叠为放行/拒绝二值：
//
//   - err 非 nil：存储故障**原样透传**——调用方必须按「基础设施不可用」
//     fail-closed 拒绝（X12：绝不放行），而不是误判成「无授权」或「有授权」；
//   - status 为 nil 且无错：CheckConsent 的实现契约不允许 (nil, nil)，出现即
//     内部管线破坏，按防御性错误拒绝（宁可 500 不可放行）；
//   - IsValid（= granted）：放行，返回 nil；
//   - 其余三态：包装为 *ConsentRequiredError（State 细分供审计）.
func RequireGranted(status *ConsentStatus, err error) error {
	if err != nil {
		return err
	}
	if status == nil {
		return errors.New("compliance: CheckConsent 返回空状态且无错误（实现契约破坏，fail-closed）")
	}
	if status.IsValid {
		return nil
	}
	return &ConsentRequiredError{
		StudentAliasID: status.StudentAliasID,
		Purpose:        status.Purpose,
		State:          status.State,
	}
}
