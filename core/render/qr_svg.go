// qr_svg.go 承载 QR 码编码与 SVG 序列化（#152）——ISO/IEC 18004 编码器 +
// 冻结 Python qrcode 库 SvgPathImage 形态的逐字节复刻。
//
// 为什么在零新依赖约束下手写编码器：卷面 QR 是审计回溯链的物理入口
// （卷码 → 扫码 → paper 反查），必须与冻结实现（src/core/render/trace_codes.py
// generate_qr_svg）产出逐字节一致的 SVG，golden 14 例锁定；引入第三方 QR 库
// 违反硬规则 3，且无法保证与 Python qrcode 库的掩码选择/分段策略逐位一致。
//
// 复刻边界（与 Python qrcode 库行为一一对应，全部有 golden 背书）：
//   - 分段策略：optimal_data_chunks(minimum=20)——≥20 连续数字取数字模式、
//     ≥20 连续字母数字取字母数字模式，其余按字节（UTF-8）模式；
//   - 版本选择：best_fit 二分（容量表 = EC 分块数据码字 × 8 bit）；
//   - 纠错：GF(256) Reed-Solomon（0x11D 本原多项式），EC 级 M；
//   - 掩码：8 选 1，惩罚分四则（qrcode lost_point），平分取最小掩码号；
//   - SVG：单条 <path>，逐模块 M{x},{y}H{x+w}V{y+h}H{x}z 子路径，mm 单位
//     （boxSize=10 → 1mm），属性序与数值格式逐字节对齐 golden。
//
// 超容量（版本 > 40）fail-loud：返回 ErrQRSVGTooLarge，不静默降级.
package render

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// ErrQRSVGTooLarge 是 payload 超出 QR 版本 40 容量的哨兵错误（fail-loud，
// 对应冻结实现 qrcode.exceptions.DataOverflowError）.
var ErrQRSVGTooLarge = errors.New("render: QR payload 超出版本 40 容量")

// ────────────────────────────────────────────────────────────────────────
// 模式与最优分段（qrcode.util.optimal_data_chunks minimum=20 同构）
// ────────────────────────────────────────────────────────────────────────

const (
	qrModeNumber = 0b0001
	qrModeAlnum  = 0b0010
	qrModeByte   = 0b0100
)

// qrAlnumCharset 字母数字模式字符集（ISO 18004 表 2，与 qrcode ALPHA_NUM 一致）.
const qrAlnumCharset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"

// qrOptimizeMinimum 最优分段最小连续长度（qrcode add_data 默认 optimize=20）.
const qrOptimizeMinimum = 20

// qrSegment 一个数据分段：模式 + 原始字节（数字/字母数字模式均为 ASCII）.
type qrSegment struct {
	mode int
	data []byte
}

func qrIsDigit(b byte) bool { return b >= '0' && b <= '9' }

func qrIsAlnum(b byte) bool { return strings.IndexByte(qrAlnumCharset, b) >= 0 }

// qrFindRun 找 data 中第一段长度 ≥ min 的连续 pred 字符（re.search 语义：
// 最左起点 + 贪婪延长）。ok=false 表示不存在.
func qrFindRun(data []byte, pred func(byte) bool, min int) (start, end int, ok bool) {
	for i := 0; i < len(data); {
		if !pred(data[i]) {
			i++
			continue
		}
		j := i
		for j < len(data) && pred(data[j]) {
			j++
		}
		if j-i >= min {
			return i, j, true
		}
		i = j
	}
	return 0, 0, false
}

