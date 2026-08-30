// exposure_pg.go 是组卷域存储端口的 PG 实现（审计卡 #151）：
// CandidateStore / ExposureStore 双端口的落库面——查询走连接池默认读面，
// 预留写入必须运行在调用方已 begin 的显式事务内（与 paper/paper_item 写入
// 同进同退，D11；db/queries/assembly.sql 文件头的纪律声明）。
//
// 账本语义（与 Memory 实现同构、与迁移 0010 约束对齐）：
//   - 两账只增不改（D1）：本实现只有 SELECT 与 INSERT——UPDATE/DELETE 无
//     查询面可写，append-only 触发器在 DB 侧物理兜底；
//   - 重复预留由 UNIQUE（uq_paper_exposure_queue_item / uq_student_exposure_
//     student_item）兜底，应用层把 23505 转译为哨兵错误（与 Memory 的重复
//     登记报错同义）；
//   - gradeband 值域在构造期校验（ck_paper_exposure_gradeband_domain 同值域）。
package assembly

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// ErrDuplicateExposure 是重复曝光预留的哨兵（23505 转译；与 Memory 实现的
// 重复登记报错同语义——UNIQUE 兜底在 DB，应用层只做转译）.
var ErrDuplicateExposure = errors.New("assembly: 曝光预留重复（UNIQUE 兜底）")

// ErrNoExecutor 是存储面未装配（db 为 nil）的哨兵（与 core/content 同惯例：
// 构造不报错，调用路径 fail-closed）.
var ErrNoExecutor = errors.New("assembly: 存储面未装配")

// Executor 是执行面最小接口（pgxpool 连接 / pgx.Tx 均满足）.
type Executor interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// PGStore 是组卷域端口的 PG 实现：读面直连池；写面经 WithTx 绑定显式事务.
type PGStore struct {
	db Executor
	qs *dbgen.Queries
}

// NewPGStore 把执行面绑定为组卷域存储（db 允许 nil——构造不报错，调用即
// ErrNoExecutor）.
func NewPGStore(db Executor) *PGStore {
	return &PGStore{db: db, qs: dbgen.New(db)}
}

// WithTx 把 PGStore 绑定到调用方已 begin 的事务执行面（预留写路径专用：
// 返回的新实例与原实例共享 nil 校验纪律，事务边界归最外层调用方）.
func (s *PGStore) WithTx(tx Executor) *PGStore {
	return &PGStore{db: tx, qs: dbgen.New(tx)}
}

// 编译期锚定：三个端口一个实现全担（与 Memory 面同构）.
var (
	_ CandidateStore      = (*PGStore)(nil)
	_ ExposureQueryStore  = (*PGStore)(nil)
	_ ExposureRecordStore = (*PGStore)(nil)
)

