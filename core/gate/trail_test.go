package gate

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// FailureTrail 侧可本地验证语义：留痕输入契约前置拦截（残缺事实不得入账）、
// 九列逐位映射（含 nil evidence → '{}'）、失败行与外层事务同进同退（D11：
// 不自 commit——门失败后业务方回滚则留痕随之消失是账实一致；成功提交则失败
// 留痕成账，验收 #3「留痕且事务不回滚」的本地代理面；真库行为由 CI 承担）.

var fixedFailedAt = time.Date(2026, 8, 27, 9, 0, 0, 0, time.UTC)

// sampleFailure 构造契约全要素齐全的合法留痕输入，mutate 注入变体.
func sampleFailure(mutate func(in *FailureInput)) FailureInput {
	in := FailureInput{
		FailureID:        "gf_01JDEMO00000000000000000000",
		ArtifactType:     ArtifactItem,
		ArtifactRef:      "item-v-1",
		ValidatorID:      "platform.fact_check",
		ValidatorVersion: "1.2.0",
		PolicyVersion:    "policy-2026w35",
		Reason:           "事实核查阻断：关键证据缺失",
		Evidence:         map[string]any{"rule": "evidence_required", "score": 0.2},
		FailedAt:         fixedFailedAt,
	}
	if mutate != nil {
		mutate(&in)
	}
	return in
}

// mustRecord 留痕成功即返回 failure_id；任何失败都视为用例缺陷.
func mustRecord(t *testing.T, tr *FailureTrail, in FailureInput) string {
	t.Helper()
	id, err := tr.Record(context.Background(), in)
	if err != nil {
		t.Fatalf("Record 意外失败: %v", err)
	}
	return id
}

// TestRecordWithoutExplicitTransactionIsRejected 是 fail-closed 面：三种「无显式
// 事务执行面」形态的全部留痕调用都直接 ErrNoTransaction.
func TestRecordWithoutExplicitTransactionIsRejected(t *testing.T) {
	cases := []struct {
		name string
		tr   *FailureTrail
	}{
		{"NewFailureTrail(nil)", NewFailureTrail(nil)},
		{"零值 Trail", &FailureTrail{}},
		{"nil Trail", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tc.tr.Record(context.Background(), sampleFailure(nil))
			if !errors.Is(err, ErrNoTransaction) {
				t.Fatalf("err = %v, want ErrNoTransaction", err)
			}
		})
	}
}

// TestRecordRejectsInvalidInputBeforeIO 锁定判定序：契约违例在进程内拦截，
// 一条 SQL 都不发。表驱动覆盖最小四元组每个必填项与类型域（0028 CHECK 的
// 应用侧镜像：DB 拒绝永远不该是第一现场）.
func TestRecordRejectsInvalidInputBeforeIO(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(in *FailureInput)
	}{
		{"空 failure_id", func(in *FailureInput) { in.FailureID = "" }},
		{"越域 artifact_type", func(in *FailureInput) { in.ArtifactType = ArtifactType("novel") }},
		{"空 artifact_type", func(in *FailureInput) { in.ArtifactType = "" }},
		{"空 artifact_ref", func(in *FailureInput) { in.ArtifactRef = "" }},
		{"空 validator_id", func(in *FailureInput) { in.ValidatorID = "" }},
		{"空 validator_version", func(in *FailureInput) { in.ValidatorVersion = "" }},
		{"空 policy_version", func(in *FailureInput) { in.PolicyVersion = "" }},
		{"空 reason", func(in *FailureInput) { in.Reason = "" }},
		{"零值 failed_at", func(in *FailureInput) { in.FailedAt = time.Time{} }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tx := &fakeGateTx{}
			_, err := NewFailureTrail(tx).Record(context.Background(), sampleFailure(tc.mutate))
			if !errors.Is(err, ErrInvalidFailure) {
				t.Fatalf("err = %v, want ErrInvalidFailure", err)
			}
			tx.mu.Lock()
			n := len(tx.pending)
			tx.mu.Unlock()
			if n != 0 {
				t.Fatalf("契约违例不得发出 SQL：pending=%d", n)
			}
		})
	}
}

