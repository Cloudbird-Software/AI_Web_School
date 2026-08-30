// entities.go 承载统一内容模型核心实体的 Go 结构体（Python 冻结基准
// src/core/models/*.py 的 Go 移植；T-W1-003 九实体 + item_group +
// material_license + item_lifecycle_transition）。
//
// 字段与 db/migrations/ 真实 DDL 逐列对齐（DDL 为准，ORM 为辅助语义）：
//   - 0002_item_model.up.sql：material_license / item_template(_version) /
//     item / item_version / material(_version) / corpus_asset(_version) /
//     item_group
//   - 0005_append_only_unify.up.sql：corpus_version 门字段
//     （gate_certificate_id / published_at / retired_at）
//   - 0013_item_param.up.sql：item_param（标定账）
//   - 0018_item_lifecycle.up.sql：item_lifecycle_transition（生命周期账）
//
// 类型映射约定：TEXT/GUID → string（可空 → *string）；JSONB →
// map[string]any / []any（顶层结构按 Python ORM 标注；可空以 nil 表示
// NULL）；TIMESTAMPTZ → time.Time / *time.Time；BOOLEAN → bool；
// INTEGER/BIGINT → int / int64；NUMERIC(4,3) → *float64（纯校验面，
// 不落库，域约束 [0,1] 由 validate.go 强制）；PG ENUM → 本包命名
// string 类型（见 status.go）。
//
// 宪法 D1：以下版本账/状态账行永不 UPDATE/DELETE（只增）——item /
// item_template / material / corpus_asset 行除 current_version_id 外只增；
// item_version / material_version / corpus_version / item_param /
// item_lifecycle_transition 严格 append-only（DB 触发器物理强制）。
// 本结构体不暴露任何持久化方法，仅承载类型与校验面（不接 DB）。
//
// 宪法 A5/X6：核心域零学科特判——本包不 import 任何学科包/学段包。
package models

import "time"

// ────────────────────────────────────────────────────────────────────
// §2.4 material_license：素材/语料库许可决策（R-Q-18/R-G-03）
// ────────────────────────────────────────────────────────────────────

// MaterialLicense 一行 = 一条素材许可决策记录；Decision=Approved 的行可被
// material_version / corpus_version 引用。
type MaterialLicense struct {
	LicenseID    string
	Source       *string
	RightsHolder *string
	Scope        *string
	ExpiresAt    *time.Time
	Decision     LicenseDecision
	CreatedAt    time.Time // server_default now()
}

// ────────────────────────────────────────────────────────────────────
// §2.3 item_template / item_template_version：母题两段式（A/B 级）
// ────────────────────────────────────────────────────────────────────

// ItemTemplate 母题不变身份；CurrentVersionID 仅发布事务可前移（D1）。
type ItemTemplate struct {
	TemplateID       string
	PackID           string
	CurrentVersionID *string // FK→item_template_version，DEFERRABLE 循环外键
	CreatedAt        time.Time
}

// ItemTemplateVersion 母题版本（TemplateVersionID = sha256 of spec，D3）；
// 母题不直接过门，Status 仅 Draft/Published/Retired（无 quarantined）。
type ItemTemplateVersion struct {
	TemplateVersionID string
	TemplateID        string
	DSLVersion        string
	Spec              map[string]any // JSONB NOT NULL（母题 DSL 六大块）
	Status            TemplateVersionStatus
	CreatedAt         time.Time
}

// ────────────────────────────────────────────────────────────────────
// §2.1 item / §2.2 item_version：题目身份 + 不可变内容快照
// ────────────────────────────────────────────────────────────────────

// Item 题目不变身份；CurrentVersionID 指向最新 published 版本
// （DEFERRABLE 循环外键，契约 §6.1 发布事务 COMMIT 时检查）。
type Item struct {
	ItemID            string
	PackID            string
	Tier              Tier
	TemplateVersionID *string // A/B 级实例的母题来源；C/D 级为 nil
	CurrentVersionID  *string
	CreatedAt         time.Time
}

// ItemVersion 题目版本不可变内容快照：一行 = 一个题目的某个版本；
// ItemVersionID 为内容寻址哈希（公式一/二，D3），同内容必同 id。
// 六大块 JSONB 承载全部内容与谱系；GateCertificateID 为门证书唯一真源。
type ItemVersion struct {
	ItemVersionID     string
	ItemID            string
	Status            ItemVersionStatus
	Objective         map[string]any // JSONB NOT NULL（§2.2.1）
	InteractionRef    map[string]any // JSONB NOT NULL
	Content           map[string]any // JSONB NOT NULL
	ScoringRef        map[string]any // JSONB NOT NULL
	ErrorBindings     []any          // JSONB NOT NULL（顶层是数组，R-Q-06/07）
	Lineage           map[string]any // JSONB NOT NULL（§2.2.2）
	RenderedSnapshot  map[string]any // JSONB 可空（quarantined 前必填，CHECK 兜底）
	GateCertificateID *string
	PublishedAt       *time.Time
	RetiredAt         *time.Time
	CreatedAt         time.Time
}

