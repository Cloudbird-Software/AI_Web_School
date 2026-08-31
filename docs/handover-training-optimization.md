# 训练优化前就绪 · 交接文档（2026-08-31）

> 承接 `docs/handover-pm-readiness.md`（工程侧就绪）。本文档面向 AI 项目经理，覆盖"专用小模型训练/引入"路线的公开信息搜集结论、沙盒已验证项、待 GPU 验证项与操作需求。目标：PM 上来即可按清单逐项开工，无需再做选型调研。

## 0. 结论摘要（一屏版）

| 工作流 | 开源现成方案 | 结论 | 状态 |
|---|---|---|---|
| A. 题目难度先验 | 无开箱即用模型；有参考代码（PoCW-IRT-Calibrator）与方法论论文 | 自训，但方法论+数据集齐全，训练量小 | 需 V100（轻量） |
| B. 学段适龄度分类 | **确认无**符合"中文小学学段三分类"的现成开源模型 | 自训（规则打底 + encoder 微调） | 需 V100（轻量） |
| C. IRT 2PL 标定 | girth（Georgia Tech）| **零训练直接用**，沙盒已验证 | ✅ 已验证 |
| D. FSRS 复习排程 | go-fsrs（官方 Go 模块）+ py-fsrs | **零训练直接用**，沙盒已验证 | ✅ 已验证 |
| E. 模拟学生引擎 | 无现成框架；方法论由论文支持 | 软件工程任务（LLM API + 错误类型库） | 零 GPU，纯开发 |
| F. 手写 OCR | GLM-OCR（0.9B, MIT）+ PaddleOCR + TAL_OCR_Composed_37K | **先测开源工具，测不过才自训**（用户既定逻辑） | 需 V100（部署测试） |

仓库工程状态：`go build ./...` + `go test ./core/... ./cmd/...` 全绿（2026-08-31 复核），训练优化前就绪。

## 1. 沙盒已验证项（CPU 即装即用，PM 可直接复现）

沙盒环境 Python 3.14.7 / pip 26.2.1，以下命令已实跑通过：

### 1.1 py-fsrs（MIT）

```bash
pip install fsrs   # 安装 fsrs-6.3.2
```

```python
from fsrs import Scheduler, Card, Rating
from datetime import datetime, timezone
s = Scheduler()
result = s.review_card(Card(), Rating.Good, datetime(2026,8,31,tzinfo=timezone.utc))
card, review_log = result          # 返回 (Card, ReviewLog) 二元组
print(card.due, card.state)
# 实测输出：2026-08-31 00:10:00+00:00 1（Learning 态，10 分钟后再复习）
```

注意：`review_card` 返回 `(card, review_log)`，不是 `[item.card, ...]`。FSRS 6 的 `Scheduler` 默认 21 参数，自定义参数时勿按 FSRS 5 的 19 参数旧文档写。

### 1.2 girth IRT（佐治亚理工）

```bash
pip install girth
```

```python
import girth, numpy as np
rng = np.random.default_rng(42)
theta = rng.normal(size=500); b = rng.normal(size=60); a = rng.uniform(0.5, 2, 60)
p = 1/(1+np.exp(-(np.outer(theta, a) + b)))
resp = (rng.uniform(size=(500,60)) < p).astype(int)   # 学生×题 0/1 矩阵
est = girth.twopl_mml(resp.T)   # 注意：输入是 题×学生，需转置
# est keys: Discrimination / Difficulty / Ability / LatentPDF / AIC / BIC
```

实测结果（合成 500 生 × 60 题，纯 CPU 秒级）：
- 难度 Spearman |ρ| = 0.954，区分度 ρ = 0.941
- **坑 1**：API 是 `twopl_mml` 不是 `two_param_mml`；输入矩阵方向是题×学生。
- **坑 2**：girth 的 Difficulty 符号约定与本仓库 `DifficultyToRating`（p 越小题越难 → 评级越高）相反，接入时做一次符号对齐并用手算地面真值单测钉死（对齐 `core/datastat` 冻结实现纪律）。
- 3PL/GRM/PCM 也有：`threepl_mml / grm_mml / pcm_mml`；能力估计 `ability_3pl_eap` 等。

## 2. 各工作流详细方案

### A. 题目难度先验预测器（第一优先）

**公开信息搜集结论**：没有可直接下载的"题目文本→难度"开源模型。但有三类高价值参考：

