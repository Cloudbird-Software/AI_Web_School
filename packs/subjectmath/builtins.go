package subjectmath

// builtins.go —— 内置母题登记。新增母题 = 新文件生成器实现 + 在此追加一行。

var builtinGenerators []Generator

func init() {
	builtins := []Generator{}
	if g, err := newIntMulGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newFracGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newConvGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	builtinGenerators = builtins
}
