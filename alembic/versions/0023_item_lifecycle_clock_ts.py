"""item_lifecycle_transition.created_at 默认值 now() → clock_timestamp()（nightly #58 根因修复）.

根因（2026-08-24 nightly 失败证据：tests/unit/test_item_health.py::
test_active_pool_excludes_retired，RETIRED 的 item 出现在活跃池）：

- ``now()`` 返回事务开始时间（transaction-stable），同一事务内的所有 INSERT
  得到**完全相同**的 created_at。生产路径按铁律 9「一次业务写入=一个事务」，
  同事务内多次状态变更必然命中；测试隔离（conftest 事务回滚模式）同样命中。
- 「当前状态 = 最新 transition」的排序 ``ORDER BY created_at DESC,
  transition_id DESC`` 因此退化为纯 ULID tiebreak；ulid-py 的 ``ulid.new()``
  同一毫秒内的随机部分与生成顺序无关，~50% 字典序倒挂 → 取到旧行。

修复：默认值改为 ``clock_timestamp()``（语句级真实时钟）。PostgreSQL 保证
同一事务内先后语句的 clock_timestamp() 严格递增（微秒分辨率），排序恢复
与插入顺序一致，无需改任何查询 SQL、不改历史行（D1 只增不改不受影响——
默认值仅作用于未来 INSERT）。

可逆性（make migrate-check：upgrade→downgrade→upgrade）：downgrade 恢复
now() 默认值，历史行两方向都不被触碰。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE item_lifecycle_transition "
        "ALTER COLUMN created_at SET DEFAULT clock_timestamp()"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE item_lifecycle_transition "
        "ALTER COLUMN created_at SET DEFAULT now()"
    )
