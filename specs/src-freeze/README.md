# src 冻结治理（Python 运行时退役归档）

> 依据：ADR-0007（Python 运行时退役——Go 单轨生效）· ADR-0004 §四（历史保留，
> 不删除——作答证据与审计不可抹除）· PyR-RETIRE #142。
> 一句话：**src/ 已退役为只读冻结归档，任何增/删/改都是篡改，机器直接红。**

## 一、冻结面与强制

- 冻结面：`src/` 整目录（api/core/packs/registry/workbench，含全部子文件）。
- 强制体：`tools/srcfreeze`（Go，标准库）——`specs/src-freeze/MANIFEST.sha256`
  逐文件 SHA256 钉扎，集合不一致（增/删）或字节不一致（改）一律退出码 1。
- 挂载点：`make check`（`$(MAKE) src-freeze`），PR gate 阶段拦截。

## 二、为什么逐字节而不只是清单

退役冻结保护的是取证面本身。行尾/编码被工具链悄悄改写、依赖文件被"顺手"
替换，同属对冻结副本的篡改；字节级钉扎把这类漂移全部纳入红绿判定。

## 三、人类例外流程（唯一合法变更通道）

仅限 **安全修复**（ADR-0004 §四划定的唯一例外；功能回移一律拒绝）：

1. 修复 PR 内说明安全理由并引用 ADR-0007 §四；
2. PR 内执行 `go run ./tools/srcfreeze --resign` 重签清单，MANIFEST.sha256
   的 diff 仅包含被修复文件；
3. reviewer 对修复 diff 与清单 diff 逐行审；缺任一要素即打回。
