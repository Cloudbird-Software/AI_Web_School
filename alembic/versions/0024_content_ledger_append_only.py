"""T-W5-001 内容版本账 append-only 物理强制补齐（W5-R Go 重锚定，镜像 db/migrations/0024_*.sql）.

D1「内容版本账只增不改」（宪法第二部分：Item/Material/Corpus 全版本化）此前
在 DB 层完全没有物理强制：内容四表的 UPDATE/DELETE 直接成功。本迁移给四张
内容版本账表补挂 BEFORE UPDATE OR DELETE FOR EACH STATEMENT 触发器，
复用 0005 定义的 raise_append_only_error()，零新函数。

覆盖清单与逐表判定理由（代码实证：全仓 grep 无任何针对这些表的 UPDATE/DELETE
路径；全部写路径为 INSERT，status/门字段在 INSERT 时一次定型）：

- item_template_version（0002）：母题模板版本行，一 row = 一版快照；
  src/core/models/item_template_version.py 明示「D1：永不 UPDATE/DELETE」。
- material_version（0002）：素材版本行，content_ref 内容寻址，同内容必同 id，
  改内容 = 新 INSERT 新 id（D3），无任何合法改行路径。
- corpus_version（0002）：语料版本行，门字段与 material_version 对齐
  （0005 补列）；publish_corpus_asset 走 INSERT 定型，两段式中资产身份另立表。
- passage（0020）：审阅扩盖——writer.py 明示「语篇身份即版本，每次改写 =
  新行新 passage_id，D1 只增不改」，content_hash 寻址每行即一个不可变内容
  快照，属内容版本账性质且同样零 UPDATE/DELETE 路径。

审阅确认不覆盖的近邻表（防整表放行误判，逐表理由）：

- item_version：契约 §4 明示合法的受控状态机前移（status/gate_certificate_id/
  published_at/retired_at，draft→quarantined→published→retired 无回边，
  见 src/core/content/publication.py「为什么允许 UPDATE item_version.status」）；
  历史留痕由独立 append-only 表承载（publication 签发账、0018
  item_lifecycle_transition），六大内容块的不可变由 D3 换行新 id + DB 角色权限
  兜底。整表触发器会掐断合法签发流，故按任务卡范围排除。
- item_template / item / material / corpus_asset：指针身份表，
  current_version_id 前移是契约合法 UPDATE（0002 AFTER INSERT 触发器亦依赖）。
- material_license：license decision（approved/rejected/expired）生命周期可变。
- relation_type / kp_node / kp_edge / graph_release（0006/0007）：知识图谱走
  时间性生效失效与状态前移（kp_node UPDATE status='superseded' 等），非
  版本账本体。
- kp_closure（0007）：派生闭包缓存，release 内重建 = DELETE+重插
  （core/knowledge/closure.py），正确性由可重建性保证（同 review_queue_entry
  的裁决逻辑）。
- estimator_run / score_run 等其余运行账：非本卡「内容版本账」域，不越界。

豁免说明：覆盖的四表均不存在合法受控字段更新，无需豁免分支；受控指针更新
（current_version_id）已由结构性设计隔离在身份表中。

迁移可逆性（SQL-1）：downgrade 精确 DROP 四触发器（IF EXISTS 成对语义）；
不触碰 raise_append_only_error 函数体（禁 CREATE OR REPLACE 重定义——down 后
函数体≠目标版本会破坏全量可逆演练）。行为验收由 make migrate-go-check 承担：
三探针（UPDATE / UPDATE WHERE FALSE / DELETE）对四表全部被拒 + down -1 后
UPDATE 放行。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 触发器 DDL：四个语句级触发器共用 raise_append_only_error()；语句文本与
# db/migrations/0024_content_ledger_append_only.up.sql 一致（migrate-go-check
# parity 按 pg_dump 归一化语句多重集比较）。
_COVERED_TABLES = (
    "item_template_version",
    "material_version",
    "corpus_version",
    "passage",
)

_CREATE_TRIGGER_SQL = [
    f"""CREATE TRIGGER trg_{t}_append_only
    BEFORE UPDATE OR DELETE ON {t}
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();"""
    for t in _COVERED_TABLES
]

_DROP_TRIGGER_SQL = [
    f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}" for t in _COVERED_TABLES
]


def upgrade() -> None:
    """给内容版本账四表挂 append-only 语句级触发器（复用既有函数，零新函数）."""
    for sql in _CREATE_TRIGGER_SQL:
        op.execute(sql)


def downgrade() -> None:
    """成对回滚：精确 DROP 四个内容版本账触发器."""
    for sql in _DROP_TRIGGER_SQL:
        op.execute(sql)