// ────────────────────────────────────────────────────────────────────
// §2.4 material / material_version：素材两段式
// ────────────────────────────────────────────────────────────────────

// Material 素材不变身份；PackID 为 "platform" 表示跨学科通用素材
// （核心域不解释其语义，A5）。
type Material struct {
	MaterialID       string
	Kind             MaterialKind
	PackID           *string
	CurrentVersionID *string // FK→material_version，DEFERRABLE 循环外键
	CreatedAt        time.Time
}

// MaterialVersion 素材版本不可变内容快照：MaterialVersionID =
// H(content_ref)（公式三，D3）；LicenseID 必须指向 approved 许可（R-Q-18）；
// Lineage 复用 §2.2.2 结构（与 item_version.lineage 同构）。
type MaterialVersion struct {
	MaterialVersionID string
	MaterialID        string
	ContentRef        string // 对象存储引用，内容哈希寻址
	LicenseID         string
	Status            ItemVersionStatus // 与 item_version 共用同一 enum
	Lineage           map[string]any    // JSONB NOT NULL
	GateCertificateID *string
	PublishedAt       *time.Time
	RetiredAt         *time.Time
	CreatedAt         time.Time
}

// ────────────────────────────────────────────────────────────────────
// §2.5 corpus_asset / corpus_version：语料库两段式
// ────────────────────────────────────────────────────────────────────

// CorpusAsset 语料库不变身份；Kind 为自由文本（字/词/篇/句/词表/音标/
// 函数/图库，未走 enum）。
type CorpusAsset struct {
	AssetID          string
	Kind             string
	PackID           *string
	CurrentVersionID *string // FK→corpus_version，DEFERRABLE 循环外键
	CreatedAt        time.Time
}

// CorpusVersion 语料库版本快照：VersionID = 内容寻址 digest，进公式一的
// corpus_digests 链；门字段由迁移 0005 补齐（与 material_version 对齐）。
type CorpusVersion struct {
	VersionID         string
	AssetID           string
	ContentRef        string
	LicenseID         string
	Lineage           map[string]any // JSONB NOT NULL
	Status            ItemVersionStatus
	GateCertificateID *string // 迁移 0005 补
	PublishedAt       *time.Time
	RetiredAt         *time.Time
	CreatedAt         time.Time
}

// ────────────────────────────────────────────────────────────────────
// §2.5 item_group：题组 / testlet（R-Z-06）
// ────────────────────────────────────────────────────────────────────

// ItemGroup 一行 = 一个题组（一材多题或纯题目集合）；引用素材版本
// （非素材身份）保证历史试卷可精确回溯（D1）。ItemVersionIDs ≤ 6
// （ck_ig_max_six_items，DB CHECK 兜底）。
type ItemGroup struct {
	ItemGroupID       string
	MaterialVersionID *string
	ItemVersionIDs    []string // TEXT[] NOT NULL，组内顺序由 Ordered 决定
	Ordered           bool     // true=固定顺序；false=可乱序
	Testlet           bool     // true=testlet 单元；false=普通题组
	CreatedAt         time.Time
}

// ────────────────────────────────────────────────────────────────────
// item_param（迁移 0013）：题目参数标定账（D5/D6）
// ────────────────────────────────────────────────────────────────────

// ItemParam 一行 = 一次估计运行对某题版本在某场景下的参数产出；
// 只增不改（新估计 = 新行；DB 触发器物理强制）。
// UNIQUE(item_version_id, purpose_scope, source, method_version, as_of)
// 承载同运行幂等写入。
type ItemParam struct {
	ParamID       string
	ItemVersionID string
	PurposeScope  string         // practice/diagnosis/measurement（D5 禁混估）
	Source        string         // prior_rule/prior_expert/measured_*
	Params        map[string]any // JSONB NOT NULL
	SampleSize    int            // >= 0
	MethodVersion string
	AsOf          time.Time
	CreatedAt     time.Time
}

// ────────────────────────────────────────────────────────────────────
// item_lifecycle_transition（迁移 0018）：题目生命周期状态账（§4.7）
// ────────────────────────────────────────────────────────────────────

// ItemLifecycleTransition 一行 = 一次生命周期状态变更；append-only（D1
// 物理强制，0018 触发器禁 UPDATE/DELETE）。当前状态 = 该 item 最新
// transition（按 CreatedAt 排序）的 ToState。
type ItemLifecycleTransition struct {
	TransitionID      string
	ItemID            string
	FromState         LifecycleState // 零值 "" = NULL = 初始 INSERT（首次进入 ACTIVE）
	ToState           LifecycleState
	GateCertificateID *string // →QUARANTINED / →RETIRED 必填（应用层校验）
	Reason            *string
	HealthScore       *float64 // NUMERIC(4,3)，域 [0,1]（ck_ilt_health_score_domain）
	AnomalyTags       []any    // JSONB 可空，变更时刻健康度快照（审计用）
	CreatedAt         time.Time
}
