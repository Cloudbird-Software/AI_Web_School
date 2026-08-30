# ADR-0008: 教研工作台处置决定——过渡保留（退役冻结），不 Go 化（GO-RW-013）

> 状态：**已批准（Accepted）**——owner 授权会话代裁（2026-08-30，#138 决策项，
> "按照建议处理"授权口径）。关联：GO-RW-013（#138）· ADR-0004 §四 · ADR-0007 ·
> languages.yaml `layers.application` / `layers.frontend`。

## 一、决策对象

教研工作台 = `src/workbench/`（Streamlit 教研端控制台：面向教研人员的看板/
查询页，非学生面）。GO-RW-013 留待决策的选项即题面两案：**Go 化重写** 或
**过渡保留**。

## 二、决定：过渡保留（退役冻结），不 Go 化

1. **不重写**。workbench 随 ADR-0007 整体转退役冻结归档（src/** 逐字节钉扎，
   安全修复外禁改）——它已是"过渡保留"的最终形态，且不可再接新功能。
2. **替代路径不变**：ADR-0004 §四既定"workbench 由 W7 学生端取代"。教研
   侧未来若有控制台需求，形态是 **TS 前端消费 Go API**（languages.yaml：
   前端=typescript only；应用=go only），消费面（core/report 弱项报告、
   core/datastat 覆盖度/健康度、core/monitor 成本仪表盘）已全部 Go 化就绪
   ——届时是"给已有 Go 内核配前端"，不是移植 Python UI。
3. **现在 Go 化一个 UI 壳是负价值**：W7 前没有教研端消费者；把 Streamlit
   壳翻译成 Go 服务端渲染等于为新波次预付零反馈的债务，违反"扩展=加资产"
   的结构目标。

## 三、后果

- GO-RW-013 以本 ADR 关闭：16 张 GO-RW 卡全部收口，无一悬空。
- 无 Python 复活面：workbench 的任何"再用"都走 ADR 例外，且受 src 冻结
  机器强制（tools/srcfreeze）拦截。
- 教研分析能力的**实质**（报告/覆盖度/成本五指标）零损失——它们是 Go 内核
  资产，与退役的展示壳解耦。
