// parse.go 承载表达式 DSL 子集的词法与语法分析（对应冻结实现的
// ast.parse(mode="eval")）。
//
// 覆盖子集：字面量（int/float/str/True/False/None）、变量名、
// 二元运算（+ - * / // % **，另将 | ^ & << >> @ 解析为待拒绝节点以给出
// "禁止的二元运算符" 语义错误而非语法错误）、一元 + -（~ 同理）、
// and/or 短路、链式比较（< <= > >= == !=，in/is 解析为待拒绝节点）、
// 三元 if/else、白名单函数直调、lambda/推导式等禁用结构的结构化拒绝。
package expr

import (
	"fmt"
	"math/big"
	"strconv"
	"strings"
	"unicode"
)

// ────────────────────────────────────────────────────────────────────
// AST 节点
// ────────────────────────────────────────────────────────────────────

// Node 是 AST 节点接口。
type Node interface{ node() }

// ExprNode 是表达式节点。
type ExprNode interface {
	Node
	isExpr()
	pos() (int, int)
}

// Constant 字面量（int/float/str/bool/None）。
type Constant struct {
	Val  Value
	line int
	col  int
}

// Name 变量引用。
type Name struct {
	ID   string
	line int
	col  int
}

// BinOp 二元运算。Allowed=false 表示运算符在白名单外（校验拒绝）。
type BinOp struct {
	X, Y    ExprNode
	OpName  string
	Allowed bool
	line    int
	col     int
}

// UnaryOp 一元运算（UAdd/USub/Not 允许；BitNot 拒绝）。
type UnaryOp struct {
	X       ExprNode
	OpName  string
	Allowed bool
	line    int
	col     int
}

// BoolOp and/or 短路链。
type BoolOp struct {
	Values []ExprNode
	IsAnd  bool
	line   int
	col    int
}

// Compare 链式比较。任一 op 不在白名单时校验整体拒绝。
type Compare struct {
	Left        ExprNode
	Ops         []compareOp
	Comparators []ExprNode
	line        int
	col         int
}

type compareOp struct {
	name    string
	allowed bool
}

// IfExp 三元条件表达式。
type IfExp struct {
	Test, Body, Orelse ExprNode
	line               int
	col                int
}

// Call 白名单函数直调。
type Call struct {
	Func    ExprNode // 静态校验要求为 *Name
	Args    []ExprNode
	HasKw   bool
	Starred bool
	line    int
	col     int
}

// Attribute 属性访问（禁止）。
type Attribute struct {
	Obj  ExprNode
	Name string
	line int
	col  int
}

// Subscript 下标（禁止）。
type Subscript struct {
	Obj  ExprNode
	line int
	col  int
}

// Forbidden 其余一切禁用节点（List/Tuple/Dict/Lambda/JoinedStr/...）。
type Forbidden struct {
	Kind string
	line int
	col  int
}

func (*Constant) node()  {}
func (*Name) node()      {}
func (*BinOp) node()     {}
func (*UnaryOp) node()   {}
func (*BoolOp) node()    {}
func (*Compare) node()   {}
func (*IfExp) node()     {}
func (*Call) node()      {}
func (*Attribute) node() {}
func (*Subscript) node() {}
func (*Forbidden) node() {}

func (*Constant) isExpr()  {}
func (*Name) isExpr()      {}
func (*BinOp) isExpr()     {}
func (*UnaryOp) isExpr()   {}
func (*BoolOp) isExpr()    {}
func (*Compare) isExpr()   {}
func (*IfExp) isExpr()     {}
func (*Call) isExpr()      {}
func (*Attribute) isExpr() {}
func (*Subscript) isExpr() {}
func (*Forbidden) isExpr() {}

func (n *Constant) pos() (int, int)  { return n.line, n.col }
func (n *Name) pos() (int, int)      { return n.line, n.col }
func (n *BinOp) pos() (int, int)     { return n.line, n.col }
func (n *UnaryOp) pos() (int, int)   { return n.line, n.col }
func (n *BoolOp) pos() (int, int)    { return n.line, n.col }
func (n *Compare) pos() (int, int)   { return n.line, n.col }
func (n *IfExp) pos() (int, int)     { return n.line, n.col }
func (n *Call) pos() (int, int)      { return n.line, n.col }
func (n *Attribute) pos() (int, int) { return n.line, n.col }
func (n *Subscript) pos() (int, int) { return n.line, n.col }
func (n *Forbidden) pos() (int, int) { return n.line, n.col }