// optimalDataChunks 最优分段（qrcode.util.optimal_data_chunks minimum=20 同构）：
// 先切 ≥20 连续数字段，剩余段内再切 ≥20 连续字母数字段，其余为字节段。
// data 长度 ≤ 20 时整串判定（re ^...$ 锚定语义）.
func optimalDataChunks(data []byte) []qrSegment {
	if len(data) == 0 {
		return nil
	}
	if len(data) <= qrOptimizeMinimum {
		if qrAllPred(data, qrIsDigit) {
			return []qrSegment{{qrModeNumber, data}}
		}
		if qrAllPred(data, qrIsAlnum) {
			return []qrSegment{{qrModeAlnum, data}}
		}
		return []qrSegment{{qrModeByte, data}}
	}
	// 第一层：数字段（Python re.findall 式从左到右、匹配后跳到段尾）
	var raw []qrSegment
	rest := data
	for len(rest) > 0 {
		st, en, ok := qrFindRun(rest, qrIsDigit, qrOptimizeMinimum)
		if !ok {
			raw = append(raw, qrSegment{0, rest})
			rest = nil
			break
		}
		if st > 0 {
			raw = append(raw, qrSegment{0, rest[:st]})
		}
		raw = append(raw, qrSegment{qrModeNumber, rest[st:en]})
		rest = rest[en:]
	}
	// 第二层：非数字段内切字母数字段，余量为字节段
	out := make([]qrSegment, 0, len(raw))
	for _, ch := range raw {
		if ch.mode == qrModeNumber {
			out = append(out, ch)
			continue
		}
		r := ch.data
		for len(r) > 0 {
			st, en, ok := qrFindRun(r, qrIsAlnum, qrOptimizeMinimum)
			if !ok {
				out = append(out, qrSegment{qrModeByte, r})
				break
			}
			if st > 0 {
				out = append(out, qrSegment{qrModeByte, r[:st]})
			}
			out = append(out, qrSegment{qrModeAlnum, r[st:en]})
			r = r[en:]
		}
	}
	return out
}

func qrAllPred(data []byte, pred func(byte) bool) bool {
	for _, b := range data {
		if !pred(b) {
			return false
		}
	}
	return len(data) > 0
}

// ────────────────────────────────────────────────────────────────────────
// 比特缓冲（qrcode.util.BitBuffer 同构）
// ────────────────────────────────────────────────────────────────────────

type qrBitBuffer struct {
	buf  []byte
	bits int
}

func (b *qrBitBuffer) put(num, length int) {
	for i := length - 1; i >= 0; i-- {
		b.putBit((num>>uint(i))&1 == 1)
	}
}

func (b *qrBitBuffer) putBit(bit bool) {
	idx := b.bits / 8
	for len(b.buf) <= idx {
		b.buf = append(b.buf, 0)
	}
	if bit {
		b.buf[idx] |= 0x80 >> uint(b.bits%8)
	}
	b.bits++
}

func (b *qrBitBuffer) bitLen() int { return b.bits }

// qrSegmentWrite 写入分段：模式指示 4 bit + 计数字段（随版本变长）+ 数据.
func qrSegmentWrite(buf *qrBitBuffer, seg qrSegment, version int) {
	buf.put(seg.mode, 4)
	buf.put(len(seg.data), qrLengthBits(seg.mode, version))
	switch seg.mode {
	case qrModeNumber:
		for i := 0; i < len(seg.data); i += 3 {
			n := len(seg.data) - i
			if n > 3 {
				n = 3
			}
			v := 0
			for k := 0; k < n; k++ {
				v = v*10 + int(seg.data[i+k]-'0')
			}
			bits := 10
			switch n {
			case 2:
				bits = 7
			case 1:
				bits = 4
			}
			buf.put(v, bits)
		}
	case qrModeAlnum:
		for i := 0; i < len(seg.data); i += 2 {
			if i+1 < len(seg.data) {
				v := strings.IndexByte(qrAlnumCharset, seg.data[i])*45 +
					strings.IndexByte(qrAlnumCharset, seg.data[i+1])
				buf.put(v, 11)
			} else {
				buf.put(strings.IndexByte(qrAlnumCharset, seg.data[i]), 6)
			}
		}
	default: // qrModeByte：UTF-8 字节逐个 8 bit
		for _, c := range seg.data {
			buf.put(int(c), 8)
		}
	}
}

// qrLengthBits 计数字段长度（小/中/大版本组，ISO 18004 表 3；qrcode
// mode_sizes_for_version 同构）.
func qrLengthBits(mode, version int) int {
	var small, medium, large int
	switch mode {
	case qrModeNumber:
		small, medium, large = 10, 12, 14
	case qrModeAlnum:
		small, medium, large = 9, 11, 13
	default:
		small, medium, large = 8, 16, 16
	}
	switch {
	case version < 10:
		return small
	case version < 27:
		return medium
	default:
		return large
	}
}

// qrSizeGroup 计数字段长度组（0 小 / 1 中 / 2 大；跨组则 best_fit 重算）.
func qrSizeGroup(version int) int {
	switch {
	case version < 10:
		return 0
	case version < 27:
		return 1
	default:
		return 2
	}
}

