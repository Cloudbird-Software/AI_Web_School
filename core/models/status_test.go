// status_test.go 状态机移植的验收测试：两台状态机的全转换矩阵覆盖
// （4×4 + 5×4 穷举）、门证书需求、终态/活跃池判定、枚举解析正负例。
//
// 语义基准：
//   - 版本状态机：_base.py「draft → quarantined → published → retired
//     （无回边）」+ publication.py「签发一跳 draft/quarantined → published」；
//   - 生命周期状态机：health.py _ALLOWED_TRANSITIONS（对齐 0018 迁移）。
package models

import "testing"

// ────────────────────────────────────────────────────────────────────
// 版本状态机：全矩阵穷举
// ────────────────────────────────────────────────────────────────────

func TestVersionStatusTransitionMatrix(t *testing.T) {
	// 合法性期望表（行 = from，列 = to），逐格对齐冻结语义
	legal := map[ItemVersionStatus]map[ItemVersionStatus]bool{
		VersionDraft:       {VersionDraft: false, VersionQuarantined: true, VersionPublished: true, VersionRetired: false},
		VersionQuarantined: {VersionDraft: false, VersionQuarantined: false, VersionPublished: true, VersionRetired: false},
		VersionPublished:   {VersionDraft: false, VersionQuarantined: false, VersionPublished: false, VersionRetired: true},
		VersionRetired:     {VersionDraft: false, VersionQuarantined: false, VersionPublished: false, VersionRetired: false},
	}
	for from, tos := range legal {
		for to, want := range tos {
			got := CanTransitionVersionStatus(from, to)
			if got != want {
				t.Fatalf("版本状态机 %s → %s: got %v, want %v", from, to, got, want)
			}
		}
	}
}

func TestVersionStatusIllegalAndUnknownStates(t *testing.T) {
	// 非法状态值一律不可作为转换起点/终点（fail-closed）
	if CanTransitionVersionStatus(ItemVersionStatus("DRAFT"), VersionPublished) {
		t.Fatal("非法状态值 DRAFT 作为起点被放行")
	}
	if CanTransitionVersionStatus(VersionDraft, ItemVersionStatus("PUBLISHED")) {
		t.Fatal("非法状态值 PUBLISHED 作为终点被放行")
	}
	if CanTransitionVersionStatus(ItemVersionStatus(""), VersionPublished) {
		t.Fatal("空状态值作为起点被放行")
	}
	// 回边全拒绝（六条非法回边逐条列出）
	backEdges := []struct{ from, to ItemVersionStatus }{
		{VersionQuarantined, VersionDraft},
		{VersionPublished, VersionDraft},
		{VersionPublished, VersionQuarantined},
		{VersionRetired, VersionDraft},
		{VersionRetired, VersionQuarantined},
		{VersionRetired, VersionPublished},
	}
	for _, e := range backEdges {
		if CanTransitionVersionStatus(e.from, e.to) {
			t.Fatalf("回边 %s → %s 被放行（无回边语义违反）", e.from, e.to)
		}
	}
}

func TestVersionStatusTerminalAndGateCert(t *testing.T) {
	if !IsVersionStatusTerminal(VersionRetired) {
		t.Fatal("retired 必须是终态")
	}
	for _, s := range []ItemVersionStatus{VersionDraft, VersionQuarantined, VersionPublished} {
		if IsVersionStatusTerminal(s) {
			t.Fatalf("%s 不应是终态", s)
		}
	}
	if !VersionStatusRequiresGateCert(VersionPublished) {
		t.Fatal("前移到 published 必须需门证书（D2）")
	}
	for _, s := range []ItemVersionStatus{VersionDraft, VersionQuarantined, VersionRetired} {
		if VersionStatusRequiresGateCert(s) {
			t.Fatalf("前移到 %s 不要求门证书（冻结实现仅在签发 published 时强制）", s)
		}
	}
}

// ────────────────────────────────────────────────────────────────────
// 生命周期状态机：全矩阵穷举（含初始态，对齐 0018/health.py）
// ────────────────────────────────────────────────────────────────────

func TestLifecycleTransitionMatrix(t *testing.T) {
	legal := map[LifecycleState]map[LifecycleState]bool{
		// "" = NULL 初始：仅允许 → ACTIVE
		"":                   {LifecycleActive: true, LifecycleWatch: false, LifecycleQuarantined: false, LifecycleRetired: false},
		LifecycleActive:      {LifecycleActive: false, LifecycleWatch: true, LifecycleQuarantined: false, LifecycleRetired: true},
		LifecycleWatch:       {LifecycleActive: true, LifecycleWatch: false, LifecycleQuarantined: true, LifecycleRetired: true},
		LifecycleQuarantined: {LifecycleActive: false, LifecycleWatch: true, LifecycleQuarantined: false, LifecycleRetired: true},
		LifecycleRetired:     {LifecycleActive: false, LifecycleWatch: false, LifecycleQuarantined: false, LifecycleRetired: false},
	}
	for from, tos := range legal {
		for to, want := range tos {
			got := CanTransitionLifecycle(from, to)
			if got != want {
				t.Fatalf("生命周期 %q → %s: got %v, want %v", from, to, got, want)
			}
		}
	}
}

