// credential.go 承载 T-W5-007 的服务端凭证治理：环境变量注入凭证的集中
// 登记面（CredentialRegistry）与错误/日志出口的统一 mask 层（Mask）。
//
// 宪法依据：D9「服务端凭证（含工作台 token）永不经任何 API 回传」、X3
// 「禁止任何密钥/凭证进入仓库、日志、prompt」。ResolveSecret（config.go）
// 是单个签名密钥的 fail-closed 解析惯例，本文件把它推广到「全部环境变量
// 注入凭证」：每个凭证启动时显式登记（name→provider+loader），校验形态按
// 凭证分级——auth 类缺失硬失败（与 ResolveSecret 生产缺钥同语义），LLM 类
// 缺失启动告警 + 调用期拒绝（降级也必须是显式拒绝，而不是空值继续跑）。
//
// 凭证值的零结构化导出：Secret 类型的 String/LogValue/MarshalText 全部
// 返回固定掩码形态，fmt/log/slog/encoding/json 等任何结构化出口都拿不到
// 原值；原值的唯一出口是 Reveal()，调用点在代码审查中逐个可见。
//
// 本文件零新依赖（仅标准库），且不 import 本包之外的任何仓内包（核心域
// 零外部耦合纪律；X6/GO-3 由 import-boundary lint 独立强制）。
package auth