// qrSegmentBits 计算分段数据本身的比特数（best_fit 用，不含头）.
func qrSegmentBits(seg qrSegment) int {
	switch seg.mode {
	case qrModeNumber:
		n := len(seg.data) / 3 * 10
		switch len(seg.data) % 3 {
		case 1:
			n += 4
		case 2:
			n += 7
		}
		return n
	case qrModeAlnum:
		n := len(seg.data) / 2 * 11
		if len(seg.data)%2 == 1 {
			n += 6
		}
		return n
	default:
		return 8 * len(seg.data)
	}
}

// ────────────────────────────────────────────────────────────────────────
// 版本选择（qrcode QRCode.best_fit 同构）
// ────────────────────────────────────────────────────────────────────────

// qrECLevelM 纠错级 M（qrcode.constants.ERROR_CORRECT_M；格式信息位 00）.
const qrECLevelM = 0

// qrRSBlockM 各版本 EC 级 M 的分块（count, total, data）三元组序列——
// 与 qrcode.base.RS_BLOCK_TABLE M 列逐项一致.
var qrRSBlockM = [40][][3]int{
	{{1, 26, 16}},
	{{1, 44, 28}},
	{{1, 70, 44}},
	{{2, 50, 32}},
	{{2, 67, 43}},
	{{4, 43, 27}},
	{{4, 49, 31}},
	{{2, 60, 38}, {2, 61, 39}},
	{{3, 58, 36}, {2, 59, 37}},
	{{4, 69, 43}, {1, 70, 44}},
	{{1, 80, 50}, {4, 81, 51}},
	{{6, 58, 36}, {2, 59, 37}},
	{{8, 59, 37}, {1, 60, 38}},
	{{4, 64, 40}, {5, 65, 41}},
	{{5, 65, 41}, {5, 66, 42}},
	{{7, 73, 45}, {3, 74, 46}},
	{{10, 74, 46}, {1, 75, 47}},
	{{9, 69, 43}, {4, 70, 44}},
	{{3, 70, 44}, {11, 71, 45}},
	{{3, 67, 41}, {13, 68, 42}},
	{{17, 68, 42}},
	{{17, 74, 46}},
	{{4, 75, 47}, {14, 76, 48}},
	{{6, 73, 45}, {14, 74, 46}},
	{{8, 75, 47}, {13, 76, 48}},
	{{19, 74, 46}, {4, 75, 47}},
	{{22, 73, 45}, {3, 74, 46}},
	{{3, 73, 45}, {23, 74, 46}},
	{{21, 73, 45}, {7, 74, 46}},
	{{19, 75, 47}, {10, 76, 48}},
	{{2, 74, 46}, {29, 75, 47}},
	{{10, 74, 46}, {23, 75, 47}},
	{{14, 74, 46}, {21, 75, 47}},
	{{14, 74, 46}, {23, 75, 47}},
	{{12, 75, 47}, {26, 76, 48}},
	{{6, 75, 47}, {34, 76, 48}},
	{{29, 74, 46}, {3, 75, 47}},
	{{13, 74, 46}, {32, 75, 47}},
	{{40, 75, 47}, {7, 76, 48}},
	{{18, 75, 47}, {31, 76, 48}},
}

// qrDataCodewords 版本 v（1 起）EC M 的数据码字总数.
func qrDataCodewords(version int) int {
	total := 0
	for _, blk := range qrRSBlockM[version-1] {
		total += blk[0] * blk[2]
	}
	return total
}

// qrBestFit 最小可用版本（qrcode QRCode.best_fit 同构：按 start 版本的
// 计数字段长度乐观估算所需比特，再对容量表 bisect；跨长度组则从新版本
// 递归重算）。返回 0 表示超容量（版本 > 40）.
func qrBestFit(segments []qrSegment, start int) int {
	if start < 1 {
		start = 1
	}
	need := 4 * len(segments)
	for _, s := range segments {
		need += qrLengthBits(s.mode, start) + qrSegmentBits(s)
	}
	version := start
	for ; version <= 40; version++ {
		if 8*qrDataCodewords(version) >= need {
			break
		}
	}
	if version > 40 {
		return 0
	}
	if qrSizeGroup(version) != qrSizeGroup(start) {
		return qrBestFit(segments, version)
	}
	return version
}

// ────────────────────────────────────────────────────────────────────────
// GF(256) 与 Reed-Solomon（qrcode.base / qrcode.util.create_bytes 同构）
// ────────────────────────────────────────────────────────────────────────

