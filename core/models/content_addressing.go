// content_addressing.go 承载内容寻址纯函数（Python 冻结基准
// src/core/models/content_addressing.py 的 Go 移植；契约 §3 公式一/二/三）。
//
// 宪法 D3：内容寻址确定性——同一输入必产生同一输出，是可复现与去重提示的
// 物理基础。本文件仅提供三个无副作用纯函数，不依赖 DB / IO；任何调用方对
// 同一组参数应得同一 id。
//
// 为什么不用 blake2b 或更快哈希：SHA-256 是契约 §3 默认承诺的摘要算法
// （digest 以 sha256: 前缀对外暴露），跨语言/跨实现兼容性优于 blake 系列。
//
// 与冻结实现的口径一致性：
//   - 规范化 JSON 复用 core/gate/validators 的 CanonicalJSON（唯一实现，
//     不另造）：对象键序升序、分隔符紧凑（',' ':'）、UTF-8 直出——与
//     Python json.dumps(sort_keys=True, ensure_ascii=False,
//     separators=(",", ":")) 逐字节同构（golden 测试钉死）。
//   - 摘要即 validators.ContentDigest：规范化文本上取 SHA-256，
//     输出 "sha256:<hex>"。
//
// fail-closed：非法 UTF-8、非有限浮点、不支持类型一律返回错误——
// 宁可拒绝计算也绝不产出歧义哈希（Python 端由 json.dumps 的类型边界
// 隐式保证；Go 端显式化）。
package models

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"unicode/utf8"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
)

// errInvalidUTF8 公式三对非法 UTF-8 输入的 fail-closed 拒绝
// （与 validators 的字符串口径一致：避免替换字符折叠不同字节序列为同一哈希）。
var errInvalidUTF8 = fmt.Errorf("models: 内容摘要输入含非法 UTF-8 序列（fail-closed）")

// sha256Hex 计算 payload 的 SHA-256 hex 摘要并加 sha256: 前缀
// （对齐冻结实现 _sha256_hex：对原始字符串字节直接取摘要，不走 JSON 规范化）。
func sha256Hex(payload string) string {
	sum := sha256.Sum256([]byte(payload))
	return validators.DigestPrefix + hex.EncodeToString(sum[:])
}

// ────────────────────────────────────────────────────────────────────
// 公式一：A/B 级实例 item_version_id
// ────────────────────────────────────────────────────────────────────

// ComputeInstanceID 计算契约 §3 公式一：A/B 级实例的 item_version_id。
//
//	H( template_version_digest, normalized_params, pack_digest,
//	   engine_digest, corpus_digests, locale )
//
// 参数语义（对齐冻结实现 compute_instance_id）：
//   - templateVersionDigest：母题版本摘要（sha256:...）。
//   - normalizedParams：规范化实例化参数（定点/分数运算结果，避免浮点漂移；
//     数值精度由调用方负责，本函数仅做序列化不做数值规范化）。
//   - packDigest：所属学科包摘要。
//   - engineDigest：实例化引擎摘要。
//   - corpusDigests：语料库版本摘要链（被本实例引用的语料版本，按引用顺序）。
//   - locale：语言/地区（zh-CN / en-US 等）。
//
// 字段顺序固定为 (tvd, np, pd, ed, cd, l)；任何字段变化必须导致 id 变化。
// corpusDigests 作为数组进入规范化 JSON，元素顺序影响 id（语料版本链的
// 顺序是谱系的一部分）。
func ComputeInstanceID(
	templateVersionDigest string,
	normalizedParams map[string]any,
	packDigest string,
	engineDigest string,
	corpusDigests []string,
	locale string,
) (string, error) {
	// validators.CanonicalJSON 容器仅接受 []any（fail-closed 不透传具体
	// 切片类型）；digest 链元素均为 string，转换为元素语义不变的 []any。
	cd := make([]any, len(corpusDigests))
	for i, d := range corpusDigests {
		cd[i] = d
	}
	payload := map[string]any{
		"tvd": templateVersionDigest,
		"np":  normalizedParams,
		"pd":  packDigest,
		"ed":  engineDigest,
		"cd":  cd,
		"l":   locale,
	}
	return validators.ContentDigest(payload)
}

// ────────────────────────────────────────────────────────────────────
// 公式二：C/D 级 item_version_id（规范化内容快照哈希）
// ────────────────────────────────────────────────────────────────────

// ComputeCanonicalItemVersionID 计算契约 §3 公式二：C/D 级 item_version_id。
//
//	H( canonical( objective, interaction_ref, content, scoring_ref,
//	              error_bindings ), locale )
//
// 参数语义（对齐冻结实现 compute_canonical_item_version_id）：
//   - objective：知识标注集（契约 §2.2.1）。
//   - interactionRef：交互类型 + 交互参数。
//   - content：题面语义 AST + 素材版本引用。
//   - scoringRef：评分器 + 评分参数。
//   - errorBindings：选项/评分维度 → 错误类型 + 置信规则。顶层是数组
//     （list[dict]，R-Q-06/07），与 DB JSONB 存储结构一致；冻结实现签名
//     类型标注为 dict 仅为类型提示稳定性，实际运行时接受数组。
//   - locale：语言/地区。
//
// 同一内容（六块完全一致 + 同 locale）必得同一 id（D3）；重复命题/粘贴
// 产生同 id，入库时作去重提示而非拒绝。
func ComputeCanonicalItemVersionID(
	objective map[string]any,
	interactionRef map[string]any,
	content map[string]any,
	scoringRef map[string]any,
	errorBindings []any,
	locale string,
) (string, error) {
	payload := map[string]any{
		"o":  objective,
		"ir": interactionRef,
		"c":  content,
		"sr": scoringRef,
		"eb": errorBindings,
		"l":  locale,
	}
	return validators.ContentDigest(payload)
}

// ────────────────────────────────────────────────────────────────────
// 公式三：material_version_id / corpus_version_id
// ────────────────────────────────────────────────────────────────────

// ComputeMaterialVersionID 计算契约 §3 公式三：素材/语料版本的 id。
//
//	H( content_digest )
//
// 为什么直接对 contentDigest 再哈希而非返回原值：调用方可能传入任意对象
// 存储引用（如 "minio:materials/sha256:abc"），统一再哈希一次保证返回值
// 是规范的 sha256:hex 形式，与公式一/二返回值格式一致，便于下游统一处理。
//
// 注意：与公式一/二不同，本公式对原始字符串字节直接取摘要（不走 JSON
// 规范化），与冻结实现 _sha256_hex(content_digest) 逐字节同构。
func ComputeMaterialVersionID(contentDigest string) (string, error) {
	if !utf8.ValidString(contentDigest) {
		return "", errInvalidUTF8
	}
	return sha256Hex(contentDigest), nil
}
