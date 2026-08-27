"""T-W5-002 门证书外键补建与门失败留痕（W5-R Go 重锚定）.

D2「门的数据库级强制」此前只有半个身子：
- 内容表 gate_certificate_id 仅有「published 非空须带证」的 CHECK（0002/0005/
  0020），没有指向 gate_certificate(cert_id) 的外键——契约
  specs/contracts/db/item-model.md §2.2 声明的 FK 物理缺席，cert_FAKE 这类
  任意字符串可直写发布态；
- 冻结编排器 run_gate 在 fail/review 时以占位 cert_id='cert:none' 写
  gate_run.certificate_id，而该占位行从未在任何迁移落地——生产首次门失败即
  撞 fk_gr_certificate、留痕事务整体回滚。X11：失败留痕依赖测试 fixture 预插。

本迁移三段内容（与 db/migrations/0028 成对镜像）：
- A. 六个 gate_certificate_id 引用面补外键（DEFERRABLE INITIALLY DEFERRED 对齐
  发布事务内自由写入序；NOT VALID 只对增量生效——存量行可能带历史占位/伪造 id，
  先数据审计后 VALIDATE CONSTRAINT 留给独立卡，增量封口已让假证无法再新增）。
  覆盖清单与逐表判定理由见同号 up.sql 注释。
- B. gate_run.certificate_id 放宽为可空（验收 #2 方案①，语义正确：没签发证书
  就没有证书引用；NOT NULL 撤销属放宽，存量行零回填），终结占位证书反模式。
- C. gate_failure 失败留痕账（append-only）：一行记一次被拒事实的最小四元组
  ——什么规则（validator_*）、什么输入（artifact_*）、何时（failed_at）、
  为何拒（reason/evidence）。失败也是账面事实；Go core/gate.FailureTrail 为
  其唯一写入面。

契约门自检（CI-Workflows scripts/contract 引擎语义）：upgrade 全部语句为
CREATE_TABLE/TRIGGER + ADD_CONSTRAINT(NOT VALID) + RELAX_NOTNULL 加性面，
无 destructive DDL、无需 ADR 手续；downgrade 为显式逆操作成对回滚。
链序说明：down_revision 指 0026（ai_call_ledger）；0027 已被并行分支预分配。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A 补外键（NOT VALID）→ B 解绑占位证书 → C 失败留痕账."""
    # ---- A. 内容表 → gate_certificate(cert_id) 外键补建（六引用面全量）----
    # 为什么用裸 SQL 而非 op.create_foreign_key：后者不可携带 NOT VALID 与
    # DEFERRABLE INITIALLY DEFERRED 的完整组合声明，裸 SQL 与 up.sql 镜像逐句一致。
    op.execute(
        "ALTER TABLE item_version ADD CONSTRAINT fk_iv_gate_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(
        "ALTER TABLE material_version ADD CONSTRAINT fk_mv_gate_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(
        "ALTER TABLE corpus_version ADD CONSTRAINT fk_cv_gate_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(
        "ALTER TABLE passage ADD CONSTRAINT fk_passage_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(
        "ALTER TABLE publication ADD CONSTRAINT fk_pub_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(
        "ALTER TABLE item_lifecycle_transition ADD CONSTRAINT fk_ilt_certificate "
        "FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) "
        "DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )

    # ---- B. gate_run 占位证书解绑（X11 终结：fail/review 不再挂 cert:none）----
    # RELAX_NOTNULL 单独成 op.alter_column 调用：不与 server_default 合写，
    # 契约引擎按首个命中分类。
    op.alter_column(
        "gate_run", "certificate_id", existing_type=sa.Text(), nullable=True
    )

    # ---- C. gate_failure 门失败留痕账（append-only）----
    op.create_table(
        "gate_failure",
        sa.Column("failure_id", sa.Text(), primary_key=True),
        # 被拒产物类型六值域（编排器文档 item/material/corpus/group/blueprint/audio）
        sa.Column("artifact_type", sa.Text(), nullable=False),
        # 被拒产物引用（如 item_version_id）＝「什么输入」
        sa.Column("artifact_ref", sa.Text(), nullable=False),
        # 「什么规则」：验证器身份与版本
        sa.Column("validator_id", sa.Text(), nullable=False),
        sa.Column("validator_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        # 「为何拒」：人读拒因 + 结构化证据细节
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # 「何时」：留痕时间与登记时间双列，对齐三表既有 created_at 惯例
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "artifact_type IN ('item', 'material', 'corpus', 'group', "
            "'blueprint', 'audio')",
            name="ck_gf_artifact_type_domain",
        ),
    )
    op.create_index(
        "ix_gate_failure_artifact", "gate_failure", ["artifact_ref"]
    )
    op.create_index(
        "ix_gate_failure_failed_at", "gate_failure", ["failed_at"]
    )
    op.execute(
        """CREATE TRIGGER trg_gate_failure_append_only
    BEFORE UPDATE OR DELETE ON gate_failure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();"""
    )


def downgrade() -> None:
    """成对回滚：精确回收本迁移引入的触发器/索引/表/约束并恢复 NOT NULL."""
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_gate_failure_append_only ON gate_failure")
    )
    op.drop_index("ix_gate_failure_failed_at", table_name="gate_failure")
    op.drop_index("ix_gate_failure_artifact", table_name="gate_failure")
    op.drop_table("gate_failure")

    binding.execute(
        sa.text(
            "ALTER TABLE item_lifecycle_transition "
            "DROP CONSTRAINT IF EXISTS fk_ilt_certificate"
        )
    )
    binding.execute(
        sa.text(
            "ALTER TABLE publication DROP CONSTRAINT IF EXISTS fk_pub_certificate"
        )
    )
    binding.execute(
        sa.text(
            "ALTER TABLE passage DROP CONSTRAINT IF EXISTS fk_passage_certificate"
        )
    )
    binding.execute(
        sa.text(
            "ALTER TABLE corpus_version "
            "DROP CONSTRAINT IF EXISTS fk_cv_gate_certificate"
        )
    )
    binding.execute(
        sa.text(
            "ALTER TABLE material_version "
            "DROP CONSTRAINT IF EXISTS fk_mv_gate_certificate"
        )
    )
    binding.execute(
        sa.text(
            "ALTER TABLE item_version "
            "DROP CONSTRAINT IF EXISTS fk_iv_gate_certificate"
        )
    )

    # 显式逆操作：nullable=False 单独成 op（引擎按首个命中分类为 SET_NOT_NULL，
    # 仅升级面 destructive 才受 ADR/逆操作审查，此处为对称恢复）。前提说明见
    # db/migrations/0028_*.down.sql：带数据的真实库回滚须先处置 NULL 留痕行。
    op.alter_column(
        "gate_run", "certificate_id", existing_type=sa.Text(), nullable=False
    )