var (
	qrExpTable [256]int
	qrLogTable [256]int
)

func init() {
	for i := 0; i < 8; i++ {
		qrExpTable[i] = 1 << uint(i)
	}
	for i := 8; i < 256; i++ {
		qrExpTable[i] = qrExpTable[i-4] ^ qrExpTable[i-5] ^ qrExpTable[i-6] ^ qrExpTable[i-8]
	}
	for i := 0; i < 255; i++ {
		qrLogTable[qrExpTable[i]] = i
	}
}

func qrGExp(n int) int { return qrExpTable[n%255] }

func qrGLog(n int) int { return qrLogTable[n] }

// qrPoly 系数多项式（index 0 = 最高次；构造时剥离前导零并右移 shift 位）.
type qrPoly struct {
	num []int
}

func newQRPoly(num []int, shift int) qrPoly {
	off := 0
	for off < len(num) && num[off] == 0 {
		off++
	}
	out := make([]int, len(num)-off+shift)
	copy(out, num[off:])
	return qrPoly{num: out}
}

func (p qrPoly) mul(o qrPoly) qrPoly {
	num := make([]int, len(p.num)+len(o.num)-1)
	for i, a := range p.num {
		for j, b := range o.num {
			if a != 0 && b != 0 {
				num[i+j] ^= qrGExp(qrGLog(a) + qrGLog(b))
			}
		}
	}
	return newQRPoly(num, 0)
}

func (p qrPoly) mod(o qrPoly) qrPoly {
	difference := len(p.num) - len(o.num)
	if difference < 0 {
		return p
	}
	ratio := qrGLog(p.num[0]) - qrGLog(o.num[0])
	num := make([]int, len(o.num))
	for i, otherItem := range o.num {
		num[i] = p.num[i] ^ qrGExp(qrGLog(otherItem)+ratio)
	}
	if difference > 0 {
		num = append(num, p.num[len(o.num):]...)
	}
	return newQRPoly(num, 0).mod(o)
}

// qrCreateData 数据码字序列：分段 → 终止符 → 字节对齐 → EC/11 交替填充 →
// RS 纠错码字（qrcode.util.create_data 同构）.
func qrCreateData(version int, segments []qrSegment) []int {
	buffer := &qrBitBuffer{}
	for _, s := range segments {
		qrSegmentWrite(buffer, s, version)
	}
	blocks := qrRSBlockM[version-1]
	bitLimit := 8 * qrDataCodewords(version)

	// 终止符：至多 4 个 0
	for i := 0; i < 4 && buffer.bitLen() < bitLimit; i++ {
		buffer.putBit(false)
	}
	// 字节对齐
	if rem := buffer.bitLen() % 8; rem != 0 {
		for i := 0; i < 8-rem; i++ {
			buffer.putBit(false)
		}
	}
	// EC/11 交替填充至满
	for i := 0; buffer.bitLen() < bitLimit; i++ {
		if i%2 == 0 {
			buffer.put(0xEC, 8)
		} else {
			buffer.put(0x11, 8)
		}
	}
	return qrCreateBytes(buffer, blocks)
}

// qrCreateBytes 分块 RS 纠错 + 码字交错（qrcode.util.create_bytes 同构；
// 三元组首项为同构分块个数，需展开）.
func qrCreateBytes(buffer *qrBitBuffer, blocks [][3]int) []int {
	offset := 0
	var dcData, ecData [][]int
	maxDc, maxEc := 0, 0
	for _, blk := range blocks {
		for k := 0; k < blk[0]; k++ {
			dcCount, ecCount := blk[2], blk[1]-blk[2]
			if dcCount > maxDc {
				maxDc = dcCount
			}
			if ecCount > maxEc {
				maxEc = ecCount
			}
			currentDc := make([]int, dcCount)
			for i := 0; i < dcCount; i++ {
				currentDc[i] = int(buffer.buf[i+offset]) & 0xFF
			}
			offset += dcCount

			rsPoly := qrPoly{num: []int{1}}
			for i := 0; i < ecCount; i++ {
				rsPoly = rsPoly.mul(newQRPoly([]int{1, qrGExp(i)}, 0))
			}
			rawPoly := newQRPoly(currentDc, len(rsPoly.num)-1)
			modPoly := rawPoly.mod(rsPoly)
			currentEc := make([]int, ecCount)
			modOffset := len(modPoly.num) - ecCount
			for i := 0; i < ecCount; i++ {
				if idx := i + modOffset; idx >= 0 {
					currentEc[i] = modPoly.num[idx]
				}
			}
			dcData = append(dcData, currentDc)
			ecData = append(ecData, currentEc)
		}
	}
	data := make([]int, 0, offset+maxEc*len(blocks))
	for i := 0; i < maxDc; i++ {
		for _, dc := range dcData {
			if i < len(dc) {
				data = append(data, dc[i])
			}
		}
	}
	for i := 0; i < maxEc; i++ {
		for _, ec := range ecData {
			if i < len(ec) {
				data = append(data, ec[i])
			}
		}
	}
	return data
}

