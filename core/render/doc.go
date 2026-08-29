// Package render 承载渲染核心域：ItemVersion → RenderIR（内容与样式分离的
// 中间态）→ 多出口渲染（HTML 片段），以及卷面追溯码与学段适配
// （Python 冻结实现 src/core/render/ 的 Go 重锚定——T-W2-033/034/037、T-W4-037）。
//
// 语义对齐边界（与冻结实现逐函数对照）：
//   - ir.go / item_to_ir.go ← ir.py / item_to_ir.py：强类型 IR 与 permissive
//     content.blocks 的字段映射（text/fill/choice/math_svg/group 五种块）；
//   - html_renderer.go ← html_renderer.py：f-string + html.escape 的 string
//     builder 同构移植，输出结构与冻结实现逐字符对齐（题干/选项/答题区/追溯行）；
//   - trace_codes.go ← trace_codes.py：Luhn 卷码/QR payload/题短码（A4 回溯链）；
//   - gradeband.go + phonetic.go ← gradeband_adapter.py + components/phonetic_overlay.py。
//
// 设计铁律（与冻结实现一致）：
//   - 内容与样式分离：IR 不含任何 CSS/HTML，样式由品牌模板决定；
//   - 学科零特判（A5/X6）：本包是核心域，不 import 任何学科包/学段包
//     （import-boundary lint 强制）；学科 SVG 经注册表挂载后以 math_svg 块
//     原样透传进 IR；
//   - HTML 安全（验收 #4）：所有用户内容转义；SVG 块白名单校验（拒
//     script/事件属性/javascript: 链接），输出无 <script、无 on* 属性；
//   - IR 不可变：任何适配（学段/注音）返回副本，绝不修改入参。
//
// 非目标（IO 面，本波留白不在 Go 核心域实现）：PDF 导出（pdf_exporter.py）、
// 周批处理（weekly_batch.py）、页面级模板装配（templates/ + brand/，依赖
// embed/文件系统取数）、QR 码 SVG 位图生成（Python 侧 qrcode 库，Go 侧
// 零新依赖约束下仅留显式骨架 GenerateQRSVG）。
package render
