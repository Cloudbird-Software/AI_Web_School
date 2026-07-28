"""低学段包（GradeBandPack L，1–2 年级）.

T-W4-035：完整参数包——全文注音开关 / 题面朗读按钮 / 大字号大按钮 /
数字键盘 / 题量上限 / 闯关形态。参数以 YAML 配置 + Python 渲染提示器
形式交付，供渲染与交互层（T-W4-037 学段适配层）及组卷约束层
（T-W4-036 学段 overlay）消费。

宪法 A5：核心域不 import 本学段包；核心通过学段参数注入消费（render_hints
产出的 dict 由调用方注入核心适配层，config.yaml 由调用方加载为 overlay dict）。
"""