// ────────────────────────────────────────────────────────────────────────
// 矩阵布局（qrcode.main setup_* / map_data 同构）
// ────────────────────────────────────────────────────────────────────────

// qrModules 单元格状态：0 未定（数据位候选），1 暗，2 亮.
type qrModules [][]uint8

func qrNewModules(n int) qrModules {
	m := make(qrModules, n)
	for i := range m {
		m[i] = make([]uint8, n)
	}
	return m
}

func (m qrModules) set(r, c int, dark bool) {
	if dark {
		m[r][c] = 1
	} else {
		m[r][c] = 2
	}
}

// qrPatternPositionTable 校正图形中心坐标（qrcode.util.PATTERN_POSITION_TABLE）.
var qrPatternPositionTable = [40][]int{
	{},
	{6, 18},
	{6, 22},
	{6, 26},
	{6, 30},
	{6, 34},
	{6, 22, 38},
	{6, 24, 42},
	{6, 26, 46},
	{6, 28, 50},
	{6, 30, 54},
	{6, 32, 58},
	{6, 34, 62},
	{6, 26, 46, 66},
	{6, 26, 48, 70},
	{6, 26, 50, 74},
	{6, 30, 54, 78},
	{6, 30, 56, 82},
	{6, 30, 58, 86},
	{6, 34, 62, 90},
	{6, 28, 50, 72, 94},
	{6, 26, 50, 74, 98},
	{6, 30, 54, 78, 102},
	{6, 28, 54, 80, 106},
	{6, 32, 58, 84, 110},
	{6, 30, 58, 86, 114},
	{6, 34, 62, 90, 118},
	{6, 26, 50, 74, 98, 122},
	{6, 30, 54, 78, 102, 126},
	{6, 26, 52, 78, 104, 130},
	{6, 30, 56, 82, 108, 134},
	{6, 34, 60, 86, 112, 138},
	{6, 30, 58, 86, 114, 142},
	{6, 34, 62, 90, 118, 146},
	{6, 30, 54, 78, 102, 126, 150},
	{6, 24, 50, 76, 102, 128, 154},
	{6, 28, 54, 80, 106, 132, 158},
	{6, 32, 58, 84, 110, 136, 162},
	{6, 26, 54, 82, 110, 138, 166},
	{6, 30, 58, 86, 114, 142, 170},
}

// qrBlankLayout 固定图案：位置探测图形 + 分隔带 + 校正图形 + 时序图形.
func qrBlankLayout(version int) qrModules {
	n := version*4 + 17
	m := qrNewModules(n)

	setupProbe := func(row, col int) {
		for r := -1; r <= 7; r++ {
			if row+r <= -1 || n <= row+r {
				continue
			}
			for c := -1; c <= 7; c++ {
				if col+c <= -1 || n <= col+c {
					continue
				}
				dark := (0 <= r && r <= 6 && (c == 0 || c == 6)) ||
					(0 <= c && c <= 6 && (r == 0 || r == 6)) ||
					(2 <= r && r <= 4 && 2 <= c && c <= 4)
				m.set(row+r, col+c, dark)
			}
		}
	}
	setupProbe(0, 0)
	setupProbe(n-7, 0)
	setupProbe(0, n-7)

	// 校正图形：中心已定则整块跳过（与 Python 一致）
	for _, row := range qrPatternPositionTable[version-1] {
		for _, col := range qrPatternPositionTable[version-1] {
			if m[row][col] != 0 {
				continue
			}
			for r := -2; r <= 2; r++ {
				for c := -2; c <= 2; c++ {
					dark := r == -2 || r == 2 || c == -2 || c == 2 || (r == 0 && c == 0)
					m.set(row+r, col+c, dark)
				}
			}
		}
	}

	// 时序图形
	for r := 8; r < n-8; r++ {
		if m[r][6] == 0 {
			m.set(r, 6, r%2 == 0)
		}
	}
	for c := 8; c < n-8; c++ {
		if m[6][c] == 0 {
			m.set(6, c, c%2 == 0)
		}
	}
	return m
}

