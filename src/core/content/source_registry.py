"""T-W2-015 内容来源登记表加载器与查询服务.

宪法 R-Q-18：素材/语料库入库必须持 approved 状态的许可；
无登记或 decision!=approved 的来源不得入库（CI 拦截：tools/ci/check_sources.py）。

本模块是 content/sources/registry.yaml 的唯一加载入口，提供：
- SourceRecord Pydantic 模型（字段对齐 material_license 表 + YAML 元数据）
- SourceRegistry 加载与查询（get_license / is_approved / all_approved）

为什么不直接读 DB：YAML 是人类可读的真源，DB material_license 是运行时
快照（迁移/种子导入）。CI 在导入前需先校验 YAML；运行时也可查询 YAML
作为快速判断（DB 查询属生产路径）。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ────────────────────────────────────────────────────────────────────
# 默认路径
# ────────────────────────────────────────────────────────────────────
DEFAULT_REGISTRY_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "content"
    / "sources"
    / "registry.yaml"
)


# ────────────────────────────────────────────────────────────────────
# Pydantic 模型
# ────────────────────────────────────────────────────────────────────

DecisionT = Literal["approved", "rejected", "expired"]


class SourceRecord(BaseModel):
    """单条来源登记记录.

    字段对齐 src/core/models/material_license.py（license_id/source/rights_holder/
    scope/expires_at/decision），新增 YAML 元数据字段（kind/notes/registered_at）。
    """

    model_config = ConfigDict(extra="forbid")

    license_id: str = Field(..., min_length=1, description="唯一标识")
    source: str = Field(..., min_length=1, description="来源名称")
    rights_holder: Optional[str] = Field(None, description="权利人")
    scope: Optional[str] = Field(None, description="授权范围")
    expires_at: Optional[datetime] = Field(None, description="期限；null=永久")
    decision: DecisionT = Field(..., description="approved/rejected/expired")
    # YAML 元数据（不进 DB，仅用于追溯）
    kind: Optional[str] = Field(None, description="来源类型")
    notes: Optional[str] = Field(None, description="备注")
    registered_at: Optional[datetime] = Field(None, description="登记日期")

    @field_validator("license_id", "source")
    @classmethod
    def _non_empty_str(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空字符串")
        return v


class RegistryFile(BaseModel):
    """registry.yaml 的顶层 schema. 只校验 records 段。"""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., min_length=1)
    records: list[SourceRecord] = Field(..., min_length=1)


# ────────────────────────────────────────────────────────────────────
# SourceRegistry
# ────────────────────────────────────────────────────────────────────


class SourceRegistry:
    """来源登记表加载与查询.

    用法:
        reg = SourceRegistry.from_yaml()  # 默认路径
        if reg.is_approved("lic-pypinyin-mit"):
            ...

    为什么 is_approved 同时校验 decision 与 expires_at：
    decision=approved 但 expires_at 已过期的来源按 R-Q-18 视为不可用。
    """

    def __init__(self, records: list[SourceRecord]):
        self._records: list[SourceRecord] = list(records)
        self._by_id: dict[str, SourceRecord] = {
            r.license_id: r for r in self._records
        }

    @classmethod
    def from_yaml(cls, path: Optional[Path] = None) -> "SourceRegistry":
        """从 registry.yaml 加载并校验.

        Args:
            path: YAML 路径；None 用 DEFAULT_REGISTRY_PATH。

        Returns:
            SourceRegistry 实例。

        Raises:
            FileNotFoundError: 文件不存在。
            pydantic.ValidationError: schema 校验失败。
            ValueError: license_id 重复。
        """
        if path is None:
            path = DEFAULT_REGISTRY_PATH
        if not path.is_file():
            raise FileNotFoundError(f"来源登记表不存在: {path}")

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"registry.yaml 顶层必须是 mapping，实际 {type(raw)}")

        parsed = RegistryFile(**raw)

        # 重复 license_id 检测
        seen: set[str] = set()
        for r in parsed.records:
            if r.license_id in seen:
                raise ValueError(f"重复 license_id: {r.license_id}")
            seen.add(r.license_id)

        return cls(parsed.records)

    # ── 查询接口 ──

    def get_license(self, license_id: str) -> Optional[SourceRecord]:
        """按 license_id 查询；未登记返回 None."""
        return self._by_id.get(license_id)

    def is_approved(self, license_id: str) -> bool:
        """是否可用于入库.

        规则：
        - 未登记 → False
        - decision != 'approved' → False
        - expires_at 非空且已过期 → False
        - 其他 → True
        """
        rec = self.get_license(license_id)
        if rec is None:
            return False
        if rec.decision != "approved":
            return False
        if rec.expires_at is not None:
            now = datetime.now(timezone.utc)
            # expires_at 可能 naive（YAML 无时区）——补 UTC 假定
            exp = rec.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                return False
        return True

    def all_approved(self) -> list[SourceRecord]:
        """返回所有当前可用的来源（is_approved=True 的子集）."""
        return [r for r in self._records if self.is_approved(r.license_id)]

    def all_records(self) -> list[SourceRecord]:
        """返回全部记录（不论 decision）."""
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, license_id: object) -> bool:
        return isinstance(license_id, str) and license_id in self._by_id


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "DecisionT",
    "RegistryFile",
    "SourceRecord",
    "SourceRegistry",
]