1. **参考代码**：[PoCW-IRT-Calibrator](https://github.com/Cognitive-Layer-Labs/PoCW-IRT-Calibrator)（已归档但代码完整）——用 Ollama 小 LLM 当"合成考官"批量作答 → 拟合 IRT → 训练 XGBoost 从题目文本直接预测 IRT 参数。与本仓库飞轮架构（模拟学生→标定→先验）同构，训练脚本可改造复用。
2. **方法论论文（已验证可行）**：
   - *Take Out Your Calculators*（arXiv:2601.09953，UMD）：LLM 角色扮演不同水平学生作答 → 拟合 IRT → 与真实难度（NAEP）相关 **0.75–0.82**；且弱数学能力模型（Gemma）预测反而更好——选模拟器时不必上强模型。
   - *Can LLMs Estimate Student Struggles?*（arXiv:2512.18880，附代码 [Difficulty_Alignment](https://github.com/MingLiiii/Difficulty_Alignment)）：直接让 LLM 判难**不可靠**（20 个模型系统性错位），必须走"模拟作答→统计拟合"路线——这正好是我们已有的 CTT/calibrate 管线。
3. **训练数据集（均公开）**：
   - MathE（HuggingFace，数学题+学生作答，可自算 CTT 难度标签）
   - Eedi/NeurIPS 2020（选择题+知识点树）
   - ASSISTments、Junyi Academy（Kaggle）
   - 注意：这些数据集**只做模型训练/评测**，题目内容不进内容库（P-ITEM 纪律：不搬运外部题目）。

**建议路线（两条腿）**：
- 腿 1（零 GPU 标定增强）：把 girth 2PL 接进 `core/datastat`（见 C 节），让现有 `cmd/calibrate` 多产出 IRT 参数。
- 腿 2（V100 训练）：`bge-small-zh-v1.5`（或 `bert-base-chinese`）嵌入 + LightGBM 回归头，在 MathE/Eedi 自算标签上预训练 → 用仓库合成作答数据对齐分布。**冷启动期同时跑"LLM 模拟学生→IRT"作为无监督先验**（论文证明 ρ>0.75），与监督模型互验。
- 落地：写 `item_param`，`source=prior_learned`（与 `measured_ctt` 严格分账，D5/D6），喂 `shrinkage.go` 贝叶斯收缩与组卷"目标正确率区间"约束。
- 验收：holdout 题集上预测难度 vs 实测 CTT 难度 Spearman ρ > 0.6，写进 `specs/contracts/TRACEABILITY.md`。

### B. 学段适龄度/可读性分类器（需自训——已确认无现成方案）

**搜集结论**：逐项排查后确认没有"中文小学 L/M/H 学段三分类"的开源模型：
- AlphaReadabilityChinese（SISU）：桌面 GUI 工具，非库；产出词汇/句法/语义指数，不输出学段。
- cntext：经典公式（Fog/SMOG 等），对短题干失真严重（示例文本 fog_index=120）。
- hsk-text-analyzer：对外汉语 HSK 1-6 词覆盖，不是课标学段。
- ROCILING 2024 论文：结论恰是"中文可读性改写/评估 LLM 零样本效果有限"——支持自训而非 prompt 兜底。

**训练方案**：
- 标签来源：课标附录字词表（事实依据，可商用）做规则弱标注 → 分级读物/教材目录结构交叉验证 → 500-2000 条人工校验种子。
- 模型：`bert-base-chinese` 或 `RoBERTa-wwm-ext` 三分类（L/M/H），V100 半天内可完成多轮。
- 英语侧：课标词表等级校验（题型矩阵 B.3 已列）+ CEFR 分级语料。
- 落地：注册为**校验门插件**（架构 v2 §4.3"语篇难度分级校验"缺口），只产证据分不改变门语义；同时作为压测工具批量扫描全题库适龄度一致性。

### C. IRT 2PL/3PL 标定（零训练，已验证，建议第一个做）

- 库：girth（已沙盒验证，见 §1.2）；备选 py-irt（MIT，Pyro/PyTorch 变分推断，1PL/2PL/4PL，GPU 可加速大规模数据）。
- 集成点：`core/datastat` 新增 IRT 统计核（纯函数面），或 CLI 作业侧用 Python 包装。**Go 侧数值正确性纪律**：若在 Go 重锚定，必须像 CTT 一样用手算地面真值单测钉死；若走 Python 作业面（`cmd/calibrate` 外挂），记录 method_version=`irt-2pl-v1`。
- 价值：IRT 把学生能力与题难度放到同一量表（CTT 只有正确率+点二列），是组卷"目标正确率区间"约束从"冷启动降级"走向精确预测的前置，也是 CAT（次年规划）的基础。
- 注意符号约定差异（§1.2 坑 2）。

### D. FSRS 复习排程（零训练，已验证，建议第二个做）

- **关键发现：FSRS 有官方 Golang 模块 [go-fsrs](https://github.com/open-spaced-repetition/go-fsrs)**——本仓库是 Go 技术栈，可原生集成，无需 Python 边界。py-fsrs 已沙盒验证（§1.1），用于参数优化器（`fsrs.optimizer`）离线调参。
- 集成点：架构 v2 已把 FSRS 列为 ReviewPolicy v2 候选；`core/review/sync.go` 复习队列写入路径已就绪，替换固定间隔表（1/3/7/21 天）为 FSRS 调度即可。
- 改造点（论文已注明）：复习对象挂接知识点与错误类型归因，非闪卡照搬——即 Card 的 identity 是 (student_alias, kp_code, error_type) 而非题目本身。
- 参数优化：真实作答回流后用 `fsrs-optimizer` 在 V100/CPU 上重估 21 参数（`fsrs` 包 Optimizer 类，PyTorch 依赖）。
- 新依赖 go-fsrs/py-fsrs 均为 MIT；**按组织依赖政策走 owner 审批**后加入 go.mod / requirements。

### E. 模拟学生引擎（红队主武器，零 GPU，纯开发）

- 无现成框架；方法论已由 A 节论文验证（LLM 角色扮演学生 + 按错误类型定向选错）。
- 实现：step-3.7-flash（已接入 AI 总线）+ 仓库 `error_type` 库生成带特定误解的作答序列；复用 `cmd/school` 的提交路径打整条飞轮（评分→错误推断→复习队列→标定）。
- 产出双用途：① 压测/红队证据；② 知识追踪模型（第三批）未来的预训练序列。
- 纪律：合成学生走独立 alias 命名空间（如 `sim_` 前缀），`item_param` 写入时 source 必须带 `sim` 标记，禁止与真实测量混账（D5 精神）。

### F. 手写 OCR（先测开源，测不过才自训——用户既定逻辑，方向正确）

**开源工具（按测试优先级）**：
1. **GLM-OCR**（智谱，2026-02 开源）：0.9B 参数，模型 **MIT** 协议/代码 Apache-2.0，OmniDocBench V1.5 得分 94.6（SOTA 级），专攻手写体/手写公式（→LaTeX）/复杂表格/信息抽取，vLLM/SGLang/Ollama 部署，显存约 **3GB**——V100 16GB 单卡绰绰有余，甚至可与其它任务共卡。文档：
   - 模型：HuggingFace `zai-org/GLM-OCR`（或智谱 MaaS API 云端版先零门槛试用）
   - 部署：官方 `start_vllm.sh` 一键脚本，Python 3.10 + PyTorch 2.x，模型约 2.5GB 下载。
2. **PaddleOCR**（PP-OCRv4/v5）：CPU 可跑，中文印刷体成熟，手写体一般——适合做版面切题 + 印刷区域兜底，与 GLM-OCR 分工而非二选一。
3. **版面分析**：GLM-OCR 自带 PP-DocLayout 版面拆解；PDF 前处理可加 MinerU。

**数据集**：
- **TAL_OCR_Composed_37K**（好未来，用户已确认存在；HuggingFace `TAL` 组织下，沙盒网络无法直连 HF，由 PM 下载）：数学手写/印刷混合，与本项目数学竖式/口算场景高度对口。**接入前核对数据集 license**（TAL 系数据集多为 CC 系但须逐项确认，是否允许商用训练）。
- 评测基准（强烈建议先建评测再谈微调）：
  - **OmniHandwritingOCR**（ECNU-RAIL，CIKM 2026，GitHub 开源）：77,572 图像-标签对，6 子任务 12 子集，中英文手写+单/多行公式，含"事实性标注"（学生真写错的也要照错转录——与本项目"作答证据不可清洗"的纪律同构，是选型的关键基准）。
  - MathWriting（Google，230k 真人 + 400k 合成，研究用途）；HME100K；CROHME；TexTeller/Tex80M（开源 HMER SOTA 模型+80M 公式数据集，若需自训可直接当基座）。

**测试方案（PM 在 V100 上执行）**：
1. 用 TAL_OCR_Composed_37K 抽 500-1000 张 + 用户提供的真实卷子扫描件（待用户提供）构建评测集。
2. GLM-OCR（vLLM）与 PaddleOCR 各跑一遍，按 OmniHandwritingOCR 的 CER/1-NED 口径打分。
3. 分场景看：数学竖式、纯数字口算、中文手写（语文）、英文书写。
4. **通过标准建议**：整字段准确率（题目级而非字符级）≥90% 进"字段校验门 + 低置信度人工队列"流程；数学表达式 ExpRate ≥80%。不达标场景才启动微调（GLM-OCR 0.9B LoRA 微调在 V100 可行；TexTeller 为 HMER 备选基座）。
5. 产物入账纪律：OCR 结果是**作答事件的候选输入**，必须过字段校验门后入账，低置信度进人工队列，不直写三本账。

## 3. GPU（V100）需求清单 —— PM 操作项

> V100 硬件边界：fp16（无 bf16）、无 FlashAttention-2（需 Ampere+）、16/32GB。以下任务均在此边界内。

| # | 任务 | 资源 | 前置 |
|---|---|---|---|
| G1 | GLM-OCR 部署 + 评测（F 节方案） | vLLM，~3GB 显存 | 下载 GLM-OCR（2.5GB）+ TAL_37K 评测集 |
| G2 | PaddleOCR CPU/GPU 基线跑分 | 可与 G1 同卡 | pip 安装 |
| G3 | 难度先验模型训练（A 节腿 2） | bge/bert 微调 + LightGBM | MathE/Eedi 数据下载、自算标签 |
| G4 | 适龄度分类器训练（B 节） | bert-base-chinese 微调 | 课标字词表规则弱标注 + 人工种子 |
| G5 | FSRS 参数优化（D 节，可延后） | fsrs Optimizer | 真实作答序列回流（≥数千条复习记录） |

无 GPU 依赖即可开工的（建议 PM 第一周就做）：C（IRT 接入）、D（go-fsrs 接入）、E（模拟学生引擎）。

## 4. 依赖与许可备注（组织纪律）

- 新依赖须 owner 批（组织 languages.yaml#dependency_policy；禁 AGPL/GPL-3.0/SSPL）：
  - go-fsrs（MIT）→ go.mod
  - girth / py-fsrs / fsrs（MIT 系；girth 具体条款接入前在仓库内复核 LICENSE）→ requirements
- 数据集许可：MathE/Eedi/ASSISTments 用于**模型训练与评测**；题目内容不入内容库、不进校验签发；TAL_37K 与 MathWriting（研究许可）逐项法务复核；OmniHandwritingOCR 代码许可见其 GitHub。
- 所有新模型/新算法走既有纪律：AI 总线台账（model_version+prompt 版本+成本+产物 id）、判官进评分注册表、先验参数 `source=prior_learned` 分账、TRACEABILITY.md 补实证。

## 5. 验收基准（建议写进任务卡）

| 项 | 基准 |
|---|---|
| IRT 接入 | 合成数据参数恢复 Spearman \|ρ\|>0.9（已手工复现 0.95/0.94，作回归基线）；真实作答重跑 `cmd/calibrate` 出 IRT 行 |
| FSRS 接入 | go-fsrs 调度替换固定间隔表后，复习队列到期分布单调且可重放（ReviewPolicy 版本化） |
| 难度先验 | holdout ρ>0.6（vs 实测 CTT）；LLM 模拟学生路线 ρ>0.6（对齐论文 0.75 下限留余量） |
| 适龄度 | 人工种子集 accuracy>0.85（三分类） |
| OCR | 字段级准确率≥90%、公式 ExpRate≥80%（不达标才启动微调） |
| 模拟学生 | 错误类型命中率（生成的错误作答被 `inferErrorBindings` 正确归因的比例）>0.8 |

## 6. 下一步开始方式

1. PM 读 `docs/handover-pm-readiness.md`（工程操作面）+ 本文档。
2. 第一周（零 GPU）：C → D → E 并行；同时下载 G1/G3/G4 数据资源。
3. 第二周起：G1 OCR 评测 → G3/G4 训练，按 §5 基准验收。
4. 每完成一项：注册表/台账/source 分账/TRACEABILITY 四件套齐了才算完（宪法铁律 10/11）。
