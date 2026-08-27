"""T-W5-012 PII 保险库角色职责分离（pii_vault_writer / pii_vault_reader）.

缺陷事实（任务卡目标说明 + 0014 现状实读）：
- 只有单角色 pii_vault_reader，却被同时授 SELECT student_identity（读身份）与
  INSERT access_log（写审计）——读身份与写审计混在同一角色；写身份
  （INSERT student_identity）反而没有任何角色承载，应用只能以表属主连接直写，
  D9 最小权限落空；
- reader 有 INSERT access_log 却无 SELECT access_log——「每次访问必留痕」的
  审计账（D7）连读身份自己都无法复核，审计完整性不可验证；
- 冻结实现 read_identity 的审计写入与业务同事务，业务回滚时审计静默消失——
  Go 侧 core/compliance.VaultService 以双 Executor 独立事务终结该缺陷，审计面
  执行面持 pii_vault_writer 角色（独立连接），本迁移为其提供角色前提。

三段内容（与 db/migrations/0030 成对镜像，全为授权面 GRANT/REVOKE + 幂等
CREATE ROLE，零表结构变更、无 destructive DDL）：
- A. 新建 pii_vault_writer 写身份角色（幂等 DO 块，0014 同惯例）；
- B. writer 授权：INSERT student_identity + INSERT access_log（写身份+写审计）；
- C. reader 重整为「读身份+读审计」：补 SELECT access_log，收回 0014 误授的
  INSERT access_log（写审计职责移交 writer）。

角色生命周期（#43 判例沿用）：downgrade 不 DROP 集群级角色——角色可能被同
集群其他库的 ACL 依赖引用；只精确回收本迁移授予的授权。

链序说明：down_revision 指 0028（main 现有最新）；编号取 0030（0029 留给并行
分支，golang-migrate 主源按版本号排序不受合入顺序影响）。
可逆性（make migrate-go-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CREATE_WRITER_ROLE_SQL = """
DO $$ BEGIN
    CREATE ROLE pii_vault_writer NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""


def upgrade() -> None:
    """A 建 writer 角色 → B writer 授权 → C reader 重整为读身份+读审计."""
    # ---- A. pii_vault_writer 写身份角色（幂等创建）----
    op.execute(_CREATE_WRITER_ROLE_SQL)

    # ---- B. writer 授权：写身份 + 写审计 ----
    op.execute("GRANT USAGE ON SCHEMA pii_vault TO pii_vault_writer")
    op.execute("GRANT INSERT ON pii_vault.student_identity TO pii_vault_writer")
    op.execute("GRANT INSERT ON pii_vault.access_log TO pii_vault_writer")

    # ---- C. reader 重整：读身份 + 读审计（收回写审计）----
    op.execute("GRANT SELECT ON pii_vault.access_log TO pii_vault_reader")
    op.execute("REVOKE INSERT ON pii_vault.access_log FROM pii_vault_reader")


def downgrade() -> None:
    """成对回滚到 0014 授权形态；writer 角色不 DROP（#43 判例），只回收授权."""
    # ---- 回收 B：writer 授权 ----
    op.execute("REVOKE INSERT ON pii_vault.access_log FROM pii_vault_writer")
    op.execute("REVOKE INSERT ON pii_vault.student_identity FROM pii_vault_writer")
    op.execute("REVOKE USAGE ON SCHEMA pii_vault FROM pii_vault_writer")

    # ---- 回滚 C：reader 恢复 0014 形态（INSERT 归还、SELECT 收回）----
    op.execute("GRANT INSERT ON pii_vault.access_log TO pii_vault_reader")
    op.execute("REVOKE SELECT ON pii_vault.access_log FROM pii_vault_reader")
