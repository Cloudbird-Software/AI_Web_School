package main

import (
	"os"
	"path/filepath"
	"testing"
)

// writeTmp 建临时目录并写入文件（测试装载面小工具）。
func writeTmp(t *testing.T, name, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("写临时文件失败: %v", err)
	}
	return path
}

func TestLoadContractIDsInteractionShape(t *testing.T) {
	path := writeTmp(t, "interaction.yaml", `registry: interaction
types:
  - id: single_choice
    status: active
  - id: oral
    status: reserved
`)
	ids, err := loadContractIDs(path, "types")
	if err != nil {
		t.Fatalf("loadContractIDs: %v", err)
	}
	if !ids.isActive("single_choice") {
		t.Fatalf("single_choice 应为 active")
	}
	if ids.isActive("oral") {
		t.Fatalf("reserved 条目不得放行")
	}
	if ids.isActive("multi_choice") {
		t.Fatalf("未注册 id 不得放行")
	}
}

func TestLoadContractIDsScorerShape(t *testing.T) {
	path := writeTmp(t, "scorer.yaml", `registry: scorer
scorers:
  - id: exact_match
    status: active
`)
	ids, err := loadContractIDs(path, "scorers")
	if err != nil {
		t.Fatalf("loadContractIDs: %v", err)
	}
	if !ids.isActive("exact_match") {
		t.Fatalf("exact_match 应为 active")
	}
}

func TestLoadContractIDsFailClosed(t *testing.T) {
	cases := []struct {
		name    string
		content string
		listKey string
	}{
		{"文件缺失", "", "types"},
		{"缺列表键", "registry: interaction\n", "types"},
		{"列表为空", "types: []\n", "types"},
		{"条目缺 id", "types:\n  - status: active\n", "types"},
		{"id 重复", "types:\n  - id: a\n    status: active\n  - id: a\n    status: active\n", "types"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var path string
			if tc.content == "" {
				path = filepath.Join(t.TempDir(), "missing.yaml")
			} else {
				path = writeTmp(t, "reg.yaml", tc.content)
			}
			if _, err := loadContractIDs(path, tc.listKey); err == nil {
				t.Fatalf("期望 fail-closed，实际成功")
			}
		})
	}
}

// TestLoadContractIDsRealContracts 用仓库真实契约文件冒烟：两个注册表都能
// 加载，且 subjectmath 实例用的 numeric_blank / exact_match 是现役条目。
// （go test 的工作目录是包目录，仓库根为其上两级。）
func TestLoadContractIDsRealContracts(t *testing.T) {
	interactions, err := loadContractIDs(filepath.Join("..", "..", "specs", "contracts", "registries", interactionYAML), "types")
	if err != nil {
		t.Fatalf("interaction.yaml 加载失败: %v", err)
	}
	scorers, err := loadContractIDs(filepath.Join("..", "..", "specs", "contracts", "registries", scorerYAML), "scorers")
	if err != nil {
		t.Fatalf("scorer.yaml 加载失败: %v", err)
	}
	if !interactions.isActive("numeric_blank") || !interactions.isActive("single_choice") {
		t.Fatalf("数学轮交互类型应为现役")
	}
	if !scorers.isActive("exact_match") {
		t.Fatalf("exact_match 应为现役")
	}
	if len(interactions) < 10 || len(scorers) < 5 {
		t.Fatalf("注册表面异常收缩: %d interactions / %d scorers", len(interactions), len(scorers))
	}
}