// LoadCandidates 实现 CandidateStore：serving 视图（published 过滤的单一
// 事实源）按 学科包×学段 取候选池。行 → CandidateItem 的规范化复用
// CandidateFromServingRow（与 Memory/冻结实现同一规范化面）.
func (s *PGStore) LoadCandidates(ctx context.Context, subjectPackID, gradeband string) ([]ServingRow, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	rows, err := s.qs.LoadServingCandidates(ctx, dbgen.LoadServingCandidatesParams{
		PackID:    subjectPackID,
		Gradeband: gradeband,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: serving 候选池查询失败: %w", err)
	}
	out := make([]ServingRow, 0, len(rows))
	for _, r := range rows {
		tpl := textValue(r.TemplateVersionID)
		row := ServingRow{
			PackID:            r.PackID,
			ItemVersionID:     r.ItemVersionID,
			ItemID:            r.ItemID,
			TemplateVersionID: tpl,
		}
		if err := unmarshalJSONB(r.Objective, &row.Objective); err != nil {
			return nil, fmt.Errorf("assembly: 候选 %s objective 反序列化失败: %w", r.ItemVersionID, err)
		}
		if err := unmarshalJSONB(r.InteractionRef, &row.InteractionRef); err != nil {
			return nil, fmt.Errorf("assembly: 候选 %s interaction_ref 反序列化失败: %w", r.ItemVersionID, err)
		}
		if err := unmarshalJSONB(r.Lineage, &row.Lineage); err != nil {
			return nil, fmt.Errorf("assembly: 候选 %s lineage 反序列化失败: %w", r.ItemVersionID, err)
		}
		out = append(out, row)
	}
	return out, nil
}

// QueueExposedItemVersionIDs 实现 ExposureQueryStore（静态轨题集）.
func (s *PGStore) QueueExposedItemVersionIDs(ctx context.Context, channel, subjectPackID, weekLabel string) (IDSet, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	ids, err := s.qs.QueueExposedItemVersionIDs(ctx, dbgen.QueueExposedItemVersionIDsParams{
		Channel:       channel,
		SubjectPackID: subjectPackID,
		WeekLabel:     weekLabel,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: 静态轨曝光题集查询失败: %w", err)
	}
	return toIDSet(ids), nil
}

// QueueExposedTemplateVersionIDs 实现 ExposureQueryStore（静态轨母题集；
// 语句内 IS NOT NULL 与冻结 SQL 同义）.
func (s *PGStore) QueueExposedTemplateVersionIDs(ctx context.Context, channel, subjectPackID, weekLabel string) (IDSet, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	ids, err := s.qs.QueueExposedTemplateVersionIDs(ctx, dbgen.QueueExposedTemplateVersionIDsParams{
		Channel:       channel,
		SubjectPackID: subjectPackID,
		WeekLabel:     weekLabel,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: 静态轨曝光母题集查询失败: %w", err)
	}
	return toTextIDSet(ids), nil
}

// StudentExposedItemVersionIDs 实现 ExposureQueryStore（在线轨题集）.
func (s *PGStore) StudentExposedItemVersionIDs(ctx context.Context, studentAliasID string) (IDSet, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	ids, err := s.qs.StudentExposedItemVersionIDs(ctx, studentAliasID)
	if err != nil {
		return nil, fmt.Errorf("assembly: 在线轨曝光题集查询失败: %w", err)
	}
	return toIDSet(ids), nil
}

// StudentExposedTemplateVersionIDs 实现 ExposureQueryStore（在线轨母题集）.
func (s *PGStore) StudentExposedTemplateVersionIDs(ctx context.Context, studentAliasID string) (IDSet, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoExecutor
	}
	ids, err := s.qs.StudentExposedTemplateVersionIDs(ctx, studentAliasID)
	if err != nil {
		return nil, fmt.Errorf("assembly: 在线轨曝光母题集查询失败: %w", err)
	}
	return toTextIDSet(ids), nil
}

// RecordPaperExposures 实现 ExposureRecordStore：入选题逐题展开登记（写面
// 必须在 WithTx 绑定的事务执行面上；23505 → ErrDuplicateExposure）.
func (s *PGStore) RecordPaperExposures(ctx context.Context, in PaperExposureInput) (int, error) {
	if s == nil || s.db == nil {
		return 0, ErrNoExecutor
	}
	switch in.Gradeband {
	case GradebandL, GradebandM, GradebandH:
	default:
		return 0, fmt.Errorf("assembly: paper_exposure gradeband %q 越域（ck_paper_exposure_gradeband_domain）", in.Gradeband)
	}
	n := 0
	for _, it := range in.Items {
		tpl := textOrNullPtr(it.TemplateVersionID)
		paper := textOrNullPtr(in.PaperID)
		textbook := textOrNullPtr(in.TextbookVersion)
		err := s.qs.InsertPaperExposure(ctx, dbgen.InsertPaperExposureParams{
			ExposureID:        newExposureID(),
			Channel:           in.Channel,
			SubjectPackID:     in.SubjectPackID,
			TextbookVersion:   textbook,
			Gradeband:         in.Gradeband,
			WeekLabel:         in.WeekLabel,
			ItemVersionID:     it.ItemVersionID,
			TemplateVersionID: tpl,
			PaperID:           paper,
		})
		if err != nil {
			if isUniqueViolation(err) {
				return n, fmt.Errorf("%w: %s@%s/%s", ErrDuplicateExposure, in.Channel, in.WeekLabel, it.ItemVersionID)
			}
			return n, fmt.Errorf("assembly: 静态轨曝光预留失败: %w", err)
		}
		n++
	}
	return n, nil
}

// RecordStudentExposures 实现 ExposureRecordStore（在线轨；写面事务纪律同上）.
func (s *PGStore) RecordStudentExposures(ctx context.Context, in StudentExposureInput) (int, error) {
	if s == nil || s.db == nil {
		return 0, ErrNoExecutor
	}
	n := 0
	for _, it := range in.Items {
		tpl := textOrNullPtr(it.TemplateVersionID)
		paper := textOrNullPtr(in.PaperID)
		session := textOrNullPtr(in.SessionID)
		err := s.qs.InsertStudentExposure(ctx, dbgen.InsertStudentExposureParams{
			ExposureID:        newExposureID(),
			StudentAliasID:    in.StudentAliasID,
			ItemVersionID:     it.ItemVersionID,
			TemplateVersionID: tpl,
			PaperID:           paper,
			SessionID:         session,
			Purpose:           in.Purpose,
		})
		if err != nil {
			if isUniqueViolation(err) {
				return n, fmt.Errorf("%w: %s@%s", ErrDuplicateExposure, in.StudentAliasID, it.ItemVersionID)
			}
			return n, fmt.Errorf("assembly: 在线轨曝光预留失败: %w", err)
		}
		n++
	}
	return n, nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}

func toIDSet(ids []string) IDSet {
	out := make(IDSet, len(ids))
	for _, id := range ids {
		out[id] = struct{}{}
	}
	return out
}

func textValue(t pgtype.Text) string {
	if !t.Valid {
		return ""
	}
	return t.String
}

func toTextIDSet(ids []pgtype.Text) IDSet {
	out := make(IDSet, len(ids))
	for _, t := range ids {
		if t.Valid {
			out[t.String] = struct{}{}
		}
	}
	return out
}

func textOrNullPtr(p *string) pgtype.Text {
	if p == nil || *p == "" {
		return pgtype.Text{}
	}
	return pgtype.Text{String: *p, Valid: true}
}

func unmarshalJSONB(b []byte, v any) error {
	if len(b) == 0 {
		return nil
	}
	return json.Unmarshal(b, v)
}

// newExposureID 发号 exposure_id（应用侧发号惯例：crypto/rand 16 字节 hex；
// 唯一性由列 UNIQUE 兜底，发号只保证低碰撞与可追溯）.
func newExposureID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand 失败属系统级故障，fail-loud 不退化到时间戳伪随机.
		panic(fmt.Sprintf("assembly: exposure_id 发号失败: %v", err))
	}
	return hex.EncodeToString(b[:])
}
