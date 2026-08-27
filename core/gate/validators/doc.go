// Package validators 承载校验门的平台通用验证器（Go 重锚定 T-W5-020）：
// 查重验证器对真实内容摘要做唯一性判定，修复冻结实现「算了哈希却去查
// 主键列」的摘要路径失实缺陷。
//
// 冻结基线（src/core/gate/validators/generic.py DuplicatePlaceholderValidator）
// 的缺陷：先计算 artifact_payload 的规范化 sha256，再用它查询
// item_version.item_version_id 等主键列——digest 不是主键，命中永远为空，
// 查重完全失效；既有测试靠把 digest 当 ID 写入而变绿（X11「测试与实现互证」
// 教科书案例）。本包语义修正：验证器只对**内容摘要列**的登记视图判定
// （DigestSource），W6 由 DB 适配映射到 *_version.content_digest 列
// （重锚定表 §二迁移 0028 语义：查 digest 列而非主键列，带索引）。
//
// 摘要口径（宪法 D3）：CanonicalJSON / ContentDigest 是 Go 侧唯一规范化函数，
// 内容寻址与回填必须复用本函数、禁止另造口径；键序升序 + 紧凑分隔实现
// 「同内容不同空白/键序必得同一摘要」。与冻结 Python json.dumps 的浮点格式
// 不保证逐字节一致——跨语言逐字节等价不在本卡范围，Go 侧以本函数为规范源。
//
// 注册纪律（宪法 D4 对照）：验证器经 registry.Registry[Validator] 登记，
// 学科包只能复用与参数化、禁止私造查重判定；近重复（n-gram shingle Jaccard）
// 只留 NearDuplicateChecker 接口骨架与阈值常量，无任何实现——non_goals 明示
// 语义相似度查重留 W6，在此之前禁止宣称近似查重已强制（A8/X11）。
//
// 判定 fail-closed（X12）：空内容 / 非结构化根 / 哈希不可算一律 fail；
// 摘要源基础设施故障一律 review（人工复核，不放行不静默通过）。
// A5/X6：本包零学科特判，import 边界由 tools/go-lint/import-boundary 强制。
//
// T-W5-021 追加（core/gate/validators）：语篇事实核查验证器 FactCheckValidator——
// 判定基于语篇来源可对账的事实（数字/日期断言与登记事实集合确定性对账，实体
// 引用完整性核对，语义事实留 FactJudge 注入面、W6 接 BAML harness）。修正冻结
// passage_fact_check 的两处缺陷：①规则全过仍 review、置信 0.5，而编排器对任何
// review 都不签证书 → 正常语篇结构性过不了门（「永远失败的规则比没有规则更
// 危险」）——本实现判定表显式逐条列出，干净语篇必须 pass；②`blocking=True`
// 类属性把阻断性焊死在验证器里——本实现不携带阻断属性，阻断性由策略矩阵
// （W6 编排器读链配置）决定。不确定不放行延续 #79 纪律：事实登记源未挂接/
// 查询失败/集合非法/判定面故障一律 review 置信 0，不伪造 pass。
package validators
