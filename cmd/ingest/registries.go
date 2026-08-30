// registries.go —— 平台注册表契约 id 集的加载面（cmd/ingest 专用）。
//
// 纪律（宪法 D4 / 铁律 3）：作答交互与评分器只能来自平台注册表。注册表契约
// 文件（specs/contracts/registries/interaction.yaml、scorer.yaml）是平台级
// 只增不改的冻结候选——本文件只读它，不解析全量 schema（渲染组件/作答
// schema 等由各自消费域负责），只取「哪些 id 已注册、现役与否」这一最小面，
// 供门侧 registry 验证器做 interaction_id / scorer_id 注册核查。
//
// 为什么在 cmd/ingest 而非 core/gate/validators：validators 的注册表挂接
// （PlatformRegistry）面向平台通用验证器，本卡不允许改 core/**；而加载
// YAML 属装配层职责（cmd/ 是装配点），实现为纯函数便于测试。
package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// contractIDs 是注册表契约的 id 投影：id → status（active/reserved/...）。
// 只保留 status 而非 bool：验证器只放行 active（预留条目未实现，放行即虚报）。
type contractIDs map[string]string

// loadContractIDs 读取注册表契约 YAML，抽取 listKey 指定列表下全部条目的
// id 与 status。文件缺失/结构残缺/条目缺 id 一律报错（装配期 fail-fast——
// 注册表面残缺时整批实例都没有可证的注册语境，继续跑只会制造假 pass）。
func loadContractIDs(path, listKey string) (contractIDs, error) {
	buf, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("registries: 读取 %s 失败: %w", path, err)
	}
	var doc map[string]any
	if err := yaml.Unmarshal(buf, &doc); err != nil {
		return nil, fmt.Errorf("registries: 解析 %s 失败: %w", path, err)
	}
	rawList, ok := doc[listKey]
	if !ok {
		return nil, fmt.Errorf("registries: %s 缺少 %s 列表（契约结构漂移，装配期拒绝）", path, listKey)
	}
	list, ok := rawList.([]any)
	if !ok {
		return nil, fmt.Errorf("registries: %s 的 %s 不是列表", path, listKey)
	}
	ids := contractIDs{}
	for i, e := range list {
		m, ok := e.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("registries: %s[%d] 不是对象", listKey, i)
		}
		id, _ := m["id"].(string)
		if id == "" {
			return nil, fmt.Errorf("registries: %s[%d] 缺 id", listKey, i)
		}
		status, _ := m["status"].(string)
		if _, dup := ids[id]; dup {
			return nil, fmt.Errorf("registries: %s 条目 id 重复 %q", path, id)
		}
		ids[id] = status
	}
	if len(ids) == 0 {
		return nil, fmt.Errorf("registries: %s 的 %s 为空（注册表面残缺，拒绝装配）", path, listKey)
	}
	return ids, nil
}

// isActive 报告 id 是否为现役注册条目（未注册/预留一律 false——放行未注册
// id 即违反铁律 3，放行 reserved 即虚报能力）。
func (c contractIDs) isActive(id string) bool { return c != nil && c[id] == "active" }
