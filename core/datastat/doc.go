// Package datastat 承载数据域统计内核（Python 冻结实现 src/core/data/ 的
// Go 重锚定，PyR 波次）：CTT 经典测量理论、先验/实测贝叶斯收缩、掌握度 Elo、
// 五轴覆盖缺口、题目健康度与生命周期状态机、增量重判/全量重放聚合核。
//
// 北极星（V2）：真实作答参数（难度/区分度/错误模式命中率）是唯一不可复制的
// 资产——本包是参数资产的可信底座，全部统计公式与冻结实现逐行对齐，数值
// 正确性由手算地面真值单测钉死（同输入必同输出，D6 可重放）。
//
// 场景口径（D5 参数分场景）：practice/diagnosis/measurement 三值域在包级
// 统一声明（ValidPurposeScope）；所有估计入口必填单值场景，越域 fail-closed
// （ErrInvalidPurposeScope），结构上不存在跨场景聚合路径。
//
// 为什么叫 datastat 而非 data：db 层已有数据访问包，本包是纯统计核，不碰
// 连接串/ORM；Python 侧 src/core/data 的对应物在此重锚定。
//
// 纯函数与 IO 分离（与冻结实现同构：compute_ctt 吃记录列表无副作用，DB 取数
// 由 run_* 承担）。IO 面在本波显式留白、如实声明，由服务化接线波次填充：
//   - CTT 标定落库（ctt.run_ctt_calibration 的 AsyncSession 面）——取数 SQL
//     按 scene 精确过滤后把 []ResponseRecord 注入 ComputeCtt；
//   - 覆盖缺口取数（coverage_gap._fetch_actual_counts 的 4 轴聚合 SQL）——
//     actualCounts 注入 ComputeCoverageGap；
//   - 健康度取数（health.evaluate_health 的三段 SQL：事件/实测区分度/选项
//     结构）——事件视图与选项结构注入 EvaluateHealth；
//   - 生命周期转换落账（health.transition_lifecycle 的 append-only INSERT）——
//     规则面由 ValidateTransition 纯函数承担；
//   - 重放事件快照取数、评分器调度、score_run 平行落账（replay 的 DB 面）——
//     Rescorer / ScoreRunSink 接口声明，本波不实现；
//   - Parquet 归档（parquet_export.py，pyarrow 依赖）——ParquetExporter
//     接口声明、零新依赖约束下本波不实现。
//
// 宪法 A5/X6：本包不 import 任何学科包/学段包（依赖方向由 import-boundary
// lint 强制）；零新依赖，仅标准库。
package datastat