// nodeKindName 返回节点类型名（错误信息对齐 Python type(node).__name__）。
func nodeKindName(n Node) string {
	switch x := n.(type) {
	case *Constant:
		return "Constant"
	case *Name:
		return "Name"
	case *BinOp:
		return "BinOp"
	case *UnaryOp:
		return "UnaryOp"
	case *BoolOp:
		return "BoolOp"
	case *Compare:
		return "Compare"
	case *IfExp:
		return "IfExp"
	case *Call:
		return "Call"
	case *Attribute:
		return "Attribute"
	case *Subscript:
		return "Subscript"
	case *Forbidden:
		return x.Kind
	default:
		return "unknown"
	}
}

// ────────────────────────────────────────────────────────────────────
// 词法
// ────────────────────────────────────────────────────────────────────

type tokKind int

const (
	tokEOF tokKind = iota
	tokNum
	tokStr
	tokName
	tokKeyword
	tokOp
)

type token struct {
	kind tokKind
	text string // 原文（name/keyword/op）或解码后的字面量文本
	line int
	col  int
}

var keywords = map[string]bool{
	"and": true, "or": true, "not": true, "if": true, "else": true,
	"True": true, "False": true, "None": true, "is": true, "in": true,
	"lambda": true, "for": true, "while": true,
}

// multiOps 运算符（先长后短匹配）。
var multiOps = []string{"**", "//", "<=", ">=", "==", "!=", "<<", ">>"}

type lexer struct {
	src  string
	pos  int
	line int
	col  int
}

func newLexer(src string) *lexer { return &lexer{src: src, line: 1, col: 1} }

func (l *lexer) advance() byte {
	c := l.src[l.pos]
	l.pos++
	if c == '\n' {
		l.line++
		l.col = 1
	} else {
		l.col++
	}
	return c
}

func (l *lexer) next() (token, error) {
	for l.pos < len(l.src) {
		c := l.src[l.pos]
		if c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' {
			l.advance()
			continue
		}
		if c == '#' {
			for l.pos < len(l.src) && l.src[l.pos] != '\n' {
				l.advance()
			}
			continue
		}
		if c == '\\' && l.pos+1 < len(l.src) && l.src[l.pos+1] == '\n' {
			l.advance()
			l.advance()
			continue
		}
		break
	}
	if l.pos >= len(l.src) {
		return token{kind: tokEOF, line: l.line, col: l.col}, nil
	}
	line, col := l.line, l.col
	c := l.src[l.pos]
	switch {
	case c >= '0' && c <= '9':
		return l.lexNumber(line, col)
	case c == '.' && l.pos+1 < len(l.src) && l.src[l.pos+1] >= '0' && l.src[l.pos+1] <= '9':
		return l.lexNumber(line, col)
	case isStringPrefixStart(l.src[l.pos:]):
		return l.lexString(line, col)
	case unicode.IsLetter(rune(c)) || c == '_':
		return l.lexName(line, col)
	case c == '"' || c == '\'':
		return l.lexString(line, col)
	default:
		return l.lexOp(line, col)
	}
}

