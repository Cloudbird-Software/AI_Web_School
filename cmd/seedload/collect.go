// 种子文件收集：-file（可重复，直取）与 -dir（目录扫描 *.yaml/*.yml，
// 按文件名排序——多文件装载顺序确定，幂等重跑才可对账）叠加去重。
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// multiFlag 承载可重复的 -file（flag.Value 惯例形）.
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ", ") }

func (m *multiFlag) Set(s string) error {
	*m = append(*m, s)
	return nil
}

// collectSeeds 汇总装载清单：-file 直取在前，-dir 扫描在后；按
// Clean 后的路径去重。目录不存在时：给了 -file 则容忍（只装显式文件），
// 否则报错（没有可装的东西 = fail loud，不静默空跑）.
func collectSeeds(files multiFlag, dir string) ([]string, error) {
	seen := map[string]struct{}{}
	var out []string
	add := func(p string) {
		c := filepath.Clean(p)
		if _, ok := seen[c]; ok {
			return
		}
		seen[c] = struct{}{}
		out = append(out, c)
	}

	for _, f := range files {
		if info, err := os.Stat(f); err != nil {
			return nil, fmt.Errorf("种子文件不可达: %w", err)
		} else if info.IsDir() {
			return nil, fmt.Errorf("-file 须指向文件而非目录: %s", f)
		}
		add(f)
	}

	if dir != "" {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if len(files) > 0 && os.IsNotExist(err) {
				// 显式给了 -file：容忍默认/缺失目录（只装显式清单）
				entries, err = nil, nil
			}
			if err != nil {
				return nil, fmt.Errorf("读取种子目录失败: %w", err)
			}
		}
		var scanned []string
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			if !strings.HasSuffix(name, ".yaml") && !strings.HasSuffix(name, ".yml") {
				continue
			}
			scanned = append(scanned, filepath.Join(dir, name))
		}
		sort.Strings(scanned)
		for _, p := range scanned {
			add(p)
		}
	}

	if len(out) == 0 {
		return nil, fmt.Errorf("没有可装载的种子文件（-dir=%q 下无 *.yaml/*.yml 且未给 -file）", dir)
	}
	return out, nil
}
