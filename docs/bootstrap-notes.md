# Bootstrap 环境笔记（Windows 适配记录）

> 从零到 `make bootstrap` 全绿。适用操作系统：Windows 10/11（含中文用户名/目录名）。

## 前置依赖

| 工具 | 版本要求 | 安装方式 |
|------|----------|----------|
| Docker Desktop | 最新版（含 Docker Compose v2） | [docker.com](https://www.docker.com/products/docker-desktop/) |
| Python | 3.12.x | [python.org](https://www.python.org/downloads/) 或 `pyenv-win` |
| MSYS2 (make) | 最新 | [msys2.org](https://www.msys2.org/) |
| Git | 最新 | [git-scm.com](https://git-scm.com/downloads/win) |

## 第 0 步：克隆仓库

```bash
git clone <repo-url>
cd 中小学教辅材料
```

## 第 1 步：Python 虚拟环境

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements-dev.txt
```

验证 Python 版本：

```bash
python -V
# 必须输出 Python 3.12.x
```

## 第 2 步：创建 .env

```bash
cp .env.example .env
```

`.env.example` 已有默认值可用于本地开发，无需修改即可启动。如需接入模型，填入对应 API Key。

## 第 3 步：启动 Docker 容器

```bash
docker compose up -d --wait
```

验证三个容器均健康运行：

```bash
docker compose ps
# STATUS 列应全部显示 "healthy"
```

## 第 4 步：执行 bootstrap 自检

```bash
make bootstrap
```

预期输出：

```
== 自检 ==
✅ PostgreSQL
✅ Redis
✅ MinIO
✅ Python 3.12
✅ bootstrap 完成
```

## 第 5 步：运行验收

```bash
make accept TASK=T-W0-001
```

预期输出以 `✅ T-W0-001 验收通过` 结尾。

---

## Windows 适配要点（6 条）

以下问题在中文 Windows 环境下必现，已全部修复。新机器搭建时若遇到同类症状可对照排查。

---

### ① make 必须用 MSYS2 版，ezwinports 版有中文食谱行崩溃缺陷

**症状**：`make bootstrap` 报错 `Makefile:xx: *** recipe commences before first target. Stop.`

**原因**：ezwinports 发行的 `make.exe` 对含中文注释/食谱行的 Makefile 解析存在 bug。

**修复**：安装 MSYS2 版 make，并将其路径加入 `PATH`（若用 Git Bash 则已自带，路径为 `/usr/bin/make` 映射到 MSYS2）。

```bash
# 验证 make 版本和来源
which make
# 预期：/usr/bin/make（Git Bash/MSYS2）
# 错误：/mingw64/bin/make 或其他 ezwinports 路径

make --version | head -1
# 预期：GNU Make 4.4.x（MSYS2）
```

> 本项目的 `Makefile` 顶部已声明 `SHELL := /bin/bash`，配合 Git Bash 使用即可。

---

### ② docker 不在默认 PATH，需手动追加

**症状**：`docker: command not found`

**原因**：Windows 上 Docker Desktop 的可执行文件位于 `C:\Program Files\Docker\Docker\resources\bin\`，不在系统默认 PATH 中。

**修复**：每个终端会话执行前，先追加 PATH：

```bash
export PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"
```

验证：

```bash
docker --version
# Docker version 28.x.x
```

---

### ③ docker-compose.yml 顶部添加 `name: muti-platform`（中文目录名导致 compose 项目名为空）

**症状**：`docker compose up` 报错 `project name "" is invalid`

**原因**：Docker Compose 默认取当前目录名作为项目名。当目录名为中文（如"中小学教辅材料"）时，Compose 将其规范化为空字符串。

**修复**：在 `docker-compose.yml` 文件顶部显式声明项目名：

```yaml
name: muti-platform
```

此修复已在仓库中内置，新克隆无需额外操作。

---

### ④ Makefile 顶部 `-include .env` 并 export 数据库/MinIO 变量（修复 pg_isready -U 空参数）

**症状**：`pg_isready -U ` 报错 `missing argument for option "-U"`

**原因**：`docker-compose.yml` 中 healthcheck 使用了 `${POSTGRES_USER}` 变量。若 Makefile 未 export 这些变量，Compose 在 `docker compose exec` 上下文中无法获取变量值，导致参数为空。

**修复**：`Makefile` 顶部已添加：

```makefile
-include .env
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB MINIO_ROOT_USER MINIO_ROOT_PASSWORD
```

此修复已在仓库中内置。

---

### ⑤ Git Bash 默认 python 是 3.8，必须激活 .venv 用 3.12

**症状**：`python -V` 输出 `Python 3.8.x`，项目运行时报语法错误（不兼容 3.12 语法）或依赖缺失。

**原因**：Git Bash 默认从 Windows 系统 PATH 中取 `python`，可能指向系统自带的旧版本。

**修复**：每次打开终端后先激活项目虚拟环境：

```bash
source .venv/Scripts/activate
python -V
# 预期：Python 3.12.x
```

验证虚拟环境中 python 版本正确后，再进行后续操作。

---

### ⑥ GBK 控制台需 `PYTHONIOENCODING=utf-8` 否则中文输出崩溃

**症状**：执行 Python 脚本或 pytest 时，中文输出报 `UnicodeEncodeError: 'gbk' codec can't encode character`

**原因**：中文 Windows 默认终端编码为 GBK（CP936），Python 的 stdout/stderr 默认跟随系统编码。当代码输出（或错误信息）包含 GBK 不支持的字符时崩溃。

**修复**：每个终端会话执行前设置环境变量：

```bash
export PYTHONIOENCODING=utf-8
```

此设置对所有 Python 子进程生效（pytest、alembic 等）。

---

## 终端启动速查

每次新开 Git Bash / PowerShell 终端时，执行以下命令完成环境准备：

```bash
# 进入项目目录
cd /d/开发项目/中小学教辅材料

# ① 追加 Docker 到 PATH
export PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"

# ② 设置 Python 输出编码
export PYTHONIOENCODING=utf-8

# ③ 激活虚拟环境
source .venv/Scripts/activate

# 验证环境
python -V
# 预期：Python 3.12.x

docker compose ps
# 预期：显示三个容器状态
```

---

## 数据卷持久化验证

```bash
# 1. 停止所有容器
docker compose down

# 2. 重新启动
docker compose up -d --wait

# 3. 连接 PostgreSQL 验证数据仍在
docker compose exec db psql -U muti -d muti_dev -c "\dt"
# 应显示之前创建的数据表

# 4. 运行 bootstrap 再次确认
make bootstrap
# 应输出三行 ✅
```

---

## 常见问题排查

| 现象 | 可能原因 | 解决 |
|------|----------|------|
| `docker compose up` 报 `port already in use` | 本机已运行 PG/Redis 占用端口 | 停掉本机服务或修改 `docker-compose.yml` 端口映射 |
| `make: command not found` | 未安装 MSYS2 make | `pacman -S make`（MSYS2 终端内）或使用 Git Bash |
| `pg_isready` 连接被拒 | PG 容器未就绪 | 等待 healthcheck 通过后重试 |
| `.venv` 激活失败（PowerShell 执行策略） | PowerShell 默认禁止脚本执行 | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |

---

## 环境信息（参考）

本笔记基于以下环境验证通过：

- **OS**: Windows 11 中文版
- **Docker Desktop**: 28.x
- **Python**: 3.12.x（via .venv）
- **Make**: GNU Make 4.4.x（MSYS2）
- **Shell**: Git Bash / PowerShell 5
- **PostgreSQL**: 16-alpine
- **Redis**: 7-alpine
- **MinIO**: latest

## 刻意未覆盖项（延迟计划）

- **无头 Chromium**（渲染管线）：W2 交付域开发时引入（渲染 Worker），W0/W1 不需要。
- **SymPy**（数学验算）：W2 校验域数学包验算时引入，W0/W1 不需要。
- **生产依赖 `requirements.txt` / `pyproject.toml`**：归 T-W1-001 交付。
- 镜像已按 digest 锁定（docker-compose.yml），升级镜像属显式变更，需同步验证本地与 CI。
