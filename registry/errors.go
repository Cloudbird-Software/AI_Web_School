package registry

import "errors"

// ErrDuplicate 表示注册表 id 冲突（条目只增不改，覆盖即错误）。
var ErrDuplicate = errors.New("registry: duplicate id")

// ErrInvalidContract 表示评分器条目的契约声明面缺失或不合规（T-W5-016）——
// 注册即失败（fail-loud），细分原因见 wrap 文本。
var ErrInvalidContract = errors.New("registry: 评分器契约声明不合规")

// ErrInvalidResult 表示评分器返回的判定违反输出契约（verdict 形态：置信度
// 越界/残缺数值/模型身份与确定性声明不符）——评分链路在装配 trace 前拦截。
var ErrInvalidResult = errors.New("registry: 评分结果违反输出契约")
