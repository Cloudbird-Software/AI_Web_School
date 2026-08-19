package registry

import "errors"

// ErrDuplicate 表示注册表 id 冲突（条目只增不改，覆盖即错误）。
var ErrDuplicate = errors.New("registry: duplicate id")