func (l *lexer) lexNumber(line, col int) (token, error) {
	start := l.pos
	// 进制前缀字面量：0x / 0o / 0b
	if l.src[l.pos] == '0' && l.pos+1 < len(l.src) {
		base, ok := radixPrefix(l.src[l.pos+1])
		if ok {
			l.advance() // 0
			l.advance() // 前缀字母
			digits := 0
			for l.pos < len(l.src) && strings.ContainsRune(radixDigits(base), rune(l.src[l.pos])) {
				l.advance()
				digits++
			}
			if digits == 0 {
				return token{}, l.syntaxErr(line, col, "invalid literal")
			}
			return token{kind: tokNum, text: l.src[start:l.pos], line: line, col: col}, nil
		}
	}
	isFloat := false
	for l.pos < len(l.src) && l.src[l.pos] >= '0' && l.src[l.pos] <= '9' {
		l.advance()
	}
	if l.pos < len(l.src) && l.src[l.pos] == '.' {
		isFloat = true
		l.advance()
		for l.pos < len(l.src) && l.src[l.pos] >= '0' && l.src[l.pos] <= '9' {
			l.advance()
		}
	}
	if l.pos < len(l.src) && (l.src[l.pos] == 'e' || l.src[l.pos] == 'E') {
		l.advance()
		if l.pos < len(l.src) && (l.src[l.pos] == '+' || l.src[l.pos] == '-') {
			l.advance()
		}
		if l.pos < len(l.src) && l.src[l.pos] >= '0' && l.src[l.pos] <= '9' {
			isFloat = true
			for l.pos < len(l.src) && l.src[l.pos] >= '0' && l.src[l.pos] <= '9' {
				l.advance()
			}
		} else {
			return token{}, l.syntaxErr(line, col, "invalid literal %q", l.src[start:l.pos])
		}
	}
	lit := l.src[start:l.pos]
	if !isFloat && l.pos == start {
		return token{}, l.syntaxErr(line, col, "invalid literal")
	}
	return token{kind: tokNum, text: lit, line: line, col: col}, nil
}

func radixPrefix(c byte) (int, bool) {
	switch c {
	case 'x', 'X':
		return 16, true
	case 'o', 'O':
		return 8, true
	case 'b', 'B':
		return 2, true
	default:
		return 10, false
	}
}

// isStringPrefixStart 判断当前位置是否为带前缀字符串（如 r"x"）的起点：
// 首字符为合法前缀字母且紧随引号。
func isStringPrefixStart(rest string) bool {
	if rest[0] != 'r' && rest[0] != 'R' && rest[0] != 'u' && rest[0] != 'U' &&
		rest[0] != 'f' && rest[0] != 'F' && rest[0] != 'b' && rest[0] != 'B' {
		return false
	}
	if len(rest) < 2 {
		return false
	}
	// 连续前缀字母后跟引号（如 rb".."）
	i := 0
	for i < len(rest) {
		ch := rest[i]
		isPrefixChar := ch == 'r' || ch == 'R' || ch == 'u' || ch == 'U' ||
			ch == 'f' || ch == 'F' || ch == 'b' || ch == 'B'
		if !isPrefixChar {
			return false
		}
		if i+1 < len(rest) && (rest[i+1] == '"' || rest[i+1] == '\'') {
			return true
		}
		i++
	}
	return false
}

func radixDigits(base int) string {
	switch base {
	case 16:
		return "0123456789abcdefABCDEF"
	case 8:
		return "01234567"
	default:
		return "01"
	}
}

func (l *lexer) lexName(line, col int) (token, error) {
	start := l.pos
	for l.pos < len(l.src) {
		r := rune(l.src[l.pos])
		if unicode.IsLetter(r) || unicode.IsDigit(r) || l.src[l.pos] == '_' {
			l.advance()
			continue
		}
		break
	}
	text := l.src[start:l.pos]
	if keywords[text] {
		return token{kind: tokKeyword, text: text, line: line, col: col}, nil
	}
	return token{kind: tokName, text: text, line: line, col: col}, nil
}

