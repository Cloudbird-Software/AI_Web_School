// exposure.go 承载曝光账本双轨的查询与预留语义（Python 冻结基准
// src/core/assembly/exposure.py 的 Go 移植；本移植为纯逻辑层——查询/预留
// 面以端口注入，Memory 实现承载 UNIQUE 兜底语义供测试，PG 实现属装配层）。
//
// 架构 v2 §4.4「曝光互斥」：同母题不同卷；跨期不重复（曝光账本双轨——
// 静态按渠道×学科×版本×年级×周队列，在线按学生）；事务性曝光预留。
//   - 查询：Assemble 的 Excluded* 入参由本文件的查询端口供给；
//   - 预留：RecordPaperExposures / RecordStudentExposures 与 paper/paper_item
//     写入在同一事务提交（事务边界归调用方），失败整体回滚，不产生
//     「卷未发出但题已标记曝光」的幽灵占用。
//
// DB 层兜底（迁移 0010）：周队列级 UNIQUE(channel, subject_pack_id,
// week_label, item_version_id) 与学生级 UNIQUE(student_alias_id,
// item_version_id) 各有约束，并发组卷的重复曝光在 INSERT 时失败（应用层
// 查询只是热路径优化）；两表 append-only（D1：曝光是历史事实）。
//
// 宪法 D7：学生轨只存 student_alias_id，本文件端口不接收任何 PII 字段。
package assembly

import (
	"context"
	"fmt"
	"sync"
)

// IDSet 是曝光集/排除集的载体（Python frozenset[str] 的 Go 形）.
type IDSet map[string]struct{}

// NewIDSet 由字符串列表构造集合.
func NewIDSet(ids ...string) IDSet {
	s := make(IDSet, len(ids))
	for _, id := range ids {
		s[id] = struct{}{}
	}
	return s
}

// Has 成员判定.
func (s IDSet) Has(id string) bool {
	_, ok := s[id]
	return ok
}

// PaperExposureRecord 静态轨一行（paper_exposure 的列投影）.
type PaperExposureRecord struct {
	ExposureID        string
	Channel           string
	SubjectPackID     string
	TextbookVersion   *string
	Gradeband         string
	WeekLabel         string
	ItemVersionID     string
	TemplateVersionID *string
	PaperID           *string
}

// StudentExposureRecord 在线轨一行（student_exposure 的列投影；D7：只有
// 匿名 id，无 PII）.
type StudentExposureRecord struct {
	ExposureID        string
	StudentAliasID    string
	ItemVersionID     string
	TemplateVersionID *string
	PaperID           *string
	SessionID         *string
	Purpose           string
}

// ExposureQueryStore 是曝光集查询面端口（assemble 的 Excluded* 供给方）.
type ExposureQueryStore interface {
	// QueueExposedItemVersionIDs 静态轨：某 渠道×学科×周队列 已曝光的题目
	// 版本集（跨期不重复）.
	QueueExposedItemVersionIDs(ctx context.Context, channel, subjectPackID, weekLabel string) (IDSet, error)
	// QueueExposedTemplateVersionIDs 静态轨：某 渠道×学科×周队列 已曝光的
	// 母题版本集（同母题不同卷）.
	QueueExposedTemplateVersionIDs(ctx context.Context, channel, subjectPackID, weekLabel string) (IDSet, error)
	// StudentExposedItemVersionIDs 在线轨：某学生已见过的题目版本集（跨期不重复）.
	StudentExposedItemVersionIDs(ctx context.Context, studentAliasID string) (IDSet, error)
	// StudentExposedTemplateVersionIDs 在线轨：某学生已见过的母题版本集（同母题不同卷）.
	StudentExposedTemplateVersionIDs(ctx context.Context, studentAliasID string) (IDSet, error)
}

// PaperExposureInput 是一次静态轨预留的请求（Python record_paper_exposures
// 关键字形；Items 为入选候选，登记行逐题展开）.
type PaperExposureInput struct {
	Channel         string
	SubjectPackID   string
	Gradeband       string
	WeekLabel       string
	Items           []CandidateItem
	TextbookVersion *string
	PaperID         *string
}

// StudentExposureInput 是一次在线轨预留的请求（Python record_student_exposures）.
type StudentExposureInput struct {
	StudentAliasID string
	Purpose        string
	Items          []CandidateItem
	PaperID        *string
	SessionID      *string
}

// ExposureRecordStore 是曝光预留面端口（与组卷写入同事务的语义边界由调用方
// 保证——端口实现只 append，不做 UPDATE/DELETE）.
type ExposureRecordStore interface {
	// RecordPaperExposures 静态轨曝光预留：把入选题登记到 渠道×学科×周队列，
	// 返回登记行数.
	RecordPaperExposures(ctx context.Context, in PaperExposureInput) (int, error)
	// RecordStudentExposures 在线轨曝光预留：把发给学生（匿名 id）的题登记到
	// 学生轨，返回登记行数.
	RecordStudentExposures(ctx context.Context, in StudentExposureInput) (int, error)
}

// ExposureStore 是曝光账本双轨的完整端口（查询 + 预留）.
type ExposureStore interface {
	ExposureQueryStore
	ExposureRecordStore
}

// MemoryExposureStore 是 ExposureStore 的内存实现：以互斥锁保护的双账本，
// UNIQUE 兜底语义（uq_paper_exposure_queue_item / uq_student_exposure_student_item）
// 在登记时强制，重复登记返回错误（迁移 0010 的 23505 同义）。
type MemoryExposureStore struct {
	mu      sync.Mutex
	paper   []PaperExposureRecord
	student []StudentExposureRecord
}