import (
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// Masked 是凭证值的固定掩码形态（`***`）。屏蔽后文本保持「有东西被打码」
// 的可观测信号——空串会让日志看起来像字段缺失，掩盖泄密未遂事件。
const Masked = "***"

// Secret 是服务端凭证值的零导出包装。零值 Secret 合法（表示未配置），
// Empty 可判定；构造只能经 NewSecret（包外无法绕过掩码层造值）。
//
// 为什么是 struct 而非 string 别名：命名 string 类型在传给 string 形参、
// 参与字符串运算时过于顺滑，意外泄漏面大；struct 让「把凭证当普通字符串
// 用」在编译期就失败，Reveal 成为唯一显式出口。
type Secret struct {
	value string
}

// NewSecret 用原值构造 Secret。调用点即「凭证进入进程」的审计点。
func NewSecret(value string) Secret {
	return Secret{value: value}
}

// Reveal 返回凭证原值——全仓唯一的原值出口。只允许传给凭证的消费原语
// （签名器、出站客户端构造器等）；禁止拼进错误、日志、响应体（X3/D9）。
func (s Secret) Reveal() string {
	return s.value
}

// Empty 报告凭证是否未配置。
func (s Secret) Empty() bool {
	return s.value == ""
}

// String 实现 fmt.Stringer：任何 %v/%s 形化输出只见掩码。这是「凭证值
// 零结构化导出」的第一道：log.Printf、fmt.Errorf 包装、测试断言消息等
// 全部经过这里。
func (s Secret) String() string {
	return Masked
}

// LogValue 实现 slog.LogValuer：结构化日志字段同样只见掩码。
func (s Secret) LogValue() slog.Value {
	return slog.StringValue(Masked)
}

// MarshalText 实现 encoding.TextMarshaler：encoding/json 与模板引擎的
// 序列化出口只见掩码（encoding/json 对未实现 json.Marshaler 的类型回退
// 到 TextMarshaler）。
func (s Secret) MarshalText() ([]byte, error) {
	return []byte(Masked), nil
}

// CredentialClass 是凭证的 fail-closed 分级（本卡设计核心）：拿不到凭证
// 的处置形态不按调用方心情，按凭证域分级固定。
type CredentialClass string

const (
	// ClassAuth 认证域凭证（签名密钥、工作台 token 等）：进程身份的根。
	// 缺失 = 启动硬失败——没有它的服务宁可拒绝启动，也不能以「空凭证
	// 也能跑」的形态存在（ResolveSecret 同语义的登记面推广）。
	ClassAuth CredentialClass = "auth"
	// ClassLLM 出站模型域凭证（网关角色 key 等）：缺失 = 启动告警 +
	// 调用期拒绝。模型供应商凭证缺位不必然瘫痪整个教学进程（本地学科
	// 链路可独立服务），因此启动只告警；但调用期必须显式拒绝——降级的
	// 形态是「可区分的拒绝」，绝不是「拿空 key 出站」。
	ClassLLM CredentialClass = "llm"
)

// Valid 报告 class 是否为合法二值域（越域登记在启动期拦截）。
func (c CredentialClass) Valid() bool {
	switch c {
	case ClassAuth, ClassLLM:
		return true
	default:
		return false
	}
}

// 哨兵错误：调用方按 errors.Is 分支处理（错误文本是审计资产也是泄漏面，
// 本组哨兵不携带任何凭证派生值）。
var (
	// ErrUnknownCredential 表示 Resolve 的名字未登记——绕过登记面的散装
	// 凭证读取在结构上不可达（登记面存在的意义）。
	ErrUnknownCredential = errors.New("auth: 凭证未登记")
	// ErrCredentialMissing 表示凭证已登记但拿不到值：auth 类在启动期就应
	// 硬失败；调用期出现该错误 = 拒绝本次操作（LLM 类的常态拒绝形态、
	// auth 类的纵深兜底）。错误只指明登记名与环境变量名。
	ErrCredentialMissing = errors.New("auth: 凭证未配置")
	// ErrDuplicateCredential 表示重复登记同名凭证（登记面变更是显式动作，
	// 静默覆盖会让审计口径漂移，与总线 ErrDuplicateTarget 同纪律）。
	ErrDuplicateCredential = errors.New("auth: 凭证已登记")
	// ErrInvalidCredential 表示登记参数非法（名字/环境变量名为空、分级
	// 越域、loader 缺失）。
	ErrInvalidCredential = errors.New("auth: 凭证登记参数非法")
)

// Loader 是凭证值加载器：返回 (值, 是否已配置)。false 表示来源未提供
// （如环境变量未设置）。loader 只在启动校验与首次调用期解析时各跑一次，
// 结果被缓存——「每次调用重读环境」既无必要，也让凭证在进程生命周期内
// 可被静默替换（审计不可接受）。
type Loader func() (Secret, bool)

// CredentialSpec 是一条凭证登记项：登记面的最小事实单元。
type CredentialSpec struct {
	// Name 是登记名（调用期 Resolve 的寻址键，如 "signing_key"）。
	// 它会出现在错误与告警文本中，必须是稳定标识而非描述句。
	Name string
	// EnvVar 是注入来源的环境变量名。集中在此而非散落 os.Getenv：
	// 密钥来源是审计关注的配置面，收口一处才可被检索与追责（X3）。
	EnvVar string
	// Provider 是凭证的提供方/用途说明（如 "litellm-gateway"），落审计面。
	Provider string
	// Class 决定缺失凭证的 fail-closed 形态（两级校验语义见 CredentialClass）。
	Class CredentialClass
	// Loader 值加载器（生产=os.Getenv 闭包；测试=内存 map 闭包）。
	Loader Loader
}

// credentialEntry 是登记后的内部态：spec + 缓存的已加载值。entry 指针
// 一经 Register 即稳定（登记面无注销），spec 字段可无锁读取。
type credentialEntry struct {
	spec  CredentialSpec
	value Secret // 已加载值；Empty 表示尚未成功加载
}

// defaultSensitiveKeys 是默认键名打码词表（RE2 交替片段形态）：值打码
// 之外的第二层——按键名形态识别并打码敏感键值对。词表覆盖面对齐
// docs/secrets.md 的 X3 扫描正则（api[_-]?key|secret|password|token）并补
// authorization/credential/passwd 两个常见泄漏键。已登记凭证的 EnvVar 名
// 会自动并入，无需在此重复。
var defaultSensitiveKeys = []string{
	"api[_-]?key",
	"apikey",
	"authorization",
	"credential",
	"passwd",
	"password",
	"secret",
	"token",
}

// CredentialRegistry 是环境变量注入凭证的集中登记面：name→provider+loader
// 的唯一事实源 + 启动期两级校验 + 调用期解析 + 统一 mask 层。
//
// 并发契约：Register/RegisterSensitiveKeys/Validate 属启动期装配调用，
// Resolve/Mask 属请求期调用；内部状态由读写锁保护，装配后并发读安全
// （-race 测试覆盖）。登记建议只发生在 main 装配期——登记面在进程启动
// 后扩张会让「这台进程持有哪些凭证」的审计问题变成时间函数。
type CredentialRegistry struct {
	mu      sync.RWMutex
	entries map[string]*credentialEntry
	// sensitiveKeys 是键名打码词表（RE2 交替形态片段，不含捕获组）。
	sensitiveKeys []string
	// keyPattern 是 sensitiveKeys 编译出的键值对打码正则；词表变更时在
	// 写锁内重建（启动期低频），Mask 只读复用。
	keyPattern *regexp.Regexp
	// values 是已加载凭证值的打码器（值按长度降序，长值优先防止子串
	// 残留）；加载面变更时在写锁内重建。
	values *strings.Replacer
}

// NewCredentialRegistry 构造空登记面。键名打码词表以默认词表启动——
// 键名层不依赖任何凭证登记即可生效（防御纵深不因「还没登记」而缺席）。
func NewCredentialRegistry() *CredentialRegistry {
	r := &CredentialRegistry{entries: make(map[string]*credentialEntry)}
	r.sensitiveKeys = append(r.sensitiveKeys, defaultSensitiveKeys...)
	r.rebuildLocked()
	return r
}

// rebuildLocked 重建键名正则与值打码器。必须在写锁内调用。
func (r *CredentialRegistry) rebuildLocked() {
	sort.Strings(r.sensitiveKeys)
	r.keyPattern = regexp.MustCompile(`(?i)(` + strings.Join(r.sensitiveKeys, "|") + `)` +
		`[ \t]*["']?[=:][ \t]*["']?[^"',;\r\n]+`)
	// 长值优先：短值是长值子串时先替换短值会留下混合残片。
	vals := make([]string, 0, len(r.entries))
	for _, e := range r.entries {
		if !e.value.Empty() {
			vals = append(vals, e.value.Reveal())
		}
	}
	sort.Slice(vals, func(i, j int) bool { return len(vals[i]) > len(vals[j]) })
	pairs := make([]string, 0, 2*len(vals))
	for _, v := range vals {
		pairs = append(pairs, v, Masked)
	}
	r.values = strings.NewReplacer(pairs...)
}

// Register 登记一条凭证。同名重复登记报错而不覆盖（与总线 RegisterTarget
// 同纪律：登记面变更必须显式两步，审计可读性优先于便利性）。登记的
// EnvVar 自动并入键名打码词表——`SCHOOL_AUTH_SECRET=...` 形态的日志行
// 无需凭证加载即被键名层覆盖。
func (r *CredentialRegistry) Register(spec CredentialSpec) error {
	if spec.Name == "" || spec.EnvVar == "" || spec.Provider == "" ||
		!spec.Class.Valid() || spec.Loader == nil {
		return fmt.Errorf("%w: name/env_var/provider/class/loader 必填", ErrInvalidCredential)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.entries[spec.Name]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateCredential, spec.Name)
	}
	r.entries[spec.Name] = &credentialEntry{spec: spec}
	dup := false
	for _, k := range r.sensitiveKeys {
		if k == spec.EnvVar {
			dup = true
			break
		}
	}
	if !dup {
		r.sensitiveKeys = append(r.sensitiveKeys, spec.EnvVar)
	}
	r.rebuildLocked()
	return nil
}