func (l *lexer) lexString(line, col int) (token, error) {
	// 字符串前缀（r/u 合法；f/b 属禁用面，报结构化错误）
	prefix := ""
	for l.pos < len(l.src) {
		ch := l.src[l.pos]
		isPrefixChar := ch == 'r' || ch == 'R' || ch == 'u' || ch == 'U' ||
			ch == 'f' || ch == 'F' || ch == 'b' || ch == 'B'
		if isPrefixChar && l.pos+1 < len(l.src) && (l.src[l.pos+1] == '"' || l.src[l.pos+1] == '\'') {
			prefix += string(ch)
			l.advance()
			continue
		}
		break
	}
	lower := strings.ToLower(prefix)
	raw := strings.Contains(lower, "r")
	if strings.Contains(lower, "f") {
		return token{}, &UnsafeError{Msg: "禁止的语法节点：JoinedStr（f-string 不在 DSL 子集内）"}
	}
	if strings.Contains(lower, "b") {
		return token{}, &UnsafeError{Msg: "禁止的语法节点：Constant（bytes 字面量不在 DSL 子集内）"}
	}
	quote := l.advance()
	var sb strings.Builder
	closed := false
	for l.pos < len(l.src) {
		ch := l.advance()
		if ch == quote {
			closed = true
			break
		}
		if ch == '\n' {
			return token{}, l.syntaxErr(line, col, "unterminated string literal")
		}
		if ch == '\\' && !raw {
			if l.pos >= len(l.src) {
				break
			}
			esc := l.advance()
			switch esc {
			case 'n':
				sb.WriteByte('\n')
			case 't':
				sb.WriteByte('\t')
			case 'r':
				sb.WriteByte('\r')
			case '\\':
				sb.WriteByte('\\')
			case '\'':
				sb.WriteByte('\'')
			case '"':
				sb.WriteByte('"')
			case '0':
				sb.WriteByte(0)
			default:
				// Python 对未知转义保留反斜杠与字符本身。
				sb.WriteByte('\\')
				sb.WriteByte(esc)
			}
			continue
		}
		sb.WriteByte(ch)
	}
	if !closed {
		return token{}, l.syntaxErr(line, col, "unterminated string literal")
	}
	return token{kind: tokStr, text: sb.String(), line: line, col: col}, nil
}

func (l *lexer) lexOp(line, col int) (token, error) {
	rest := l.src[l.pos:]
	for _, op := range multiOps {
		if strings.HasPrefix(rest, op) {
			for range op {
				l.advance()
			}
			return token{kind: tokOp, text: op, line: line, col: col}, nil
		}
	}
	switch l.src[l.pos] {
	case '+', '-', '*', '/', '%', '(', ')', ',', '<', '>', '|', '^', '&', '@', '~', '[', ']', '{', '}', ':', '.', '=':
		tok := token{kind: tokOp, text: string(l.src[l.pos]), line: line, col: col}
		l.advance()
		return tok, nil
	default:
		return token{}, l.syntaxErr(line, col, "invalid character %q", string(l.src[l.pos]))
	}
}

func (l *lexer) syntaxErr(line, col int, format string, args ...any) error {
	return &SyntaxError{Msg: fmt.Sprintf(format, args...), Line: line, Col: col}
}

// ────────────────────────────────────────────────────────────────────
// 语法分析（递归下降）
// ────────────────────────────────────────────────────────────────────

type parser struct {
	lex   *lexer
	ahead []token // 多槽回退队列（FIFO：先回退的先出）
}

// parseExpr 解析单条表达式（对应 ast.parse(mode="eval")）。
func parseExpr(src string) (ExprNode, error) {
	p := &parser{lex: newLexer(src)}
	root, err := p.parseTernary()
	if err != nil {
		return nil, err
	}
	t, err := p.nextTok()
	if err != nil {
		return nil, err
	}
	if t.kind != tokEOF {
		return nil, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
	}
	return root, nil
}

func (p *parser) nextTok() (token, error) {
	if len(p.ahead) > 0 {
		t := p.ahead[0]
		p.ahead = p.ahead[1:]
		return t, nil
	}
	return p.lex.next()
}

func (p *parser) peekTok() (token, error) {
	t, err := p.nextTok()
	if err != nil {
		return token{}, err
	}
	p.ahead = append([]token{t}, p.ahead...)
	return t, nil
}

// pushback 把已消费的 token 放回队首。
func (p *parser) pushback(t token) {
	p.ahead = append([]token{t}, p.ahead...)
}

// consumeTok 消费刚成功 peek 的 token（必在回退队列中，不触发词法错误）。
func (p *parser) consumeTok() {
	if len(p.ahead) == 0 {
		panic("expr: consumeTok 前必须成功 peekTok（解析器内部不变量）")
	}
	p.ahead = p.ahead[1:]
}

func (p *parser) expectOp(op string) (token, error) {
	t, err := p.nextTok()
	if err != nil {
		return token{}, err
	}
	if t.kind != tokOp || t.text != op {
		return token{}, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
	}
	return t, nil
}

