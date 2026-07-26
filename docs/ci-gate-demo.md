# CI 三道门禁反模式演示记录（T-W0-002）

> 本文件记录三个反模式演示 PR 的 CI 拦截效果，用于验证 pr-check / contract-watch / nightly 三道门禁已正确联通。

---

## 正常 PR 基准（已合并）

- **PR**: #1
- **状态**: ✅ 全绿
- **说明**: 正常 PR 验证已完成，`pr-check.yml` 全部通过并合并至 main。

---

## 反模式演示 PR

> 注：以下演示 PR 由 Builder 推送分支后，由人类点击 compare 链接创建。CI 结果由真实 GitHub Actions 运行产生，记录时若尚未执行则标注「待人类确认」。

---

### 演示 A：删断言（反测试削弱）

| 字段 | 内容 |
|------|------|
| 分支 | `demo/w0-002-del-assert` |
| 违规修改 | 删除 `tests/contract/test_smoke.py` 中的 `assert True` |
| 预期拦截门禁 | `pr-check` → **反测试削弱（X1）** |
| 预期失败步骤 | `反测试削弱（X1）` |
| 预期报错摘要 | `❌ 删除既有断言` |
| PR 创建链接 | https://github.com/randypanding/AI_Web_School/compare/main...demo/w0-002-del-assert |
| 实际 CI 结果 | 待人类创建 PR 并确认 |
| 备注 | 演示完后由人类关闭，不合并 |

---

### 演示 B：假密钥（泄密扫描）

| 字段 | 内容 |
|------|------|
| 分支 | `demo/w0-002-fake-key` |
| 违规修改 | 新增 `src/fake/leak_demo.py`，内容含 `api_key = "sk-fake×××（32位）"`（完整字面量见该分支源码） |
| 预期拦截门禁 | `pr-check` → **泄密扫描（X3）** |
| 预期失败步骤 | `泄密扫描（X3）` |
| 预期报错摘要 | `❌ 疑似密钥进入仓库` |
| PR 创建链接 | https://github.com/randypanding/AI_Web_School/compare/main...demo/w0-002-fake-key |
| 实际 CI 结果 | 待人类创建 PR 并确认 |
| 备注 | 演示完后由人类关闭，不合并 |

---

### 演示 C：核心域 import 学科包（学科边界）

| 字段 | 内容 |
|------|------|
| 分支 | `demo/w0-002-core-import` |
| 违规修改 | 新增 `src/core/bad.py`，内容含 `import packs.subject_math` |
| 预期拦截门禁 | `pr-check` → **核心域禁止 import 学科包（X6）** |
| 预期失败步骤 | `核心域禁止 import 学科包（X6）` |
| 预期报错摘要 | `❌ 核心域引用了学科包` |
| PR 创建链接 | https://github.com/randypanding/AI_Web_School/compare/main...demo/w0-002-core-import |
| 实际 CI 结果 | 待人类创建 PR 并确认 |
| 备注 | 演示完后由人类关闭，不合并 |

---

## Nightly 手动触发验证

- **Workflow**: `.github/workflows/nightly.yml`
- **触发方式**: GitHub Actions 页面手动触发（`workflow_dispatch`）
- **状态**: 留待人类执行（Actions → nightly → Run workflow）
- **验证要点**:
  1. 全栈地基启动（docker compose up -d --wait）
  2. 迁移可逆演练（make migrate-check）
  3. 种子数据重灌 + 黄金数据集回归
  4. 黄金路径全量（30 题型）
  5. 契约测试全量
  6. 失败时自动创建 issue（红灯置顶）

---

## 分支清单

| 分支名 | 用途 | 是否合并 |
|--------|------|----------|
| `demo/w0-002-del-assert` | 删断言演示 | 否（演示后关闭） |
| `demo/w0-002-fake-key` | 假密钥演示 | 否（演示后关闭） |
| `demo/w0-002-core-import` | 核心域 import 学科包演示 | 否（演示后关闭） |
| `task/T-W0-002-ci-gates` | 本记录文件交付分支 | 由人类审阅后合并 |

---

## 更新日志

- 2026-07-26: 创建三个演示分支并推送；记录预期 CI 拦截结果；nightly 触发项标注待人类执行。