// RegisterSensitiveKeys 追加键名打码词表（RE2 交替片段形态；大小写不
// 敏感由词表正则统一保证）。用于登记面之外的项目特有敏感字段名。
func (r *CredentialRegistry) RegisterSensitiveKeys(keys ...string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, k := range keys {
		if k == "" {
			continue
		}
		dup := false
		for _, have := range r.sensitiveKeys {
			if have == k {
				dup = true
				break
			}
		}
		if !dup {
			r.sensitiveKeys = append(r.sensitiveKeys, k)
		}
	}
	r.rebuildLocked()
}

// Validate 启动期两级校验（fail-closed 形态按凭证分级）：
//
//   - ClassAuth 凭证缺失 → 返回 error（含登记名与环境变量名，绝不含值），
//     调用方（main 装配）必须阻断启动——与 ResolveSecret 生产缺钥同语义；
//   - ClassLLM 凭证缺失 → 追加 warning，启动继续；调用期由 Resolve 显式
//     拒绝（告警必须由调用方落启动日志——「缺失」不能静默）；
//   - 加载成功的凭证值并入 mask 值打码面（打码面只增不减）。
//
// 校验按登记名排序遍历：warnings/错误聚合顺序确定，审计可读。
func (r *CredentialRegistry) Validate() (warnings []string, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	names := make([]string, 0, len(r.entries))
	for name := range r.entries {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		e := r.entries[name]
		v, ok := e.spec.Loader()
		if !ok || v.Empty() {
			switch e.spec.Class {
			case ClassAuth:
				err = errors.Join(err, fmt.Errorf("%w: auth 凭证 %s（环境变量 %s）缺失，按 D9 fail-closed 拒绝启动",
					ErrCredentialMissing, name, e.spec.EnvVar))
			default:
				warnings = append(warnings, fmt.Sprintf(
					"credential: LLM 凭证 %s（环境变量 %s）未配置：启动继续，相关调用将被拒绝",
					name, e.spec.EnvVar))
			}
			continue
		}
		e.value = v
	}
	r.rebuildLocked()
	return warnings, err
}