// parseTernary: or_test ('if' or_test 'else' ternary)?
func (p *parser) parseTernary() (ExprNode, error) {
	body, err := p.parseOr()
	if err != nil {
		return nil, err
	}
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokKeyword && t.text == "if" {
		p.consumeTok()
		test, err := p.parseOr()
		if err != nil {
			return nil, err
		}
		if _, err := p.expectKeyword("else"); err != nil {
			return nil, err
		}
		orelse, err := p.parseTernary()
		if err != nil {
			return nil, err
		}
		n := &IfExp{Test: test, Body: body, Orelse: orelse}
		n.line, n.col = body.pos()
		return n, nil
	}
	return body, nil
}

func (p *parser) expectKeyword(kw string) (token, error) {
	t, err := p.nextTok()
	if err != nil {
		return token{}, err
	}
	if t.kind != tokKeyword || t.text != kw {
		return token{}, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
	}
	return t, nil
}

// parseOr: and_test ('or' and_test)*
func (p *parser) parseOr() (ExprNode, error) {
	first, err := p.parseAnd()
	if err != nil {
		return nil, err
	}
	vals := []ExprNode{first}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		if t.kind != tokKeyword || t.text != "or" {
			break
		}
		p.consumeTok()
		next, err := p.parseAnd()
		if err != nil {
			return nil, err
		}
		vals = append(vals, next)
	}
	if len(vals) == 1 {
		return vals[0], nil
	}
	n := &BoolOp{Values: vals, IsAnd: false}
	n.line, n.col = first.pos()
	return n, nil
}

// parseAnd: not_test ('and' not_test)*
func (p *parser) parseAnd() (ExprNode, error) {
	first, err := p.parseNot()
	if err != nil {
		return nil, err
	}
	vals := []ExprNode{first}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		if t.kind != tokKeyword || t.text != "and" {
			break
		}
		p.consumeTok()
		next, err := p.parseNot()
		if err != nil {
			return nil, err
		}
		vals = append(vals, next)
	}
	if len(vals) == 1 {
		return vals[0], nil
	}
	n := &BoolOp{Values: vals, IsAnd: true}
	n.line, n.col = first.pos()
	return n, nil
}

// parseNot: 'not' not_test | comparison
func (p *parser) parseNot() (ExprNode, error) {
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokKeyword && t.text == "not" {
		p.consumeTok()
		x, err := p.parseNot()
		if err != nil {
			return nil, err
		}
		n := &UnaryOp{X: x, OpName: "Not", Allowed: true}
		n.line, n.col = t.line, t.col
		return n, nil
	}
	return p.parseComparison()
}

var cmpOps = map[string]string{
	"<": "Lt", "<=": "LtE", ">": "Gt", ">=": "GtE", "==": "Eq", "!=": "NotEq",
}

var cmpOpsForbidden = map[string]string{
	"in": "In", "is": "Is",
}

// parseComparison: arith (comp_op arith)*  （链式）
func (p *parser) parseComparison() (ExprNode, error) {
	left, err := p.parseArith()
	if err != nil {
		return nil, err
	}
	lline, lcol := left.pos()
	var ops []compareOp
	var comps []ExprNode
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		var op compareOp
		switch {
		case t.kind == tokOp && cmpOps[t.text] != "":
			op = compareOp{name: cmpOps[t.text], allowed: true}
		case t.kind == tokKeyword && cmpOpsForbidden[t.text] != "":
			op = compareOp{name: cmpOpsForbidden[t.text], allowed: false}
		case t.kind == tokKeyword && t.text == "not":
			// not in
			p.consumeTok()
			nx, err := p.nextTok()
			if err != nil {
				return nil, err
			}
			if nx.kind != tokKeyword || nx.text != "in" {
				return nil, &SyntaxError{Msg: "invalid syntax", Line: nx.line, Col: nx.col}
			}
			op = compareOp{name: "NotIn", allowed: false}
		case t.kind == tokKeyword && t.text == "is":
			p.consumeTok()
			nx, err := p.peekTok()
			if err != nil {
				return nil, err
			}
			if nx.kind == tokKeyword && nx.text == "not" {
				p.consumeTok()
				op = compareOp{name: "IsNot", allowed: false}
			} else {
				op = compareOp{name: "Is", allowed: false}
			}
		default:
			if len(ops) == 0 {
				return left, nil
			}
			n := &Compare{Left: left, Ops: ops, Comparators: comps}
			n.line, n.col = lline, lcol
			return n, nil
		}
		p.consumeTok()
		right, err := p.parseArith()
		if err != nil {
			return nil, err
		}
		ops = append(ops, op)
		comps = append(comps, right)
	}
}

