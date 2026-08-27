// Command mathgen 是 W6 数学轮第一阶的批量出题入口：对已注册数学母题
// 各生成 N 个「过确定性验证器且结构互异」的实例，写 JSONL 并输出产能
// 汇总报告（issue #34 §七 W6：函数库生成 + 确定性验证）。
//
// 用法：
//
//	go run ./cmd/mathgen -templates all -n 30 -out out/mathgen/
//
// 确定性：全管线零时钟依赖——文件内容只由 (-seed, -n, -templates) 决定，
// 同参数重跑产物逐字节相同（可回放）；stdout 的耗时字段仅供观察不落盘。
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "mathgen 失败:", err)
		os.Exit(1)
	}
}

func run() error {
	templates := flag.String("templates", "all", "逗号分隔的母题 id，或 all")
	n := flag.Int("n", 30, "每母题生成的合格实例数")
	outDir := flag.String("out", "out/mathgen/", "JSONL 输出目录")
	seed := flag.Uint64("seed", 20260827, "批种子（同 seed 同输出，可回放）")
	flag.Parse()

	ids, err := resolveTemplates(*templates)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		return fmt.Errorf("创建输出目录 %s 失败: %w", *outDir, err)
	}

	fmt.Println("════════ mathgen · 数学母题确定性生成（W6 数学轮第一阶） ════════")
	fmt.Printf("seed=%d  n=%d/母题  out=%s\n", *seed, *n, *outDir)
	fmt.Printf("模板 %d 个：%s\n\n", len(ids), strings.Join(ids, ", "))

	totalOK, totalReq := 0, 0
	failed := false
	for _, id := range ids {
		totalOK += runTemplate(id, *n, *seed, *outDir, &failed)
		totalReq += *n
	}

	fmt.Println("\n──────── 总汇总 ────────")
	status := "PASS"
	if failed || totalOK != totalReq {
		status = "FAIL"
	}
	fmt.Printf("%d 母题 × n=%d：合格 %d/%d 实例；唯一率按批由 AssertPairwiseDistinct 断言。\n",
		len(ids), *n, totalOK, totalReq)
	fmt.Println("总体结果:", status)
	if status == "FAIL" {
		return fmt.Errorf("存在未达标母题，详见上方分项输出")
	}
	return nil
}

// resolveTemplates 解析 -templates 参数："all" 或显式逗号列表。
func resolveTemplates(spec string) ([]string, error) {
	all := subjectmath.IDs()
	if spec == "all" {
		return all, nil
	}
	var ids []string
	for _, s := range strings.Split(spec, ",") {
		id := strings.TrimSpace(s)
		if _, ok := subjectmath.Get(id); !ok {
			return nil, fmt.Errorf("未知模板 %q（可用：%s）", id, strings.Join(all, ", "))
		}
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		return nil, fmt.Errorf("-templates 为空")
	}
	return ids, nil
}

// runTemplate 跑单模板批次并完成落盘与分项打印；返回合格数。
func runTemplate(id string, n int, seed uint64, outDir string, failed *bool) int {
	start := time.Now()
	records, rep, err := subjectmath.Run(subjectmath.Options{TemplateID: id, N: n, Seed: seed})
	took := time.Since(start).Milliseconds()
	if rep == nil {
		fmt.Printf("▶ %s\n  ✘ %v（耗时 %dms）\n", id, err, took)
		*failed = true
		return 0
	}
	rep.DurationHintMs = took
	fmt.Printf("▶ %s v%s  空间=%d\n", id, rep.TemplateVersion, rep.SpaceSize)
	if err != nil {
		fmt.Printf("  ✘ %v\n", err)
		fmt.Printf("    统计：尝试=%d 构造成功=%d 合格=%d 拒绝分布={%s} 耗时=%dms\n",
			rep.Attempts, rep.Generated, rep.Accepted,
			strings.Join(mkRejectPairs(rep), ", "), took)
		*failed = true
		return rep.Accepted
	}

	path := filepath.Join(outDir, id+".jsonl")
	if werr := writeJSONL(path, records); werr != nil {
		fmt.Printf("  ✘ 写入失败: %v\n", werr)
		*failed = true
		return rep.Accepted
	}

	distinctMark := "断言通过"
	if !rep.DistinctOK || rep.UniqueRate != 1 {
		distinctMark = "异常"
	}
	fmt.Printf("  ✔ 合格 %d/%d  唯一率 %.1f%%（%s）  尝试 %d 次  拒绝 {%s}  耗时 %dms\n",
		rep.Accepted, rep.RequestedN, rep.UniqueRate*100, distinctMark, rep.Attempts,
		strings.Join(mkRejectPairs(rep), ", "), took)
	fmt.Printf("  ✔ JSONL → %s（%d 行）\n", path, len(records))
	return rep.Accepted
}

func mkRejectPairs(rep *subjectmath.Report) []string {
	pairs := make([]string, 0, len(rep.Rejected))
	for k, v := range rep.Rejected {
		pairs = append(pairs, fmt.Sprintf("%s=%d", k, v))
	}
	if len(pairs) == 0 {
		pairs = append(pairs, "-")
	}
	return pairs
}

// writeJSONL 每行一条实例记录（含 content_digest 与 space_index）。
func writeJSONL(path string, records []subjectmath.Record) error {
	f, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("创建 %s 失败: %w", path, err)
	}
	w := bufio.NewWriter(f)
	var firstErr error
	for _, r := range records {
		line, jerr := json.Marshal(r)
		if jerr != nil {
			firstErr = fmt.Errorf("序列化 %s 索引 %d 失败: %w", r.TemplateID, r.SpaceIndex, jerr)
			break
		}
		if _, werr := w.Write(append(line, '\n')); werr != nil {
			firstErr = fmt.Errorf("写入 %s 失败: %w", path, werr)
			break
		}
	}
	if ferr := w.Flush(); ferr != nil && firstErr == nil {
		firstErr = fmt.Errorf("flush %s 失败: %w", path, ferr)
	}
	if cerr := f.Close(); cerr != nil && firstErr == nil {
		firstErr = fmt.Errorf("关闭 %s 失败: %w", path, cerr)
	}
	return firstErr
}
