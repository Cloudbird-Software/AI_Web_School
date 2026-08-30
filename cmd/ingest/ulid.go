// ulid.go —— ULID 的零依赖最小实现（cmd/ingest 专用）。
//
// 为什么不用第三方库：硬规则「零新依赖（标准库 + go.mod 既有）」，而 go.mod
// 无任何 Go 侧 ULID 实现（ulid-py 是 Python 侧依赖，随 src/ 冻结退役）。
// 账行 id 的冻结惯例是「语义前缀 + ULID」：
//   - cert_id  = "cert_" + ULID（src/core/gate/certifier/service.py issue_certificate）
//   - run_id   = "run_"  + ULID（src/core/gate/orchestrator/orchestrator.py）
//   - publication_id = "pub_" + ULID（src/core/content/publication.py）
//
// 本文件按 ULID 规范（48 位毫秒时间戳 + 80 位 crypto/rand 随机数，
// Crockford Base32 26 字符）最小复刻该形态，保证 cmd/ingest 产出的账行 id
// 与冻结实现同形同位阶。
package main

import (
	"crypto/rand"
	"fmt"
	"io"
	"strings"
	"time"
)

// crockford 是 ULID 规范的 Crockford Base32 字母表（不含 I/L/O/U）。
const crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

// ulidTimeMax 是 48 位毫秒时间戳的上界（2^48 - 1，公元 10889 年）。
const ulidTimeMax = 1<<48 - 1

// newULID 生成当前时刻的 ULID（26 字符大写 Crockford Base32）。
func newULID() (string, error) { return newULIDAt(time.Now(), rand.Reader) }

// newULIDAt 以指定时刻与随机源生成 ULID（测试注入固定时钟/随机源用）。
func newULIDAt(now time.Time, r io.Reader) (string, error) {
	ms := now.UnixMilli()
	switch {
	case ms < 0:
		ms = 0 // 负时钟（劣质环境）钳到 0：账行 id 只需唯一，不需时间语义
	case ms > ulidTimeMax:
		return "", fmt.Errorf("ulid: 毫秒时间戳 %d 超出 48 位上界", ms)
	}

	var b [16]byte // ULID 规范位布局：前 6 字节时间戳（大端）+ 后 10 字节随机
	b[0], b[1], b[2] = byte(ms>>40), byte(ms>>32), byte(ms>>24)
	b[3], b[4], b[5] = byte(ms>>16), byte(ms>>8), byte(ms)
	if _, err := io.ReadFull(r, b[6:]); err != nil {
		return "", fmt.Errorf("ulid: 随机数读取失败: %w", err)
	}
	return encodeULID(b), nil
}

// encodeULID 把 16 字节 ULID 编码为 26 字符 Crockford Base32。
// 位流 = 2 位规范补零 + 48 位时间戳 + 80 位随机数（共 130 位 = 26×5）：
// 时间字段占前 10 字符（首字符只含时间最高 3 位——48 位装进 50 位字段，
// 前导 2 位恒零，规范最大值向量「7ZZZ…Z」由此而来），无末尾补位。
func encodeULID(b [16]byte) string {
	var sb strings.Builder
	sb.Grow(26)
	// 字节 0 高 3 位 + 低 5 位恰为前两个字符（补零位在最高处）。
	sb.WriteByte(crockford[(b[0]>>5)&0x07])
	sb.WriteByte(crockford[b[0]&0x1F])
	var acc uint32
	var nbits uint
	// 其余 120 位（b1..b15）→ 恰好 24 字符。
	for _, by := range b[1:] {
		acc = acc<<8 | uint32(by)
		nbits += 8
		for nbits >= 5 {
			nbits -= 5
			sb.WriteByte(crockford[(acc>>nbits)&0x1F])
		}
	}
	return sb.String()
}
