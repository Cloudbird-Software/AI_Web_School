# Secret 管理规范（T-W0-006）

> 宪法依据：[X3 禁止任何密钥/凭证进入仓库、日志、prompt](../specs/constitution.md)
> 任务卡：[T-W0-006](../tasks/w0/T-W0-006.md)
> 适用范围：全体 agent、CI、本地开发。首年方案=.env + CI secrets + LiteLLM 网关；Vault/KMS 等企业级系统属 non_goal。

## 1. 密钥存储位置

### 1.1 允许的位置

| 位置 | 用途 | 谁可读 | 说明 |
|------|------|--------|------|
| **本地 `.env`** | 本地开发用的真实密钥 | 仅本机开发者 | 已在 `.gitignore` 中，禁止提交；结构见 `.env.example`（占位值） |
| **CI secrets** | GitHub Actions 运行时所需密钥 | 仅 CI 运行环境 | 通过 `${{ secrets.* }}` 引用，日志自动脱敏 |
| **LiteLLM 网关** | 模型供应商 key 的唯一持有点 | 仅网关进程 | agent 只拿到网关分发的角色虚拟 key，供应商真实 key 不出网关 |

### 1.2 当前密钥清单（按 `.env.example` 结构，**不含真实值**）

| 变量名 | 类别 | 存储位置 | 备注 |
|--------|------|----------|------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | 基础设施 | `.env` + CI secrets | 本地默认 `muti` / `change-me-local` / `muti_dev` |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | 基础设施 | `.env` + CI secrets | 对象存储 root 凭证 |
| `LITELLM_MASTER_KEY` | 网关主控 | `.env` + CI secrets | 网关管理面 key，禁止直接用于业务调用 |
| `DEEPSEEK_API_KEY` | 供应商 key | **仅 LiteLLM 网关配置**（不在 `.env` 中用于业务） | 真实值只存在于网关的 secret store |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 供应商 key | 同上 | 按需启用 |

### 1.3 禁止出现的位置（违反即 X3 FAIL）

- **仓库**：任何 `.py`/`.md`/`.yaml`/`.json`/`.ini`/`.toml`/测试夹具/脚本中写入真实密钥值。CI 由 `pr-check.yml` 的「泄密扫描（X3）」步骤拦截——正则匹配 `(api[_-]?key|secret|password|token)\s*[:=]\s*[A-Za-z0-9_\-]{16,}`。
- **日志**：`print()` / `logging` / pytest 输出 / docker compose logs 中输出密钥值。
- **prompt**：向 LLM 发送的 prompt 中包含密钥值（含系统消息、工具参数、few-shot 示例）。
- **遥测**：`.agent/telemetry/*.jsonl` 中记录密钥值（该目录已在 `.gitignore`，但仍禁止写入密钥）。
- **数据库**：将密钥以明文存入业务表。

## 2. Agent 只拿角色虚拟 key 条款

**核心原则**：agent 永远不直接持有供应商真实 key；所有模型调用经 LiteLLM 网关，按角色分发独立虚拟 key。

### 2.1 角色虚拟 key 机制

- 网关（LiteLLM）持有供应商真实 key（如 `DEEPSEEK_API_KEY`），是唯一真实 key 持有者。
- 网关为每个**角色**（Builder / Verifier / Judge / Scribe 等）签发独立虚拟 key，记录在网关的 key store 中。
- 角色虚拟 key 可绑定：
  - 允许调用的模型集合（如 Builder 只能调 `deepseek-chat`，Judge 只能调 `claude-sonnet`）
  - 每日 token 预算上限
  - 调用速率限制
  - 过期时间

### 2.2 可吊销轮换

- 角色虚拟 key 可单独吊销（revoke）而不影响其他角色——疑似某 agent 泄密时，仅 revoke 该角色 key，其他角色继续工作。
- 轮换（rotate）角色虚拟 key 不需要触碰供应商真实 key：在网关管理面生成新虚拟 key、更新该角色的 `LITELLM_ROLE_KEY` 环境变量、吊销旧 key。

### 2.3 最小 key 集

- 每个 agent 只接收其当前任务所需的角色虚拟 key——不一次性分发所有角色 key。
- 任务结束后，该角色的临时 key（若有）立即吊销。
- 禁止 agent 之间共享 key；禁止把 key 写入 `.agent/telemetry/` 或任务卡。

## 3. 轮换流程

### 3.1 DeepSeek API Key 轮换

1. 在 DeepSeek 控制台生成新 key。
2. 更新 LiteLLM 网关配置中的 `DEEPSEEK_API_KEY`（仅网关持有）。
3. 重启/热加载网关使新 key 生效。
4. 在 DeepSeek 控制台 revoke 旧 key。
5. 抽查网关日志确认旧 key 不再被调用（grep 旧 key 前缀）。