// qrBCHTypeInfo 格式信息 15 bit（G15 掩码异或；qrcode.util.BCH_type_info）.
func qrBCHTypeInfo(data int) int {
	const g15 = 0b000010100110111
	const g15Mask = 0b101010000010010
	d := data << 10
	for qrBCHDigit(d)-qrBCHDigit(g15) >= 0 {
		d ^= g15 << uint(qrBCHDigit(d)-qrBCHDigit(g15))
	}
	return ((data << 10) | d) ^ g15Mask
}

// qrBCHTypeNumber 版本信息 18 bit（qrcode.util.BCH_type_number）.
func qrBCHTypeNumber(data int) int {
	const g18 = 0b1111100100101
	d := data << 12
	for qrBCHDigit(d)-qrBCHDigit(g18) >= 0 {
		d ^= g18 << uint(qrBCHDigit(d)-qrBCHDigit(g18))
	}
	return (data << 12) | d
}

func qrBCHDigit(data int) int {
	digit := 0
	for data != 0 {
		digit++
		data >>= 1
	}
	return digit
}

// qrSetupTypeInfo 格式信息（左下/右上两份冗余）+ 固定暗模块.
func qrSetupTypeInfo(m qrModules, test bool, maskPattern int) {
	n := len(m)
	data := (qrECLevelM << 3) | maskPattern
	bits := qrBCHTypeInfo(data)
	light := func(i int) bool { return !test && (bits>>uint(i))&1 == 1 }

	for i := 0; i < 15; i++ {
		mod := light(i)
		switch {
		case i < 6:
			m.set(i, 8, mod)
		case i < 8:
			m.set(i+1, 8, mod)
		default:
			m.set(n-15+i, 8, mod)
		}
	}
	for i := 0; i < 15; i++ {
		mod := light(i)
		switch {
		case i < 8:
			m.set(8, n-i-1, mod)
		case i < 9:
			m.set(8, 15-i, mod)
		default:
			m.set(8, 15-i-1, mod)
		}
	}
	// 固定暗模块（恒暗，与掩码/格式无关）
	m.set(n-8, 8, !test)
}

// qrSetupTypeNumber 版本信息（v ≥ 7；两份冗余）.
func qrSetupTypeNumber(m qrModules, test bool, version int) {
	n := len(m)
	bits := qrBCHTypeNumber(version)
	for i := 0; i < 18; i++ {
		mod := !test && (bits>>uint(i))&1 == 1
		m.set(i/3, i%3+n-8-3, mod)
	}
	for i := 0; i < 18; i++ {
		mod := !test && (bits>>uint(i))&1 == 1
		m.set(i%3+n-8-3, i/3, mod)
	}
}

// qrMaskFunc 八种掩码（qrcode.util.mask_func 同构）.
func qrMaskFunc(pattern int) func(i, j int) bool {
	switch pattern {
	case 0:
		return func(i, j int) bool { return (i+j)%2 == 0 }
	case 1:
		return func(i, j int) bool { return i%2 == 0 }
	case 2:
		return func(i, j int) bool { return j%3 == 0 }
	case 3:
		return func(i, j int) bool { return (i+j)%3 == 0 }
	case 4:
		return func(i, j int) bool { return (i/2+j/3)%2 == 0 }
	case 5:
		return func(i, j int) bool { return (i*j)%2+(i*j)%3 == 0 }
	case 6:
		return func(i, j int) bool { return ((i*j)%2+(i*j)%3)%2 == 0 }
	default:
		return func(i, j int) bool { return ((i*j)%3+(i+j)%2)%2 == 0 }
	}
}

