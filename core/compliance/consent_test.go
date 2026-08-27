// T-W5-010：授权判定出口（consent.go）的域级单测——折叠函数的三分型、
// 类型化错误的哨兵锚定与日志注入防御、purpose 常量的非空约定.
package compliance

import (
	"errors"
	"strings"
	"testing"
)

func TestPurposeOnlinePractice(t *testing.T) {
	if PurposeOnlinePractice == "" {
		t.Fatal("purpose 常量不得为空（空 purpose 会被 validateChainKey 拒绝，常量失去意义）")
	}
}

// TestRequireGranted_Folding 折叠函数三分型：
//
//	err 非 nil → 原样透传（基础设施故障，调用方 fail-closed 500）；
//	(nil, nil) → 防御性错误（实现契约破坏）；
//	granted    → nil；其余三态 → *ConsentRequiredError 且字段逐一对应.
func TestRequireGranted_Folding(t *testing.T) {
	sentinel := errors.New("db down")

	t.Run("存储故障原样透传", func(t *testing.T) {
		err := RequireGranted(nil, sentinel)
		if !errors.Is(err, sentinel) {
			t.Fatalf("故障必须透传（identity 保留），得到 %v", err)
		}
		if errors.Is(err, ErrConsentRequired) {
			t.Fatal("基础设施故障不得被折叠成授权缺失")
		}
	})

	t.Run("空状态无错误属契约破坏", func(t *testing.T) {
		err := RequireGranted(nil, nil)
		if err == nil || errors.Is(err, ErrConsentRequired) {
			t.Fatalf("(nil,nil) 必须防御性拒绝且不是授权哨兵，得到 %v", err)
		}
	})

	t.Run("granted放行", func(t *testing.T) {
		if err := RequireGranted(&ConsentStatus{State: StateGranted, IsValid: true}, nil); err != nil {
			t.Fatalf("granted 必须放行，得到 %v", err)
		}
	})

	for _, st := range []State{StateMissing, StateRevoked, StateExpired} {
		st := st
		t.Run("拒绝_"+string(st), func(t *testing.T) {
			err := RequireGranted(&ConsentStatus{
				StudentAliasID: "alias-x", Purpose: PurposeOnlinePractice, State: st,
			}, nil)
			if err == nil {
				t.Fatalf("%s 态必须拒绝", st)
			}
			if !errors.Is(err, ErrConsentRequired) {
				t.Fatalf("拒绝必须锚定哨兵，得到 %v", err)
			}
			var cre *ConsentRequiredError
			if !errors.As(err, &cre) || cre.State != st || cre.StudentAliasID != "alias-x" ||
				cre.Purpose != PurposeOnlinePractice {
				t.Fatalf("载体字段须逐一对应状态行，得到 %#v", cre)
			}
		})
	}
}

// TestConsentRequiredError_ErrorFormat Error() 是审计行来源：三元组齐备，
// 且 alias 经 Quote——控制字符（含换行）只落转义字面量，不破坏日志行结构
// （CodeQL go/log-injection 同源纪律：alias 出自令牌载荷，按不可信输入防御）.
func TestConsentRequiredError_ErrorFormat(t *testing.T) {
	e := &ConsentRequiredError{
		StudentAliasID: "alias\"with\nnewline",
		Purpose:        PurposeOnlinePractice,
		State:          StateRevoked,
	}
	msg := e.Error()
	for _, want := range []string{"alias", PurposeOnlinePractice, string(StateRevoked)} {
		if !strings.Contains(msg, want) {
			t.Fatalf("审计文本缺 %q: %s", want, msg)
		}
	}
	if strings.Contains(msg, "\n") {
		t.Fatalf("审计文本不得含原始换行（须被 Quote 转义）: %q", msg)
	}
	if !errors.Is(e, ErrConsentRequired) {
		t.Fatal("Unwrap 必须锚定哨兵")
	}
}