### 3.2 GitHub PAT 轮换

1. 在 GitHub Settings → Developer settings → Personal access tokens 生成新 token（最小权限：`repo` + `workflow`）。
2. 更新 CI secrets：`GH_PAT`（或等价名）。
3. 等待一次 CI 运行确认新 token 可用。
4. 在 GitHub revoke 旧 token。
5. 确认本地 `git remote -v` 不依赖旧 token（若用 HTTPS + token，更新 credential helper）。

### 3.3 数据库密码轮换

1. 在 `.env` 与 CI secrets 中准备新密码（`POSTGRES_PASSWORD`）。
2. 通过迁移或管理命令在 PG 中 `ALTER USER muti WITH PASSWORD '<新密码>'`（注意：这是数据变更，走管理命令而非手工 SQL——参考 X7 的精神）。
3. 更新所有连接方（本地 `.env`、CI secrets、网关配置若连库）。
4. 滚动重启所有连库进程。
5. 确认旧密码失效（用旧密码连接应失败）。

## 4. 泄密应急：revoke → 轮换 → 审计

疑似密钥泄露时（如发现 key 出现在日志/prompt/公网），立即按以下三步处置：

### 4.1 Step 1 — Revoke（立即吊销，止损优先）

- **供应商 key**：立即在供应商控制台 revoke（DeepSeek/GitHub/Anthropic）。
- **角色虚拟 key**：在 LiteLLM 网关管理面 revoke 受影响角色的虚拟 key（其他角色不受影响）。
- **DB 密码**：若 `POSTGRES_PASSWORD` 疑似泄露，立即 `ALTER USER` 改密码（接受短暂连接中断）。
- **目标**：15 分钟内完成 revoke，使泄露的 key 失效。

### 4.2 Step 2 — 轮换（按 §3 流程生成新 key）

- 按 §3.1/§3.2/§3.3 对应流程生成并部署新 key。
- 更新所有密钥持有点（`.env` / CI secrets / 网关）。
- 确认新 key 生效后，再继续业务。

### 4.3 Step 3 — 审计（追溯影响面）

- **git 历史**：`git log -p --all -S '<key 片段>'` 检查是否曾进入仓库历史；若有，需 `git filter-repo` 清理（严重情况）。
- **CI 日志**：检查最近 N 次 CI 运行日志是否输出过该 key。
- **遥测**：grep `.agent/telemetry/*.jsonl` 是否记录该 key。
- **prompt 日志**：若网关记录了 prompt，检查是否包含该 key。
- **输出**：在 ADR/  or 事故复盘文档中记录泄露范围、根因、整改措施；必要时通报人类。

## 5. CI 泄密扫描拦截演示结论（T-W0-002 演示 B）

含假 key 的演示 PR 由 T-W0-002 统一执行（演示 B），不在本任务重复。演示结论引用如下：

- **拦截位置**：`.github/workflows/pr-check.yml` 的「泄密扫描（X3）」步骤。
- **拦截逻辑**：`! git diff origin/main...HEAD | grep -inE '(api[_-]?key|secret|password|token)\s*[:=]\s*[A-Za-z0-9_\-]{16,}'`——对 PR diff 做大小写不敏感正则匹配，命中即 `exit 1` 拦红。
- **演示结论**：含 `api_key=sk-<40位假key>` 的 PR 被该步骤拦红并输出「❌ 疑似密钥进入仓库」。
- **复核状态**：本任务复核 pr-check.yml 配置完整、正则覆盖常见密钥命名（api_key/api-key/apikey/secret/password/token），与 T-W0-002 演示结论一致，无需改动 workflow。

## 6. 验证记录：.gitignore 覆盖确认

任务卡验收标准 3 要求 `git check-ignore .env .agent/telemetry/x.jsonl` 均命中。实际验证扩展到三条（含 `tests/model-bench/results/x.json`）。

**命令**（在 T-W0-006 worktree 根目录执行）：

```bash
git check-ignore .env .agent/telemetry/x.jsonl tests/model-bench/results/x.json
```

**输出**（全部命中 = 三条路径都被 .gitignore 忽略）：

```
.env
.agent/telemetry/x.jsonl
tests/model-bench/results/x.json
```

**退出码**：`0`（git check-ignore 命中时返回 0）。

**对应 .gitignore 规则**：
- `.env` → `.gitignore` 第 1 行 `.env`
- `.agent/telemetry/x.jsonl` → `.gitignore` 第 7 行 `.agent/telemetry/*.jsonl`
- `tests/model-bench/results/x.json` → `.gitignore` 第 8 行 `tests/model-bench/results/`

三条全部命中，密钥文件、遥测 JSONL、模型基准结果均不会进入仓库。