// qrMapData 数据位之字形布入 + 掩码（qrcode.main.map_data 同构：右下起、
// 双列蛇形、col ≤ 6 时跳过时序列）.
func qrMapData(m qrModules, data []int, maskPattern int) {
	n := len(m)
	maskFunc := qrMaskFunc(maskPattern)
	inc := -1
	row := n - 1
	bitIndex := 7
	byteIndex := 0
	for col := n - 1; col > 0; col -= 2 {
		c := col
		if c <= 6 {
			c--
		}
		for {
			for _, cc := range [2]int{c, c - 1} {
				if m[row][cc] == 0 {
					dark := false
					if byteIndex < len(data) {
						dark = (data[byteIndex]>>uint(bitIndex))&1 == 1
					}
					if maskFunc(row, cc) {
						dark = !dark
					}
					m.set(row, cc, dark)
					bitIndex--
					if bitIndex == -1 {
						byteIndex++
						bitIndex = 7
					}
				}
			}
			row += inc
			if row < 0 || n <= row {
				row -= inc
				inc = -inc
				break
			}
		}
	}
}

// qrMakeImpl 完整矩阵：固定图案 + 格式/版本信息 + 数据（makeImpl 同构）.
func qrMakeImpl(version int, test bool, maskPattern int, data []int) qrModules {
	m := qrBlankLayout(version)
	qrSetupTypeInfo(m, test, maskPattern)
	if version >= 7 {
		qrSetupTypeNumber(m, test, version)
	}
	qrMapData(m, data, maskPattern)
	return m
}

// ────────────────────────────────────────────────────────────────────────
// 掩码惩罚分（qrcode.util.lost_point 四则同构）
// ────────────────────────────────────────────────────────────────────────

func qrLostPoint(m qrModules) int {
	n := len(m)
	lost := qrLostPointLevel1(m, n)
	lost += qrLostPointLevel2(m, n)
	lost += qrLostPointLevel3(m, n)
	lost += qrLostPointLevel4(m, n)
	return lost
}

func qrLostPointLevel1(m qrModules, n int) int {
	container := make([]int, n+1)
	dark := func(v uint8) bool { return v == 1 }
	// 行向连续段
	for row := 0; row < n; row++ {
		previousColor := dark(m[row][0])
		length := 0
		for col := 0; col < n; col++ {
			if dark(m[row][col]) == previousColor {
				length++
			} else {
				if length >= 5 {
					container[length]++
				}
				length = 1
				previousColor = dark(m[row][col])
			}
		}
		if length >= 5 {
			container[length]++
		}
	}
	// 列向连续段
	for col := 0; col < n; col++ {
		previousColor := dark(m[0][col])
		length := 0
		for row := 0; row < n; row++ {
			if dark(m[row][col]) == previousColor {
				length++
			} else {
				if length >= 5 {
					container[length]++
				}
				length = 1
				previousColor = dark(m[row][col])
			}
		}
		if length >= 5 {
			container[length]++
		}
	}
	lost := 0
	for length := 5; length <= n; length++ {
		lost += container[length] * (length - 2)
	}
	return lost
}

func qrLostPointLevel2(m qrModules, n int) int {
	lost := 0
	for row := 0; row < n-1; row++ {
		thisRow, nextRow := m[row], m[row+1]
		for col := 0; col < n-1; col++ {
			topRight := thisRow[col+1]
			switch {
			case topRight != nextRow[col+1]:
				// 右列不成块 → 下一列必然也不成块，跳过（Horspool 式省略）
				col++
			case topRight != thisRow[col]:
			case topRight != nextRow[col]:
			default:
				lost += 3
			}
		}
	}
	return lost
}

func qrLostPointLevel3(m qrModules, n int) int {
	lost := 0
	dark := func(v uint8) bool { return v == 1 }
	// 1:1:3:1:1 特征比（含 4 亮前/后置）：
	// pattern1 10111010000 / pattern2 00001011101
	pattern := func(get func(int) bool, base int) bool {
		b := func(k int) bool { return get(base + k) }
		p1 := b(0) && !b(1) && b(2) && b(3) && b(4) && !b(5) && b(6) && !b(7) && !b(8) && !b(9) && !b(10)
		p2 := !b(0) && !b(1) && !b(2) && !b(3) && b(4) && !b(5) && b(6) && b(7) && b(8) && !b(9) && b(10)
		return p1 || p2
	}
	for row := 0; row < n; row++ {
		for col := 0; col+10 < n; col++ {
			if pattern(func(k int) bool { return dark(m[row][k]) }, col) {
				lost += 40
			}
		}
	}
	for col := 0; col < n; col++ {
		for row := 0; row+10 < n; row++ {
			if pattern(func(k int) bool { return dark(m[k][col]) }, row) {
				lost += 40
			}
		}
	}
	return lost
}