var binOps = map[string]string{
	"+": "Add", "-": "Sub", "*": "Mult", "/": "Div", "//": "FloorDiv", "%": "Mod",
}

var binOpsForbidden = map[string]string{
	"|": "BitOr", "^": "BitXor", "&": "BitAnd", "<<": "LShift", ">>": "RShift", "@": "MatMult",
}

// parseArith: term (('+'|'-') term)*
func (p *parser) parseArith() (ExprNode, error) {
	left, err := p.parseTerm()
	if err != nil {
		return nil, err
	}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		if t.kind != tokOp || (t.text != "+" && t.text != "-") {
			return left, nil
		}
		p.consumeTok()
		right, err := p.parseTerm()
		if err != nil {
			return nil, err
		}
		n := &BinOp{X: left, Y: right, OpName: binOps[t.text], Allowed: true}
		n.line, n.col = left.pos()
		left = n
	}
}

// parseTerm: factor (('*'|'/'|'//'|'%'|'|'|...) factor)*
func (p *parser) parseTerm() (ExprNode, error) {
	left, err := p.parseFactor()
	if err != nil {
		return nil, err
	}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		name := ""
		allowed := false
		if t.kind == tokOp {
			// 项级运算符只含乘除模（+/- 在更高层的 parseArith）。
			if t.text == "*" || t.text == "/" || t.text == "//" || t.text == "%" {
				name, allowed = binOps[t.text], true
			} else if n, ok := binOpsForbidden[t.text]; ok {
				name = n
			}
		}
		if name == "" {
			return left, nil
		}
		p.consumeTok()
		right, err := p.parseFactor()
		if err != nil {
			return nil, err
		}
		n := &BinOp{X: left, Y: right, OpName: name, Allowed: allowed}
		n.line, n.col = left.pos()
		left = n
	}
}

// parseFactor: ('+'|'-'|'~') factor | power
func (p *parser) parseFactor() (ExprNode, error) {
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokOp && (t.text == "+" || t.text == "-" || t.text == "~") {
		p.consumeTok()
		x, err := p.parseFactor()
		if err != nil {
			return nil, err
		}
		n := &UnaryOp{X: x}
		switch t.text {
		case "+":
			n.OpName, n.Allowed = "UAdd", true
		case "-":
			n.OpName, n.Allowed = "USub", true
		default:
			n.OpName, n.Allowed = "BitNot", false
		}
		n.line, n.col = t.line, t.col
		return n, nil
	}
	return p.parsePower()
}

// parsePower: primary ['**' factor]（右结合；右侧可带一元前缀）
func (p *parser) parsePower() (ExprNode, error) {
	base, err := p.parsePrimary()
	if err != nil {
		return nil, err
	}
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokOp && t.text == "**" {
		p.consumeTok()
		exp, err := p.parseFactor()
		if err != nil {
			return nil, err
		}
		n := &BinOp{X: base, Y: exp, OpName: "Pow", Allowed: true}
		n.line, n.col = base.pos()
		return n, nil
	}
	return base, nil
}

// parsePrimary: atom trailer*
// trailer: '(' args ')' | '.' NAME | '[' ... ']'
func (p *parser) parsePrimary() (ExprNode, error) {
	x, err := p.parseAtom()
	if err != nil {
		return nil, err
	}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		if t.kind != tokOp {
			return x, nil
		}
		switch t.text {
		case "(":
			call, err := p.parseCall(x)
			if err != nil {
				return nil, err
			}
			x = call
		case ".":
			p.consumeTok()
			nameTok, err := p.nextTok()
			if err != nil {
				return nil, err
			}
			if nameTok.kind != tokName && nameTok.kind != tokKeyword {
				return nil, &SyntaxError{Msg: "invalid syntax", Line: nameTok.line, Col: nameTok.col}
			}
			n := &Attribute{Obj: x, Name: nameTok.text}
			n.line, n.col = x.pos()
			x = n
		case "[":
			sub, err := p.parseSubscript(x)
			if err != nil {
				return nil, err
			}
			x = sub
		default:
			return x, nil
		}
	}
}

