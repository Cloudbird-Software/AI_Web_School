package main

// blueprint.go 承载 CLI 的蓝图解析面：读 JSON 文件 → assembly.PaperBlueprint。
//
// 字段级必填校验归编排层 Orchestrate（assembly.ErrInvalidBlueprint 单一真相
// 源，本 CLI 不另立一套校验语义——重复即漂移）；本文件只负责「文件可读、
// 内容是合法 JSON」的解析失败显式化.

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/Cloudbird-Software/AI_Web_School/core/assembly"
)

// loadBlueprint 读并解析组卷蓝图 JSON 文件（与 POST /papers 请求体同一
// PaperBlueprint JSON 投影契约）.
func loadBlueprint(path string) (assembly.PaperBlueprint, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return assembly.PaperBlueprint{}, fmt.Errorf("读取蓝图 %s 失败: %w", path, err)
	}
	var bp assembly.PaperBlueprint
	if err := json.Unmarshal(raw, &bp); err != nil {
		return assembly.PaperBlueprint{}, fmt.Errorf("蓝图 %s 非合法 JSON: %w", path, err)
	}
	return bp, nil
}