func qrLostPointLevel4(m qrModules, n int) int {
	darkCount := 0
	for _, rowVals := range m {
		for _, v := range rowVals {
			if v == 1 {
				darkCount++
			}
		}
	}
	percent := float64(darkCount) / float64(n*n)
	rating := int(absFloat(percent*100-50) / 5)
	return rating * 10
}

func absFloat(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

// qrBestMaskPattern 8 选 1（qrcode QRCode.best_mask_pattern 同构：
// 严格更小才替换 → 平分取最小掩码号）.
func qrBestMaskPattern(version int, data []int) int {
	minLostPoint := 0
	pattern := 0
	for i := 0; i < 8; i++ {
		m := qrMakeImpl(version, true, i, data)
		lostPoint := qrLostPoint(m)
		if i == 0 || minLostPoint > lostPoint {
			minLostPoint = lostPoint
			pattern = i
		}
	}
	return pattern
}

// ────────────────────────────────────────────────────────────────────────
// SVG 序列化（qrcode SvgPathImage 形态逐字节复刻）
// ────────────────────────────────────────────────────────────────────────

// qrCoord 像素 → mm 坐标字符串（boxSize=10 → 1mm；10 等分后去尾零，
// 与 Python Decimal(pixels)/10 的字符串形态一致：无多余的 ".0"）.
func qrCoord(pixels int) string {
	if pixels%10 == 0 {
		return strconv.Itoa(pixels / 10)
	}
	return strconv.Itoa(pixels/10) + "." + strconv.Itoa(pixels%10)
}

// GenerateQRSVG 生成 QR 码 SVG 字符串（#152 实现，签名不变）。
//
// 参数语义与冻结实现 generate_qr_svg 一致：boxSize 每模块尺寸（10 → 1mm，
// 冻结默认 4 → 0.4mm），border 静区模块数（冻结默认 1）。
//
//   - 纠错级恒为 M（冻结实现 error_correction=ERROR_CORRECT_M）；
//   - 版本自动选最小可用（1-40），超容量返回 ErrQRSVGTooLarge 包装错误；
//   - 掩码按惩罚分自动选择（与 Python qrcode 逐位一致，golden 锁定）；
//   - 输出为单 <path> 矢量 SVG，可直接嵌入 HTML/PDF。
//
// boxSize < 1 或 border < 0 返回 ErrInvalidCode 包装错误.
func GenerateQRSVG(payload string, boxSize, border int) (string, error) {
	if boxSize < 1 {
		return "", fmt.Errorf("%w: boxSize 必须 ≥ 1，得到 %d", ErrInvalidCode, boxSize)
	}
	if border < 0 {
		return "", fmt.Errorf("%w: border 不能为负，得到 %d", ErrInvalidCode, border)
	}
	segments := optimalDataChunks([]byte(payload))
	version := qrBestFit(segments, 1)
	if version == 0 {
		return "", fmt.Errorf("%w: %d 字节（UTF-8）", ErrQRSVGTooLarge, len(payload))
	}
	data := qrCreateData(version, segments)
	maskPattern := qrBestMaskPattern(version, data)
	modules := qrMakeImpl(version, false, maskPattern, data)

	n := len(modules)
	pixelSize := (n + 2*border) * boxSize
	dim := qrCoord(pixelSize)

	var b strings.Builder
	b.WriteString(`<svg width="`)
	b.WriteString(dim)
	b.WriteString(`mm" height="`)
	b.WriteString(dim)
	b.WriteString(`mm" version="1.1" viewBox="0 0 `)
	b.WriteString(dim)
	b.WriteString(` `)
	b.WriteString(dim)
	b.WriteString(`" xmlns="http://www.w3.org/2000/svg"><path d="`)
	for r := 0; r < n; r++ {
		y := (r + border) * boxSize
		for c := 0; c < n; c++ {
			if modules[r][c] != 1 {
				continue
			}
			x := (c + border) * boxSize
			b.WriteString("M")
			b.WriteString(qrCoord(x))
			b.WriteString(",")
			b.WriteString(qrCoord(y))
			b.WriteString("H")
			b.WriteString(qrCoord(x + boxSize))
			b.WriteString("V")
			b.WriteString(qrCoord(y + boxSize))
			b.WriteString("H")
			b.WriteString(qrCoord(x))
			b.WriteString("z")
		}
	}
	b.WriteString(`" id="qr-path" fill="#000000" fill-opacity="1" fill-rule="nonzero" stroke="none"/></svg>`)
	return b.String(), nil
}
