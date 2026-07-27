# 内容来源登记表（content/sources/registry.yaml）

> 宪法 R-Q-18：素材/语料库入库必须持 approved 状态的许可；无登记或 `decision!=approved` 的来源不得入库。

## 一、登记规则

任何引入内容的素材/语料库/字体/字库/词表/方法论，**必须**先在本文件中登记一条记录，并在 `content/sources/registry.yaml` 中给出 `decision` 与 `expires_at` 字段。

- 未登记 → CI 拦截（`tools/ci/check_sources.py` 非零退出）。
- `decision=rejected` 或 `decision=expired` → CI 拦截。
- `decision=approved` 但 `expires_at` 已过期 → 视为不可用，CI 拦截。

## 二、字段规范（YAML）

| 字段           | 类型     | 必填 | 说明                                                       |
| -------------- | -------- | ---- | ---------------------------------------------------------- |
| `license_id`   | string   | ✅   | 唯一标识；被 material_version / corpus_version 引用       |
| `source`       | string   | ✅   | 来源公开名称                                               |
| `rights_holder`| string   | 可空 | 权利人                                                     |
| `scope`        | string   | 可空 | 授权范围（用途限定）                                       |
| `expires_at`   | datetime | 可空 | 期限；`null` 表示永久；过去时间表示已过期                  |
| `decision`     | enum     | ✅   | `approved` / `rejected` / `expired`                         |
| `kind`         | string   | 可空 | 来源类型（curriculum/wordlist/font/methodology/textbook） |
| `notes`        | string   | 可空 | 备注（可商用初判、法务复核状态等）                         |
| `registered_at`| datetime | 可空 | 登记日期                                                   |

字段 `license_id / source / rights_holder / scope / expires_at / decision` 与
`material_license` 表逐字对齐（迁移 0002）；`kind / notes / registered_at`
为 YAML 元数据，仅用于人类追溯，不进 DB。

## 三、加载与查询

`src/core/content/source_registry.py::SourceRegistry` 提供唯一加载入口：

```python
from src.core.content.source_registry import SourceRegistry

reg = SourceRegistry.from_yaml()  # 默认加载 content/sources/registry.yaml
reg.get_license("lic-pypinyin-mit")  # → SourceRecord | None
reg.is_approved("lic-pypinyin-mit")  # → bool
reg.all_approved()                  # → list[SourceRecord]
```

## 四、CI 拦截

`tools/ci/check_sources.py`：

```bash
python tools/ci/check_sources.py
#   退出 0 = 全部 license_id 已登记且 approved（或无任何引用）
#   退出 1 = 发现未登记 / decision!=approved / 已过期 的 license_id
#   退出 2 = registry.yaml 自身 schema 校验失败
```

扫描范围：`content/**/*.{yaml,yml,json}`，自动跳过 `registry.yaml` 自身。

## 五、已登记来源总览

| 来源                                       | 类型         | 决策       | 备注                          |
| ------------------------------------------ | ------------ | ---------- | ----------------------------- |
| 义务教育数学课程标准（2022 年版）          | curriculum   | approved   | 官方公开，作事实引用          |
| 义务教育语文课程标准（2022 年版）          | curriculum   | approved   | 官方公开                      |
| pypinyin                                   | library      | approved   | MIT 许可证                    |
| 古诗文原文                                 | text-corpus  | approved   | 公有领域                      |
| 义务教育教科书·数学（人教版）               | textbook     | approved   | 课堂教学合理使用（至 2030）  |
| Make Me a Hanzi                            | font-data    | rejected   | Arphic PL + 附加条件待复核   |
| THUOCL 清华大学开放中文词库                 | wordlist     | rejected   | 许可表述模糊                  |
| Eedi/NeurIPS2020                           | methodology  | rejected   | 仅研究用途，仅借鉴方法        |
| NUCLE/CoNLL-2014                           | methodology  | expired    | 非商业，2025-12-31 已过期     |

完整数据见 `registry.yaml`。