// parseCall: '(' [args] ')'，args: expr | '*' expr | NAME '=' expr（kwarg）
func (p *parser) parseCall(fn ExprNode) (ExprNode, error) {
	openTok, err := p.expectOp("(")
	if err != nil {
		return nil, err
	}
	call := &Call{Func: fn}
	call.line, call.col = fn.pos()
	_ = openTok
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokOp && t.text == ")" {
		p.consumeTok()
		return call, nil
	}
	for {
		t, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		switch {
		case t.kind == tokOp && t.text == "*":
			p.consumeTok()
			call.Starred = true
			arg, err := p.parseTernary()
			if err != nil {
				return nil, err
			}
			call.Args = append(call.Args, arg)
		case t.kind == tokName:
			// 关键字参数判定（NAME '='）；借助消费后回推支持任意表达式。
			nameTok, _ := p.nextTok()
			t2, err := p.peekTok()
			if err != nil {
				return nil, err
			}
			if t2.kind == tokOp && t2.text == "=" {
				call.HasKw = true
				p.consumeTok()
				arg, err := p.parseTernary()
				if err != nil {
					return nil, err
				}
				call.Args = append(call.Args, arg)
			} else {
				p.pushback(nameTok)
				arg, err := p.parseTernary()
				if err != nil {
					return nil, err
				}
				call.Args = append(call.Args, arg)
			}
		default:
			arg, err := p.parseTernary()
			if err != nil {
				return nil, err
			}
			call.Args = append(call.Args, arg)
		}
		t3, err := p.peekTok()
		if err != nil {
			return nil, err
		}
		if t3.kind == tokOp && t3.text == "," {
			p.consumeTok()
			// 尾随逗号合法（Python f(1,) 合法）
			t4, err := p.peekTok()
			if err != nil {
				return nil, err
			}
			if t4.kind == tokOp && t4.text == ")" {
				p.consumeTok()
				return call, nil
			}
			continue
		}
		break
	}
	if _, err := p.expectOp(")"); err != nil {
		return nil, err
	}
	return call, nil
}

func (p *parser) parseSubscript(obj ExprNode) (ExprNode, error) {
	if _, err := p.expectOp("["); err != nil {
		return nil, err
	}
	depth := 1
	for depth > 0 {
		t, err := p.nextTok()
		if err != nil {
			return nil, err
		}
		if t.kind == tokEOF {
			return nil, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
		}
		if t.kind == tokOp {
			switch t.text {
			case "[", "(", "{":
				depth++
			case "]", ")", "}":
				depth--
			}
		}
	}
	n := &Subscript{Obj: obj}
	n.line, n.col = obj.pos()
	return n, nil
}

// parseAtom: 字面量 / 名称 / 括号表达式 / 禁止结构。
func (p *parser) parseAtom() (ExprNode, error) {
	t, err := p.nextTok()
	if err != nil {
		return nil, err
	}
	switch {
	case t.kind == tokNum:
		v, err := numberLiteralValue(t.text)
		if err != nil {
			return nil, &SyntaxError{Msg: err.Error(), Line: t.line, Col: t.col}
		}
		n := &Constant{Val: v}
		n.line, n.col = t.line, t.col
		return n, nil
	case t.kind == tokStr:
		n := &Constant{Val: StringValue(t.text)}
		n.line, n.col = t.line, t.col
		return n, nil
	case t.kind == tokKeyword:
		switch t.text {
		case "True", "False", "None":
			n := &Constant{Val: builtinConstant(t.text)}
			n.line, n.col = t.line, t.col
			return n, nil
		case "lambda":
			return p.parseLambda(t)
		default:
			// for/while/in/is 等出现在原子位置：语法错误
			return nil, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
		}
	case t.kind == tokName:
		n := &Name{ID: t.text}
		n.line, n.col = t.line, t.col
		return n, nil
	case t.kind == tokOp:
		switch t.text {
		case "(":
			return p.parseParen(t)
		case "[":
			n := &Forbidden{Kind: "List"}
			n.line, n.col = t.line, t.col
			if err := p.skipBalanced(); err != nil {
				return nil, err
			}
			return n, nil
		case "{":
			n := &Forbidden{Kind: "Dict"}
			n.line, n.col = t.line, t.col
			if err := p.skipBalanced(); err != nil {
				return nil, err
			}
			return n, nil
		}
	}
	return nil, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
}

