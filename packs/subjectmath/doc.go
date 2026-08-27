// Package subjectmath 是数学学科包的 Go 落地（issue #34 §七 W6「数学轮」）：
// 母题（A 线 item_template）→ 结构互异实例的**确定性生成管线**。
//
// 数学轮语义（§二目标状态 2「两个轮子一根轴」）：数学轮是函数库生成 +
// 确定性验证——不经 LLM、不经 AI 台账，但产物结构与总线产物对齐下游形态
// （item_version 契约的 objective/interaction_ref/content/scoring_ref，
// specs/contracts/db/item-model.md §2.2）。同 seed 同输出可回放
// （AI 台账外的确定性路径也要可回放）。
//
// 包结构：
//   - generator.go    母题生成器接口 + 注册表（复用 registry 泛型注册表，铁律 3）
//   - int_mul.go      母题①整数乘法（单选）
//   - frac_compare.go 母题②分数比较大小（单选）
//   - unit_convert.go 母题③单位换算（数值填空）
//   - int_round.go    母题④四舍五入求近似数（数值填空；round_half_up 语义）
//   - frac_addsub.go  母题⑤同分母分数加减（单选；结果必化简）
//   - dec_compare.go  母题⑥小数大小比较（单选；补零对齐/位数陷阱）
//   - int_addsub.go   母题⑦整数进位加/退位减（数值填空）
//   - int_muldiv.go   母题⑧乘除混合运算（数值填空；同级从左往右、每步整除）
//   - unit_time.go    母题⑨时间单位换算（数值填空；60 进制量纲表，复用 conv 骨架）
//   - geo_rect.go     母题⑩长方形/正方形周长与面积（数值填空；四 KP 形态）
//   - validators.go   每母题的独立确定性 validator（与生成器零代码共享：
//     只消费已发布形态的实例文本，重新计算答案再比对——禁止生成器自证）
//   - distinct.go     结构互异判定：content 规范化摘要（键排序 sha256）+
//     两两不同断言（issue #34 §11.2 H-W6-1 机器判定口径）
//   - batch.go        批量管线：采样 → 过 validator → 结构互异去重 → 记录
//
// 谱系与门（不越界声明）：本包产出的是 pre-gate 实例草稿（tier=A 谱系、
// 门证书位为占位）。入已发布区仍须走校验门 + 内容写入服务（宪法 A3/D2）；
// 本包不做账、不加迁移（入账走后续门+账卡）。lineage.signed_at 用零值占位
// 保持字节级可回放——签名时间戳属门签发动作，不在生成器注入。
//
// 依赖纪律（X6/A5）：本包只 import stdlib + registry（packs → registry ✓）；
// core 禁止 import 本包（tools/go-lint/import-boundary 强制）。
package subjectmath
