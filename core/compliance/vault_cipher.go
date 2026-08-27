package compliance

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
)

// PII 保险库列级加密原语（T-W5-012；Python 冻结实现
// src/core/compliance/pii_encryption.py 的 Go 重锚定）：
// - AES-256-GCM 认证加密（confidentiality + integrity），算法不换（任务卡
//   non_goals：更换加密算法出局）——256-bit key / 96-bit nonce / 128-bit tag；
// - 应用层加解密：密钥只在 Go 进程内存中短暂存在，不进 SQL bind param /
//   query log（若在 DB 侧 pgcrypto 加解密，密钥会入 SQL，违反 X3）；
// - nonce 每次写入 crypto/rand 随机生成，与密文同列存（nonce 不保密但须唯一，
//   GCM 下 nonce 复用破坏安全性——随机生成满足唯一性概率要求）。
//
// PII 纪律：本文件任何错误信息只含键名/长度等配置元数据，绝不携带密钥字节、
// 密文字节或明文（X3/D7）；解密认证失败一律 ErrCiphertextTampered，禁止返回
// 部分明文。

// PIIVaultKeyEnv 是 PII 保险库主密钥的环境变量名（与冻结实现
// PII_VAULT_KEY_ENV 同名：32 字节 base64 编码）.
const PIIVaultKeyEnv = "PII_VAULT_KEY"

// AES-256-GCM 参数（与冻结实现 _KEY_BYTES/_NONCE_BYTES 同值）.
const (
	vaultKeyBytes   = 32 // AES-256
	vaultNonceBytes = 12 // GCM 标准 96-bit nonce
)

// ErrVaultKey 表示 PII 主密钥配置错误（缺失/非 base64/长度非 32 字节）。
// 对应冻结实现 PIIKeyError；细分原因见 wrap 文本（不含密钥材料本身）.
var ErrVaultKey = errors.New("compliance: PII 主密钥配置错误")

// ErrCiphertextTampered 表示密文完整性校验失败（GCM 认证失败：密文被篡改 /
// nonce 错误 / 密钥错误）。调用方必须视为「密文损坏」整体失败，不存在部分
// 明文可用——与冻结实现 InvalidTag 语义同构.
var ErrCiphertextTampered = errors.New("compliance: PII 密文完整性校验失败（拒绝返回任何明文）")

// LoadMasterKey 加载 PII 主密钥（AES-256 32 字节）。
//
// getenv 为环境变量取值函数（测试注入）；nil 回落 os.Getenv——与冻结实现
// load_master_key(env=None) 的双入口同构。密钥缺失/非 base64/长度非 32 字节
// 一律 ErrVaultKey（fail-closed：配置错误绝不静默回落到弱默认或跳过加密）.
func LoadMasterKey(getenv func(string) string) ([]byte, error) {
	if getenv == nil {
		getenv = os.Getenv
	}
	raw := getenv(PIIVaultKeyEnv)
	if raw == "" {
		return nil, fmt.Errorf("%w: 环境变量 %s 未设置——主密钥必须经环境变量注入（禁止入库/入代码，X3）", ErrVaultKey, PIIVaultKeyEnv)
	}
	key, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		// 只报编码事实，不回显 raw（raw 即密钥材料，进错误信息=进日志，X3）.
		return nil, fmt.Errorf("%w: %s 非 base64 编码", ErrVaultKey, PIIVaultKeyEnv)
	}
	if len(key) != vaultKeyBytes {
		return nil, fmt.Errorf("%w: %s 解码后 %d 字节，预期 %d（AES-256）", ErrVaultKey, PIIVaultKeyEnv, len(key), vaultKeyBytes)
	}
	return key, nil
}

// GenerateMasterKey 生成新主密钥并返回 base64（运维初始化辅助，与冻结实现
// generate_master_key 同义：输出写入 .env 的 PII_VAULT_KEY= 行，永不入仓）.
func GenerateMasterKey() (string, error) {
	key := make([]byte, vaultKeyBytes)
	if _, err := rand.Read(key); err != nil {
		return "", fmt.Errorf("compliance: 熵源不可用，无法生成主密钥: %w", err)
	}
	return base64.StdEncoding.EncodeToString(key), nil
}

// encryptField 加密单字段：返回 (ciphertext, nonce)。每次调用独立随机 nonce
// （96-bit）；输出含 GCM 认证标签（= ciphertext||tag，与冻结实现
// AESGCM.encrypt 同构）；明文按 UTF-8 编码（中文姓名/地址兼容）.
func encryptField(plaintext string, key []byte) ([]byte, []byte, error) {
	gcm, err := vaultGCM(key)
	if err != nil {
		return nil, nil, err
	}
	nonce := make([]byte, vaultNonceBytes)
	if _, err := rand.Read(nonce); err != nil {
		return nil, nil, fmt.Errorf("compliance: 熵源不可用，无法生成 nonce: %w", err)
	}
	return gcm.Seal(nil, nonce, []byte(plaintext), nil), nonce, nil
}

// decryptField 解密单字段：返回明文。认证失败（篡改/nonce 错/密钥错）一律
// ErrCiphertextTampered——错误只述事实，不携带密文/明文字节.
func decryptField(ciphertext, nonce, key []byte) (string, error) {
	gcm, err := vaultGCM(key)
	if err != nil {
		return "", err
	}
	plaintext, err := gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return "", fmt.Errorf("%w: GCM 认证失败（%v）", ErrCiphertextTampered, err)
	}
	return string(plaintext), nil
}

// vaultGCM 构造 AES-256-GCM AEAD 面：密钥长度在此最后把关（进程内直接持钥的
// 调用方也绕不过长度校验，而非信任上游已检）.
func vaultGCM(key []byte) (cipher.AEAD, error) {
	if len(key) != vaultKeyBytes {
		return nil, fmt.Errorf("%w: 密钥长度 %d 字节，预期 %d（AES-256）", ErrVaultKey, len(key), vaultKeyBytes)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("%w: AES 初始化失败: %v", ErrVaultKey, err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("%w: GCM 初始化失败: %v", ErrVaultKey, err)
	}
	return gcm, nil
}
