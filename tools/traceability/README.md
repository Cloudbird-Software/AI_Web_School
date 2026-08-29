# traceability —— 强制实证矩阵机器校验（A8/P9）

T-W5-027 交付。校验 `specs/contracts/TRACEABILITY.md` 的「强制实证矩阵」章节：

1. **条款覆盖**：矩阵条款编号集合 ⊇ 宪法（specs/constitution.md）解析出的全部编号（V/A/D/P/X）；
2. **已强制必有实证**：状态列含「已强制」的行，实证路径列不得为空/`—`（P9：声称已强制但无实证 = 最高优先级）；
3. **路径存在**：每个实证条目的文件部分必须在盘上。

## 用法

```bash
go run ./tools/traceability              # 仓库任意子目录可跑（上溯找 go.mod）
go test ./tools/traceability/ -race      # 双向自测（fixture 红绿用例）
```

退出码：0 全绿 / 1 违规 / 2 操作错误。

## 路径列书写规范

- 多条目用 `;` 分隔；条目 = 纯文件路径（可带 `:符号` 或 `#锚` 尾注）+ 可选全角括号注释；
- 全角括号 `（...）` 内为人类可读说明，不参与存在性校验；
- `org:` 前缀 = 外部组织仓引用（.github / CI-Workflows），存在性由 org gate 承载，本地跳过；
- `—` 开头 = 明示无实证（只允许配合非「已强制」状态）。

## 接线建议（owner 裁量；Makefile check 目标与本工具解耦）

- **入 gate**：ci.yml `go-check` job 的 run 块追加一行 `go run ./tools/traceability`；
- **入波次出口**：scripts/wave-exit/wN.sh（owner 维护）追加同命令——P9 的「波次出口刷新矩阵」由出口脚本强制；
- 未接线前的兜底：`tools/traceability/main_test.go` 的实仓冒烟已随 `go test ./... -race` 进 go-test gate（矩阵破坏时测试先红）。
