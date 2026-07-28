"""T-W4-002 ActiveModelPointer：估计器版本切换机制（架构 v2 §4.7 / 宪法 D6）.

ActiveModelPointer 在 estimator_run 表上登记每场景当前活跃的估计器版本，
并按 timestamp 回溯「当时活跃版本」——历史报告永远引用当时版本
（D6 估计器可替换、可重放）。

核心接口：
- set_active(purpose_scope, model_version, *, code_digest,
  input_snapshot_id, graph_release_id)：登记当前活跃版本；旧活跃版本打
  retired_at 退役时间戳。每场景同一时刻至多一个活跃版本（偏唯一索引强制）。
- get_active(purpose_scope, timestamp=None)：取该场景当前（或 timestamp
  当时）活跃的 EstimatorRun；无则返回 None。
- get_params(item_id, purpose_scope, *, timestamp=None)：按 timestamp 回溯
  当时活跃 model_version，查 item_param 返回该版本对该题的参数；
  无 timestamp 返回当前活跃版本的参数。

为什么 get_params 必须带 purpose_scope（D5）：item_param 按 source（先验/实测）
与 purpose_scope（practice/diagnosis/measurement）分开存储、分开估计，禁止跨场景
混估——取参数必须指明场景，不存在「跨场景聚合参数」的查询路径。

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import ulid
from sqlalchemy import or_ as sa_or, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.estimator_run import EstimatorRun

# 场景三值域（与 ctt.VALID_PURPOSE_SCOPES / D5 对齐）
VALID_PURPOSE_SCOPES: frozenset[str] = frozenset(
    {"practice", "diagnosis", "measurement"}
)


@dataclass(frozen=True)
class ParamSnapshot:
    """get_params 返回的参数快照（D6：历史报告引用当时版本的实证）."""

    item_version_id: str
    purpose_scope: str
    model_version: str
    params: dict[str, Any]
    sample_size: int
    as_of: datetime


class ActiveModelPointer:
    """估计器版本切换服务（持久化于 estimator_run 表）.

    每次 set_active 登记一个新活跃版本：旧活跃行 UPDATE retired_at=now，
    新行 INSERT(retired_at=NULL)。偏唯一索引保证每场景至多一个活跃版本。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── set_active ────────────────────────────────────────────────

    async def set_active(
        self,
        purpose_scope: str,
        model_version: str,
        *,
        code_digest: str,
        input_snapshot_id: str,
        graph_release_id: str,
        activated_at: Optional[datetime] = None,
    ) -> EstimatorRun:
        """登记 purpose_scope 下当前活跃的估计器版本.

        Args:
            purpose_scope: 场景（practice/diagnosis/measurement，D5）。
            model_version: 估计器版本（如 'ctt-v1'/'rasch-v1'，D6 可替换）。
            code_digest: 估计器代码 SHA256。
            input_snapshot_id: 输入数据快照标识（as_of + 数据指纹）。
            graph_release_id: 估计时所用图谱 release 标识。
            activated_at: 登记时刻（默认 now）；可回填历史登记。

        Returns:
            新登记的活跃 EstimatorRun 行。

        Side effects:
            旧活跃行（同 scope, retired_at IS NULL）retired_at 被打戳为
            activated_at（先于新行 INSERT，保证偏唯一索引不冲突）。
        """
        if purpose_scope not in VALID_PURPOSE_SCOPES:
            raise ValueError(
                f"purpose_scope 越域：{purpose_scope!r}"
                f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
            )
        ts = activated_at or datetime.now()

        # 1) 退役旧活跃版本（同 scope 当前 retired_at IS NULL 的行）
        await self._db.execute(
            text(
                "UPDATE estimator_run SET retired_at = :ts"
                " WHERE purpose_scope = :scope AND retired_at IS NULL"
            ),
            {"ts": ts, "scope": purpose_scope},
        )
        # 2) 登记新活跃版本
        run = EstimatorRun(
            run_id="run_" + str(ulid.new()),
            purpose_scope=purpose_scope,
            model_version=model_version,
            code_digest=code_digest,
            input_snapshot_id=input_snapshot_id,
            graph_release_id=graph_release_id,
            activated_at=ts,
            retired_at=None,
        )
        self._db.add(run)
        await self._db.commit()
        return run

    # ── get_active ────────────────────────────────────────────────

    async def get_active(
        self, purpose_scope: str, timestamp: Optional[datetime] = None
    ) -> Optional[EstimatorRun]:
        """取该场景当前（或 timestamp 当时）活跃的估计器版本.

        timestamp=None：返回 retired_at IS NULL 的当前活跃行。
        给定 timestamp：返回 activated_at <= timestamp 且（retired_at IS NULL
        或 retired_at > timestamp）的最新行——即 timestamp 当时正活跃的版本。
        """
        if timestamp is None:
            stmt = (
                select(EstimatorRun)
                .where(
                    EstimatorRun.purpose_scope == purpose_scope,
                    EstimatorRun.retired_at.is_(None),
                )
                .order_by(EstimatorRun.activated_at.desc())
                .limit(1)
            )
        else:
            stmt = (
                select(EstimatorRun)
                .where(
                    EstimatorRun.purpose_scope == purpose_scope,
                    EstimatorRun.activated_at <= timestamp,
                    # 该版本在 timestamp 时仍活跃（未退役或退役晚于 timestamp）
                    sa_or(
                        EstimatorRun.retired_at.is_(None),
                        EstimatorRun.retired_at > timestamp,
                    ),
                )
                .order_by(EstimatorRun.activated_at.desc())
                .limit(1)
            )
        res = await self._db.execute(stmt)
        return res.scalar_one_or_none()

    # ── get_params ────────────────────────────────────────────────

    async def get_params(
        self,
        item_id: str,
        purpose_scope: str,
        *,
        timestamp: Optional[datetime] = None,
    ) -> Optional[ParamSnapshot]:
        """按 timestamp 回溯当时活跃版本，返回该版本对该题的参数.

        Args:
            item_id: item_version_id（参数挂在不可变版本上，D3）。
            purpose_scope: 场景（D5 必填——参数分场景存储，禁止跨场景取）。
            timestamp: 报告时刻；None=当前活跃版本。历史报告引用当时版本实证。

        Returns:
            ParamSnapshot（params + sample_size + as_of + model_version）；
            若该版本对该题无参数行（未估计过）则返回 None。
        """
        if purpose_scope not in VALID_PURPOSE_SCOPES:
            raise ValueError(
                f"purpose_scope 越域：{purpose_scope!r}"
                f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
            )
        run = await self.get_active(purpose_scope, timestamp)
        if run is None:
            return None
        # item_param.method_version 与 estimator_run.model_version 对齐：
        # 估计器运行产出 item_param 行时，method_version 即登记的 model_version。
        row = (
            await self._db.execute(
                text(
                    "SELECT params, sample_size, as_of FROM item_param"
                    " WHERE item_version_id = :iid"
                    "   AND purpose_scope = :scope"
                    "   AND method_version = :mv"
                    " ORDER BY as_of DESC LIMIT 1"
                ),
                {"iid": item_id, "scope": purpose_scope, "mv": run.model_version},
            )
        ).first()
        if row is None:
            return None
        return ParamSnapshot(
            item_version_id=item_id,
            purpose_scope=purpose_scope,
            model_version=run.model_version,
            params=row.params,
            sample_size=row.sample_size,
            as_of=row.as_of,
        )


__all__ = [
    "VALID_PURPOSE_SCOPES",
    "ParamSnapshot",
    "ActiveModelPointer",
]
