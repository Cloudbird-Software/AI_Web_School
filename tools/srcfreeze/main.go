// Package main 实现 ADR-0007 的 src 冻结机器执行体（PyR-RETIRE 2/2）：
// src/（Python 服务运行时）已退役为只读冻结归档——树内任何增/删/改一律红，
// 与 specs/test-freeze/ 的测试冻结治理同构（纪律约束升级为确定性红绿）。
//
// 为什么逐文件 SHA256 而非只钉文件清单：退役冻结保护的是取证面本身，字节级
// 漂移（含行尾/编码被工具链悄悄改写）同属篡改；清单变更必须走 ADR-0007 §四
// 的人类例外通道（--resign 重签 + PR 逐行审），不允许"顺手改"。
//
// 用法：
//
//	go run ./tools/srcfreeze            # 校验：src/** 与清单集合+内容一致
//	go run ./tools/srcfreeze --resign   # 人类例外：重算清单（安全修复例外，须在 PR 说明并引用 ADR-0007 §四）
//
// 退出码：0 全绿；1 违规（gate 拦截）；2 执行失败。只用标准库。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const (
	srcDir      = "src"
	manifestDir = "specs/src-freeze"
	manifestNam = "MANIFEST.sha256"
)

func main() {
	root := "."
	resign := false
	for _, a := range os.Args[1:] {
		switch a {
		case "--resign":
			resign = true
		default:
			fmt.Fprintf(os.Stderr, "srcfreeze: 未知参数 %q\n", a)
			os.Exit(2)
		}
	}

	actual, err := scanTree(filepath.Join(root, srcDir))
	if err != nil {
		fmt.Fprintf(os.Stderr, "srcfreeze: 扫描失败: %v\n", err)
		os.Exit(2)
	}

	manifest := filepath.Join(root, manifestDir, manifestNam)
	if resign {
		if err := writeManifest(manifest, actual); err != nil {
			fmt.Fprintf(os.Stderr, "srcfreeze: 重签失败: %v\n", err)
			os.Exit(2)
		}
		fmt.Printf("✅ 已重签 %s（%d 个文件）——人类例外通道，PR 须引用 ADR-0007 §四并逐行审\n", manifest, len(actual))
		return
	}

	pinned, err := readManifest(manifest)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srcfreeze: 清单不可读: %v\n", err)
		os.Exit(2)
	}

	var bad []string
	for p, want := range pinned {
		got, ok := actual[p]
		if !ok {
			bad = append(bad, fmt.Sprintf("已删除(冻结面禁改): %s", p))
			continue
		}
		if got != want {
			bad = append(bad, fmt.Sprintf("已修改(冻结面禁改): %s", p))
		}
	}
	for p := range actual {
		if _, ok := pinned[p]; !ok {
			bad = append(bad, fmt.Sprintf("新增(冻结面禁增): %s", p))
		}
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		fmt.Fprintf(os.Stderr, "❌ src/ 冻结面违规 %d 处（ADR-0007：退役归档只读，安全修复走 --resign 例外）:\n", len(bad))
		for _, b := range bad {
			fmt.Fprintf(os.Stderr, "  - %s\n", b)
		}
		os.Exit(1)
	}
	fmt.Printf("✅ src/ 冻结面完整：%d 个文件与清单逐字节一致（ADR-0007）\n", len(pinned))
}

// scanTree 返回 srcDir 下全部冻结文件的 {相对路径: sha256}（相对路径正斜杠）。
// 跳过 Python 解释器运行时产物（__pycache__/、*.pyc/*.pyo）——make check 的
// pytest/alembic 阶段会先于本校验在 src/ 下再生这些派生物（CI 实证 200 处
// 假红），它们不是冻结面内容；源码冻结仍逐字节。
func scanTree(base string) (map[string]string, error) {
	out := map[string]string{}
	err := filepath.WalkDir(base, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "__pycache__" {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(d.Name(), ".pyc") || strings.HasSuffix(d.Name(), ".pyo") {
			return nil
		}
		sum, err := fileSHA256(path)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(filepath.Dir(base), path)
		if err != nil {
			return err
		}
		out[filepath.ToSlash(rel)] = sum
		return nil
	})
	return out, err
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() {
		_ = f.Close()
	}()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func readManifest(path string) (map[string]string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		digest, rel, found := strings.Cut(line, "  ")
		if !found {
			return nil, fmt.Errorf("清单行格式非法（须为 sha256 两空格 path）: %q", line)
		}
		out[strings.TrimSpace(rel)] = strings.TrimSpace(digest)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("清单为空——冻结面失效")
	}
	return out, nil
}

func writeManifest(path string, files map[string]string) error {
	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	var b strings.Builder
	b.WriteString("# 由 tools/srcfreeze --resign 生成，禁止手改；变更须走 ADR-0007 §四人类例外流程（安全修复 + PR 逐行审）\n")
	for _, p := range paths {
		b.WriteString(files[p])
		b.WriteString("  ")
		b.WriteString(p)
		b.WriteString("\n")
	}
	return os.WriteFile(path, []byte(b.String()), 0o644)
}
