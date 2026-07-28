# 小程序发布前就绪清单（OPC §6.6）

> 本文件由 T-W4-051 交付，对照 ADR/OPC-AI原生开发组织方案.md §6.6 的 7 项条件。
> 机器可验部分由 `scripts/wave-exit/check_release_readiness.py` 承载；本清单供人类最终确认。
> 状态图例：✅ 机器可验通过 ｜ ⚠️ 机器可验部分通过（待人工补齐） ｜ ❌ 缺失 ｜ 🕐 待人工确认

## 7 项就绪条件

### 1. API v1 冻结（OpenAPI + 契约测试常绿）
- **机器可验**：
  - `specs/contracts/api/openapi-v1.yaml`（或等价路径）存在；
  - `specs/contracts/FROZEN.txt` 登记 API 契约；
  - `tests/contract/test_api_frozen.py` 契约冻结测试存在；
  - `scripts/ci/check_openapi_diff.py` 契约 diff 脚本存在。
- **承载任务卡**：T-W4-042（API 冻结）。
- **出口映射**：E2E-10（不退化-API 冻结部分）。
- 🕐 待人工确认：契约测试在 CI 常绿（连续 2 周 nightly 全绿）。

### 2. 学生模拟器跑通全部 C 端链路
- **机器可验**：
  - `tests/simulator/test_practice_e2e.py`（练习链路）；
  - `tests/simulator/test_diagnosis_review_e2e.py`（诊断+复习链路）；
  - `tests/unit/test_simulator_client.py`（参考客户端）。
- **承载任务卡**：T-W4-043/044/045。
- **出口映射**：E2E-1（学生模拟器全链路）。
- 🕐 待人工确认：微信端特有能力清单（扫码入口、小程序音频播放与限次、包体/组件渲染差异、微信登录）标注为「已知未验证项」，正式移交小程序设计阶段——不构成隐瞒，构成清单。

### 3. 内容就绪（三科首批 3–4 年级）
- **机器可验**：
  - 三科学科包目录存在：`src/packs/subject-{math,chinese,english}`；
  - 语料/模板目录非空（占位检查）。
- **承载任务卡**：W2/W3 学科包 + W4 C/D 线。
- 🕐 待人工确认（OPC §7.3 数量目标）：
  - 图谱节点：数学 ~400 / 语文 ~800 / 英语 ~500；
  - 错误类型：数学 80+ / 语文 60+ / 英语 50+；
  - 语料库：字库 3500 常用字、词库 2 万+、篇目库、课标词表 700+；
  - 母题/蓝图：三科各 300+（A/B 级）+ 阅读素材 100+ 篇 + 命题蓝图 60+；
  - 黄金数据集：100 母题实例化期望 + 200 份标注作答；
  - 周更卷三科可产需运营确认。

### 4. 黄金路径 30 题型 + 黄金数据集 + nightly 重放
- **机器可验**：
  - `tests/golden-path/` 测试函数数 ≥ 30（题型覆盖）；
  - `tests/golden/` 黄金数据集目录存在。
- **承载任务卡**：W0/W1 黄金路径 + W4 重放（T-W4-003/046）。
- **出口映射**：E2E-8（重放）。
- 🕐 待人工确认：nightly 重放全绿连续 2 周（需 CI 历史记录）。

### 5. 性能达标（组卷 p95<2s、批改 10s 级）
- **机器可验**：
  - `tests/performance/report_assembly.md` 含 p95 指标；
  - `tests/performance/report_grading.md` 含批改延迟指标；
  - 压测脚本存在：`test_assembly_latency.py` / `test_grading_latency.py`。
- **承载任务卡**：T-W4-038/039。
- **出口映射**：E2E-7（性能）。
- 🕐 待人工确认：最新一次压测运行结果达标（p95<2s / 批改 10s 级）。

### 6. 合规项机器可验全过
- **机器可验**：
  - `tests/contract/test_no_ranking_query.py`（无跨用户排名查询路径）；
  - `scripts/ci/check_no_ranking.py`（静态实证扫描）；
  - `tests/unit/test_pii_vault.py`（PII 保险库 schema）；
  - `tests/unit/test_parental_consent.py`（家长授权）；
  - `tests/unit/test_duration_guard.py`（时长保护触发）；
  - `tests/unit/test_redaction.py`（姓名 redaction）。
- **承载任务卡**：T-W4-031/032/033/034。
- **出口映射**：E2E-6（合规）。
- 宪法铁律 #6：PII 只在保险库 schema；调用 LLM/TTS 前必须剥离。
- 宪法铁律 #7：禁止跨用户排名查询。

### 7. 运维就绪（备份恢复/监控告警/成本仪表盘）
- **机器可验**：
  - `scripts/ops/backup.sh`（备份脚本）；
  - `scripts/ops/backup_verify.py`（备份校验）；
  - `tests/unit/test_cost_dashboard.py`（成本仪表盘）；
  - 监控/健康端点路由存在（`src/api/routers/health.py` 或 main.py 注册）。
- **承载任务卡**：T-W4-040/041。
- 🕐 待人工确认：
  - 备份恢复演练记录已归档；
  - 监控告警规则已接入运维通道。

## 机器可验核验

```bash
# 一键核验（输出结构化报告 + 缺失项列表）
python scripts/wave-exit/check_release_readiness.py

# JSON 输出（供程序消费）
python scripts/wave-exit/check_release_readiness.py --json
```

退出码：0=全部机器可验项通过；1=有缺失项（供人类决策）。

## 人类最终确认

机器可验部分通过仅代表「证据存在」，不等于「就绪」。发布前人类须逐项确认：
1. API 契约测试在 CI 连续 2 周常绿；
2. 三科首批入库量达标（§7.3）；
3. nightly 重放连续 2 周全绿；
4. 最新压测结果达标；
5. 备份恢复演练已执行并归档；
6. 监控告警已接入；
7. 微信端特有能力清单已移交小程序设计阶段。

## 出口承载

- **E2E-1~10**：由 `scripts/wave-exit/w4.sh`（T-W4-050）承载机器可验部分；
- **就绪清单核验**：由 `scripts/wave-exit/check_release_readiness.py`（T-W4-051）承载；
- **最终出口**：T-W4-T05 验证卡引用 w4.sh + check_release_readiness.py。
