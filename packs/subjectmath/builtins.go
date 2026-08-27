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
	if g, err := newRoundGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newFracAddGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newDecGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newAddSubGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newMulDivGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newTimeGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	if g, err := newGeoGen(); err != nil {
		panic(err)
	} else {
		builtins = append(builtins, g)
	}
	builtinGenerators = builtins
}