// Resolve 调用期凭证解析：已加载值直取；未加载则懒加载一次并缓存；
// 仍拿不到 → ErrCredentialMissing（LLM 类调用期拒绝形态）。未登记名字 →
// ErrUnknownCredential：散装凭证读取在结构上不可达。
//
// 错误只携带登记名与来源变量名——错误链是日志出口，绝不含凭证值（X3）。
func (r *CredentialRegistry) Resolve(name string) (Secret, error) {
	r.mu.RLock()
	e, ok := r.entries[name]
	var v Secret
	envVar := ""
	if ok {
		v, envVar = e.value, e.spec.EnvVar
	}
	r.mu.RUnlock()
	if !ok {
		return Secret{}, fmt.Errorf("%w: %q", ErrUnknownCredential, name)
	}
	if !v.Empty() {
		return v, nil
	}
	// 懒加载兜底（Validate 之前/之后的首次调用）：命中也并入打码面。
	fresh, present := e.spec.Loader()
	r.mu.Lock()
	if present && !fresh.Empty() {
		e.value = fresh
		r.rebuildLocked()
		r.mu.Unlock()
		return fresh, nil
	}
	r.mu.Unlock()
	return Secret{}, fmt.Errorf("%w: %s（环境变量 %s）", ErrCredentialMissing, name, envVar)
}

// Mask 是错误/日志出口的统一 mask 层：文本中
//
//   - 出现任何已加载凭证值 → 整段替换为 ***（值打码面，长值优先）；
//   - 出现「敏感键名=值 / 键名: 值」形态 → 重写为 键名=***（键名层，
//     覆盖尚未加载但按形态可识别的敏感键值对；键名保留供审计定位）。
//
// 键名层的值部贪婪吞到引号/逗号/分号/行尾（`Authorization: Bearer x.y.z`
// 与 JSON 形态 `"api_key":"v"` 均整体打码）——错误/日志出口的打码方向是
// 宁可多打码不漏打码，被连带打码的邻近诊断文本可从完整错误链重查。
//
// 中间件与 handler 的全部动态文本（错误详情、诊断串、panic 值）落日志前
// 经此收口；响应体按既有惯例只含固定 error_class 常量，本函数是其纵深
// 防御位。Mask 幂等：对已打码文本重复处理结果不变。
func (r *CredentialRegistry) Mask(text string) string {
	r.mu.RLock()
	keyPattern, values := r.keyPattern, r.values
	r.mu.RUnlock()
	if keyPattern == nil || values == nil {
		return text
	}
	// 先值后键名：已加载值是确凿敏感物优先整段抹除；键名层随后兜底
	// 残余形态。值替换产物 *** 不会被键名层二次改写（掩码不是合法值部
	// 之外的新敏感物，重写结果仍是 键=***）。
	return keyPattern.ReplaceAllString(values.Replace(text), "$1="+Masked)
}