// TestContractFieldsMapToTypedParams 锁定九列参数映射：身份文本、六值域投影、
// 时间戳保真与证据 '{}' 缺省——留痕行的每一列都要能对上入参的出处.
func TestContractFieldsMapToTypedParams(t *testing.T) {
	tx := &fakeGateTx{}
	tr := NewFailureTrail(tx)
	in := sampleFailure(func(f *FailureInput) { f.Evidence = nil }) // nil → '{}' 空对象
	mustRecord(t, tr, in)

	args := tx.lastPending(t).args
	if len(args) != 9 {
		t.Fatalf("INSERT 参数应为九列: %d", len(args))
	}
	if args[0] != in.FailureID || args[2] != in.ArtifactRef || args[3] != in.ValidatorID ||
		args[4] != in.ValidatorVersion || args[5] != in.PolicyVersion || args[6] != in.Reason {
		t.Fatalf("文本列映射失真: %#v", args)
	}
	if at, ok := args[1].(string); !ok || at != "item" {
		t.Fatalf("arg[1] 应为 item 类型投影: %#v", args[1])
	}
	if ev, ok := args[7].([]byte); !ok || string(ev) != "{}" {
		t.Fatalf("nil evidence 应记 '{}' 空对象: %#v", args[7])
	}
	ts, ok := args[8].(pgtype.Timestamptz)
	if !ok || !ts.Valid || !ts.Time.Equal(fixedFailedAt) {
		t.Fatalf("failed_at 应为原值时间戳: %#v", args[8])
	}
}

// TestEvidenceJSONBRoundTrip JSONB 序列化保真：结构往返一致、Unicode 与 HTML
// 字符按原文落库不转义（SetEscapeHTML(false)，对齐冻结实现 ensure_ascii=False）
// ——失败现场必须直读，审账人不应在 \u 转义里考古.
func TestEvidenceJSONBRoundTrip(t *testing.T) {
	tx := &fakeGateTx{}
	raw := map[string]any{
		"note": "<证据>缺失", // HTML 与中文混排：不转义、不 ASCII 化
		"deep": map[string]any{"matched": false},
	}
	mustRecord(t, NewFailureTrail(tx), sampleFailure(func(f *FailureInput) { f.Evidence = raw }))

	blob := tx.lastPending(t).args[7].([]byte)
	var back map[string]any
	if err := json.Unmarshal(blob, &back); err != nil {
		t.Fatal(err)
	}
	if back["note"] != raw["note"] {
		t.Fatalf("evidence 往返失真: %#v vs %#v", back["note"], raw["note"])
	}
	if nested, _ := back["deep"].(map[string]any); nested["matched"] != false {
		t.Fatalf("嵌套结构丢失: %#v", back)
	}
	if strings.Contains(string(blob), "\\u") {
		t.Fatalf("JSONB 不当转义（应为原文）: %s", blob)
	}
}

// TestFailureRowFollowsOwnerTransaction 是「失败也是账面事实」的事务归属面：
// 最外层 Commit → 失败行进账（留痕与判定链路的其它写入同一事务，验收 #3 本地
// 代理）；最外层 Rollback → 失败行消失（与服务调用方对失败的整体处置一致）。
// Record 自身永不触发终结——旧占位方案里「失败路径写库即炸回滚」的病灶在 Go
// 侧无处复燃.
func TestFailureRowFollowsOwnerTransaction(t *testing.T) {
	const companionStepSQL = "UPDATE companion_step SET done = true WHERE id = $1"

	cases := []struct {
		name     string
		finalize func(f *fakeGateTx) error
		wantRows int
	}{
		{
			name: "提交→失败行进账",
			finalize: func(f *fakeGateTx) error {
				return f.Commit()
			},
			wantRows: 1,
		},
		{
			name: "他域步骤失败→整体回滚→失败行不残留",
			finalize: func(f *fakeGateTx) error {
				f.failNext = true
				if _, err := f.Exec(context.Background(), companionStepSQL, "flow-1"); err == nil {
					t.Fatal("注入的失败步应真的失败")
				}
				return f.Rollback()
			},
			wantRows: 0,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeGateTx{}
			id := mustRecord(t, NewFailureTrail(f), sampleFailure(nil))
			if len(f.pending) != 1 {
				t.Fatalf("留痕应恰好发出一条未决语句: pending=%d", len(f.pending))
			}
			// 关键差异点：此刻 gate_failure 尚不可见——写入器未自作主张提交.
			if ferr := tc.finalize(f); ferr != nil {
				t.Fatal(ferr)
			}
			if got := f.countApplied(t, kindInsertFailure); got != tc.wantRows {
				t.Fatalf("applied 失败行数=%d want %d", got, tc.wantRows)
			}
			if tc.wantRows == 1 && !f.committed {
				t.Fatal("finalize 应为 Commit")
			}
			if id == "" {
				t.Fatal("Record 应返回 failure_id")
			}
		})
	}
}

// TestRecordMapsDriverErrorsVerbatim 驱动/约束错误原样 wrap 放行（如 append-only
// 触发器的 SQLSTATE），不吞并混报.
func TestRecordMapsDriverErrorsVerbatim(t *testing.T) {
	f := &fakeGateTx{failNext: true}
	_, err := NewFailureTrail(f).Record(context.Background(), sampleFailure(nil))
	if !errors.Is(err, errStepFailed) {
		t.Fatalf("驱动错误应原样可见: %v", err)
	}
	if errors.Is(err, ErrInvalidFailure) || errors.Is(err, ErrNoTransaction) {
		t.Fatalf("驱动故障不得被归一为业务哨兵: %v", err)
	}
}
