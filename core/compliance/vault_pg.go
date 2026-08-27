package compliance

import (
	"context"
	"errors"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

// PGVaultStore 是 VaultStore 的 PG 生产实现（0030 角色前提下的语句面全部
// 来自 db/queries/pii_vault.sql 的类型安全生成方法）。
//
// 最小权限映射（应用执行面 × 0030 DB 角色）：
//   - ReadIdentity / ListAccessLog 的 q ↔ pii_vault_reader 角色连接
//     （SELECT student_identity / SELECT access_log）；
//   - WriteIdentity 的 q ↔ pii_vault_writer 角色连接（INSERT student_identity）；
//   - AppendAccessLog 的 q（审计独立事务面）↔ pii_vault_writer 角色连接
//     （INSERT access_log；reader 已被 0030 收回写审计）。
//
// 事务纪律（S4/D11）：本类型不持有连接、不自 begin/commit——执行面由调用方
// 传入且必须是已 begin 的事务面；vault 业务与审计的两条事务边界在
// VaultService 的双 Executor 签名上分离.
type PGVaultStore struct{}

// NewPGVaultStore 构造 PG 实现.
func NewPGVaultStore() *PGVaultStore { return &PGVaultStore{} }

// WriteIdentity 实现 VaultStore：直标识密文追加；PK 冲突（23505）翻译为
// ErrIdentityExists（一次写入后不可改写，出 Go 进程前给出可判定错误）.
func (PGVaultStore) WriteIdentity(ctx context.Context, q Executor, row IdentityCiphertext) error {
	if q == nil {
		return ErrNoTransaction
	}
	err := dbgen.New(q).InsertStudentIdentity(ctx, dbgen.InsertStudentIdentityParams{
		StudentAliasID:          pgtype.UUID{Bytes: row.StudentAliasID, Valid: true},
		NameCiphertext:          row.NameCiphertext,
		NameNonce:               row.NameNonce,
		PhoneCiphertext:         row.PhoneCiphertext,
		PhoneNonce:              row.PhoneNonce,
		AddressCiphertext:       row.AddressCiphertext,
		AddressNonce:            row.AddressNonce,
		ParentContactCiphertext: row.ParentContactCiphertext,
		ParentContactNonce:      row.ParentContactNonce,
	})
	if err != nil {
		return fmt.Errorf("compliance/pg vault insert identity: %w", mapVaultConflict(err))
	}
	return nil
}

// ReadIdentity 实现 VaultStore：无行翻译为 ErrIdentityNotFound，pgx.ErrNoRows
// 不穿透（与内存实现对同一输入同一条哨兵错误）.
func (PGVaultStore) ReadIdentity(ctx context.Context, q Executor, alias [16]byte) (*IdentityCiphertext, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	rowGen, err := dbgen.New(q).GetStudentIdentity(ctx, pgtype.UUID{Bytes: alias, Valid: true})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrIdentityNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("compliance/pg vault get identity: %w", err)
	}
	out := IdentityCiphertext{
		StudentAliasID:          alias,
		NameCiphertext:          rowGen.NameCiphertext,
		NameNonce:               rowGen.NameNonce,
		PhoneCiphertext:         rowGen.PhoneCiphertext,
		PhoneNonce:              rowGen.PhoneNonce,
		AddressCiphertext:       rowGen.AddressCiphertext,
		AddressNonce:            rowGen.AddressNonce,
		ParentContactCiphertext: rowGen.ParentContactCiphertext,
		ParentContactNonce:      rowGen.ParentContactNonce,
		CreatedAt:               rowGen.CreatedAt.Time,
	}
	return &out, nil
}

// AppendAccessLog 实现 VaultStore：审计独立事务执行面上的 INSERT（调用方
// 应传 auditQ——非业务事务面；角色上该面持 pii_vault_writer）.
func (PGVaultStore) AppendAccessLog(ctx context.Context, q Executor, entry AccessLogEntry) error {
	if q == nil {
		return ErrNoTransaction
	}
	err := dbgen.New(q).InsertVaultAccessLog(ctx, dbgen.InsertVaultAccessLogParams{
		AccessID:       pgtype.UUID{Bytes: entry.AccessID, Valid: true},
		StudentAliasID: pgtype.UUID{Bytes: entry.StudentAliasID, Valid: true},
		Accessor:       entry.Accessor,
		AccessedAt:     pgtype.Timestamptz{Time: entry.AccessedAt, Valid: true},
		Purpose:        entry.Purpose,
	})
	if err != nil {
		return fmt.Errorf("compliance/pg vault insert access_log: %w", err)
	}
	return nil
}

// ListAccessLog 实现 VaultStore：审计账只读投影（accessed_at, access_id 升序，
// 与内存实现同序）.
func (PGVaultStore) ListAccessLog(ctx context.Context, q Executor, alias [16]byte) ([]AccessLogEntry, error) {
	if q == nil {
		return nil, ErrNoTransaction
	}
	rowsGen, err := dbgen.New(q).ListVaultAccessLog(ctx, pgtype.UUID{Bytes: alias, Valid: true})
	if err != nil {
		return nil, fmt.Errorf("compliance/pg vault list access_log: %w", err)
	}
	out := make([]AccessLogEntry, 0, len(rowsGen))
	for i := range rowsGen {
		out = append(out, AccessLogEntry{
			AccessID:       rowsGen[i].AccessID.Bytes,
			StudentAliasID: rowsGen[i].StudentAliasID.Bytes,
			Accessor:       rowsGen[i].Accessor,
			AccessedAt:     rowsGen[i].AccessedAt.Time,
			Purpose:        rowsGen[i].Purpose,
		})
	}
	return out, nil
}

// mapVaultConflict 把 PK 唯一冲突翻译为 ErrIdentityExists（errors.Is 可判）；
// 非唯一冲突原样放行——异常不泄漏，但也绝不吞真故障.
func mapVaultConflict(err error) error {
	var pe *pgconn.PgError
	if errors.As(err, &pe) && pe.Code == sqlStateUniqueViolation {
		// 双 %w：哨兵错误与原始驱动错误都留在 wrap 链里（与 consent 侧
		// mapUniqueViolation 同纪律）.
		return fmt.Errorf("%w: %w", ErrIdentityExists, err)
	}
	return err
}