// NewMemoryExposureStore 构造空账本.
func NewMemoryExposureStore() *MemoryExposureStore { return &MemoryExposureStore{} }

// PaperLedger 返回静态轨账面副本（断言/审计用）.
func (m *MemoryExposureStore) PaperLedger() []PaperExposureRecord {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]PaperExposureRecord(nil), m.paper...)
}

// StudentLedger 返回在线轨账面副本（断言/审计用）.
func (m *MemoryExposureStore) StudentLedger() []StudentExposureRecord {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]StudentExposureRecord(nil), m.student...)
}

// QueueExposedItemVersionIDs 静态轨题目版本集.
func (m *MemoryExposureStore) QueueExposedItemVersionIDs(_ context.Context, channel, subjectPackID, weekLabel string) (IDSet, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := IDSet{}
	for _, r := range m.paper {
		if r.Channel == channel && r.SubjectPackID == subjectPackID && r.WeekLabel == weekLabel {
			out[r.ItemVersionID] = struct{}{}
		}
	}
	return out, nil
}

// QueueExposedTemplateVersionIDs 静态轨母题版本集（模板 NULL 的行不计——
// 与冻结 SQL 的 IS NOT NULL 同义）.
func (m *MemoryExposureStore) QueueExposedTemplateVersionIDs(_ context.Context, channel, subjectPackID, weekLabel string) (IDSet, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := IDSet{}
	for _, r := range m.paper {
		if r.Channel == channel && r.SubjectPackID == subjectPackID && r.WeekLabel == weekLabel && r.TemplateVersionID != nil {
			out[*r.TemplateVersionID] = struct{}{}
		}
	}
	return out, nil
}

// StudentExposedItemVersionIDs 在线轨题目版本集.
func (m *MemoryExposureStore) StudentExposedItemVersionIDs(_ context.Context, studentAliasID string) (IDSet, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := IDSet{}
	for _, r := range m.student {
		if r.StudentAliasID == studentAliasID {
			out[r.ItemVersionID] = struct{}{}
		}
	}
	return out, nil
}

// StudentExposedTemplateVersionIDs 在线轨母题版本集.
func (m *MemoryExposureStore) StudentExposedTemplateVersionIDs(_ context.Context, studentAliasID string) (IDSet, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := IDSet{}
	for _, r := range m.student {
		if r.StudentAliasID == studentAliasID && r.TemplateVersionID != nil {
			out[*r.TemplateVersionID] = struct{}{}
		}
	}
	return out, nil
}

// RecordPaperExposures 静态轨预留（UNIQUE(channel, pack, week, item) 兜底；
// gradeband 值域 ck 同步强制）.
func (m *MemoryExposureStore) RecordPaperExposures(_ context.Context, in PaperExposureInput) (int, error) {
	switch in.Gradeband {
	case GradebandL, GradebandM, GradebandH:
	default:
		return 0, fmt.Errorf("assembly: paper_exposure gradeband %q 越域（ck_paper_exposure_gradeband_domain）", in.Gradeband)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	seen := map[string]struct{}{}
	for _, r := range m.paper {
		key := r.Channel + "\x00" + r.SubjectPackID + "\x00" + r.WeekLabel + "\x00" + r.ItemVersionID
		seen[key] = struct{}{}
	}
	n := 0
	for _, item := range in.Items {
		key := in.Channel + "\x00" + in.SubjectPackID + "\x00" + in.WeekLabel + "\x00" + item.ItemVersionID
		if _, dup := seen[key]; dup {
			return n, fmt.Errorf(
				"assembly: 曝光重复登记（uq_paper_exposure_queue_item）：channel=%s pack=%s week=%s item=%s",
				in.Channel, in.SubjectPackID, in.WeekLabel, item.ItemVersionID)
		}
		seen[key] = struct{}{}
		m.paper = append(m.paper, PaperExposureRecord{
			Channel:           in.Channel,
			SubjectPackID:     in.SubjectPackID,
			TextbookVersion:   in.TextbookVersion,
			Gradeband:         in.Gradeband,
			WeekLabel:         in.WeekLabel,
			ItemVersionID:     item.ItemVersionID,
			TemplateVersionID: item.TemplateVersionID,
			PaperID:           in.PaperID,
		})
		n++
	}
	return n, nil
}

// RecordStudentExposures 在线轨预留（UNIQUE(student_alias_id, item_version_id) 兜底）.
func (m *MemoryExposureStore) RecordStudentExposures(_ context.Context, in StudentExposureInput) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	seen := map[string]struct{}{}
	for _, r := range m.student {
		key := r.StudentAliasID + "\x00" + r.ItemVersionID
		seen[key] = struct{}{}
	}
	n := 0
	for _, item := range in.Items {
		key := in.StudentAliasID + "\x00" + item.ItemVersionID
		if _, dup := seen[key]; dup {
			return n, fmt.Errorf(
				"assembly: 曝光重复登记（uq_student_exposure_student_item）：student=%s item=%s",
				in.StudentAliasID, item.ItemVersionID)
		}
		seen[key] = struct{}{}
		m.student = append(m.student, StudentExposureRecord{
			StudentAliasID:    in.StudentAliasID,
			ItemVersionID:     item.ItemVersionID,
			TemplateVersionID: item.TemplateVersionID,
			PaperID:           in.PaperID,
			SessionID:         in.SessionID,
			Purpose:           in.Purpose,
		})
		n++
	}
	return n, nil
}