func TestLifecycleTerminalGateCertAndActivePool(t *testing.T) {
	if !IsLifecycleTerminal(LifecycleRetired) {
		t.Fatal("RETIRED 必须是终态（无任何回边）")
	}
	for _, s := range []LifecycleState{"", LifecycleActive, LifecycleWatch, LifecycleQuarantined} {
		if IsLifecycleTerminal(s) {
			t.Fatalf("%q 不应是终态", s)
		}
	}

	// GATE_CERT_REQUIRED_STATES = {QUARANTINED, RETIRED}
	if !LifecycleRequiresGateCert(LifecycleQuarantined) || !LifecycleRequiresGateCert(LifecycleRetired) {
		t.Fatal("转入 QUARANTINED/RETIRED 必须需门证书")
	}
	for _, s := range []LifecycleState{"", LifecycleActive, LifecycleWatch} {
		if LifecycleRequiresGateCert(s) {
			t.Fatalf("转入 %q 不要求门证书（ACTIVE↔WATCH 自动转换）", s)
		}
	}

	// 活跃池 = {ACTIVE, WATCH}（排除 QUARANTINED/RETIRED）
	if !IsInActivePool(LifecycleActive) || !IsInActivePool(LifecycleWatch) {
		t.Fatal("ACTIVE/WATCH 必须属于活跃池")
	}
	if IsInActivePool(LifecycleQuarantined) || IsInActivePool(LifecycleRetired) {
		t.Fatal("QUARANTINED/RETIRED 不得属于活跃池")
	}
}

// ────────────────────────────────────────────────────────────────────
// 枚举解析：正负例全覆盖
// ────────────────────────────────────────────────────────────────────

func TestParseEnumRoundTrip(t *testing.T) {
	for _, tc := range []struct {
		name  string
		parse func(string) (any, error)
		valid []string
	}{
		{"tier", func(s string) (any, error) { return ParseTier(s) },
			[]string{"A", "B", "C", "D"}},
		{"version_status", func(s string) (any, error) { return ParseVersionStatus(s) },
			[]string{"draft", "quarantined", "published", "retired"}},
		{"template_status", func(s string) (any, error) { return ParseTemplateStatus(s) },
			[]string{"draft", "published", "retired"}},
		{"material_kind", func(s string) (any, error) { return ParseMaterialKind(s) },
			[]string{"passage", "image", "table", "audio"}},
		{"license_decision", func(s string) (any, error) { return ParseLicenseDecision(s) },
			[]string{"approved", "rejected", "expired"}},
		{"lifecycle_state", func(s string) (any, error) { return ParseLifecycleState(s) },
			[]string{"ACTIVE", "WATCH", "QUARANTINED", "RETIRED"}},
	} {
		for _, v := range tc.valid {
			if _, err := tc.parse(v); err != nil {
				t.Fatalf("%s: 合法值 %q 被拒绝: %v", tc.name, v, err)
			}
		}
	}
}

func TestParseEnumRejectsIllegal(t *testing.T) {
	for _, tc := range []struct {
		name  string
		bad   string
		parse func(string) (any, error)
	}{
		{"tier", "E", func(s string) (any, error) { return ParseTier(s) }},
		{"tier", "a", func(s string) (any, error) { return ParseTier(s) }},
		{"version_status", "PUBLISHED", func(s string) (any, error) { return ParseVersionStatus(s) }},
		{"version_status", "", func(s string) (any, error) { return ParseVersionStatus(s) }},
		{"template_status", "quarantined", func(s string) (any, error) { return ParseTemplateStatus(s) }},
		{"material_kind", "video", func(s string) (any, error) { return ParseMaterialKind(s) }},
		{"license_decision", "pending", func(s string) (any, error) { return ParseLicenseDecision(s) }},
		{"lifecycle_state", "SLEEPING", func(s string) (any, error) { return ParseLifecycleState(s) }},
		{"lifecycle_state", "active", func(s string) (any, error) { return ParseLifecycleState(s) }},
	} {
		if _, err := tc.parse(tc.bad); err == nil {
			t.Fatalf("%s: 非法值 %q 未被拒绝", tc.name, tc.bad)
		}
	}
}

func TestParseLifecycleStateEmptyIsNull(t *testing.T) {
	// 空串 = NULL = 初始（该 item 尚无 transition），返回零值不报错
	s, err := ParseLifecycleState("")
	if err != nil {
		t.Fatalf("空串（NULL 初始态）被拒绝: %v", err)
	}
	if s != "" {
		t.Fatalf("空串应映射为零值, got %q", s)
	}
}