// parseParen: '(' expr ')' | '(' ')' | '(' expr ',' ... ')' → Tuple（禁止）。
func (p *parser) parseParen(open token) (ExprNode, error) {
	t, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t.kind == tokOp && t.text == ")" {
		p.consumeTok()
		n := &Forbidden{Kind: "Tuple"}
		n.line, n.col = open.line, open.col
		return n, nil
	}
	inner, err := p.parseTernary()
	if err != nil {
		return nil, err
	}
	t2, err := p.peekTok()
	if err != nil {
		return nil, err
	}
	if t2.kind == tokOp && t2.text == "," {
		// 元组字面量：整体作为禁用节点，消费到配对右括号
		n := &Forbidden{Kind: "Tuple"}
		n.line, n.col = open.line, open.col
		depth := 1
		for depth > 0 {
			tk, err := p.nextTok()
			if err != nil {
				return nil, err
			}
			if tk.kind == tokEOF {
				return nil, &SyntaxError{Msg: "invalid syntax", Line: tk.line, Col: tk.col}
			}
			if tk.kind == tokOp {
				switch tk.text {
				case "(", "[":
					depth++
				case ")", "]":
					depth--
				}
			}
		}
		return n, nil
	}
	if _, err := p.expectOp(")"); err != nil {
		return nil, err
	}
	return inner, nil
}

// skipBalanced 消费当前已消费开括号之后到配对闭括号的全部 token。
func (p *parser) skipBalanced() error {
	depth := 1
	for depth > 0 {
		t, err := p.nextTok()
		if err != nil {
			return err
		}
		if t.kind == tokEOF {
			return &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
		}
		if t.kind == tokOp {
			switch t.text {
			case "(", "[", "{":
				depth++
			case ")", "]", "}":
				depth--
			}
		}
	}
	return nil
}

// parseLambda: 'lambda' [params] ':' expr（解析后由校验拒绝）。
func (p *parser) parseLambda(kw token) (ExprNode, error) {
	for {
		t, err := p.nextTok()
		if err != nil {
			return nil, err
		}
		if t.kind == tokEOF {
			return nil, &SyntaxError{Msg: "invalid syntax", Line: t.line, Col: t.col}
		}
		if t.kind == tokOp && t.text == ":" {
			break
		}
	}
	if _, err := p.parseTernary(); err != nil {
		return nil, err
	}
	n := &Forbidden{Kind: "Lambda"}
	n.line, n.col = kw.line, kw.col
	return n, nil
}

// builtinConstant 把 True/False/None 关键字映射为常量值。
func builtinConstant(name string) Value {
	switch name {
	case "True":
		return BoolValue(true)
	case "False":
		return BoolValue(false)
	default:
		return NoneValue{}
	}
}

// numberLiteralValue 把数字字面量文本转为常量值（int/float）。
// int64 边界内的整数为 IntValue；越界整数与非法字面量报错
// （fail-closed：Python 任意精度整数不在 int64 表示域内）。
func numberLiteralValue(text string) (Value, error) {
	if strings.ContainsAny(text, ".eE") {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil {
			return nil, fmt.Errorf("非法浮点字面量 %q", text)
		}
		return FloatValue(f), nil
	}
	if len(text) > 1 && text[0] == '0' {
		if base, ok := radixPrefix(text[1]); ok {
			v, ok2 := new(big.Int).SetString(text[2:], base)
			if !ok2 {
				return nil, fmt.Errorf("非法整数字面量 %q", text)
			}
			return bigIntToInt64(v, text)
		}
	}
	v, ok := new(big.Int).SetString(text, 10)
	if !ok {
		return nil, fmt.Errorf("非法整数字面量 %q", text)
	}
	return bigIntToInt64(v, text)
}

func bigIntToInt64(v *big.Int, text string) (Value, error) {
	if !v.IsInt64() {
		return nil, fmt.Errorf("整数字面量 %q 超出 int64 表示域（fail-closed，Python int 为任意精度）", text)
	}
	return IntValue(v.Int64()), nil
}
