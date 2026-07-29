"""T-W2-016 语料库加载器：函数库 YAML → corpus_asset + corpus_version.

地位：本模块是 src/packs/subject-math/corpora/functions.yaml 等
学科包语料库 YAML 的统一加载入口（不写死学科特判，仅消费 YAML 元数据）。

契约对齐（specs/contracts/db/item-model.md §2.5 / §4）：
- 语料库 = corpus_asset（不变身份） + corpus_version（不可变内容快照）
- corpus_version 与 material_version 门字段对齐：status / gate_certificate_id /
  published_at / retired_at 均强制校验（status='published' 必须有 gate_certificate_id）
- license_id 必须在 content/sources/registry.yaml 且 approved（宪法 R-Q-18）

函数白名单：返回 {name: FunctionSignature} dict 供 expr_eval 注册；
不安全函数（pure/no_io/deterministic 任一为 false）一律排除并记录原因。

宪法 A5/A7：本模块不 import 任何学科包/学段包（仅复用 SourceRegistry）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import ulid
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.content.source_registry import SourceRegistry
from src.core.models.corpus_asset import CorpusAsset
from src.core.models.corpus_version import CorpusVersion
from src.core.models.material_license import MaterialLicense


# ────────────────────────────────────────────────────────────────────
# 默认路径
# ────────────────────────────────────────────────────────────────────
DEFAULT_FUNCTIONS_YAML: Path = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "packs"
    / "subject-math"
    / "corpora"
    / "functions.yaml"
)


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class CorpusLoaderError(ValueError):
    """语料库加载/校验失败基类."""


class LicenseNotApprovedError(CorpusLoaderError):
    """license_id 未登记或未 approved（宪法 R-Q-18）."""


class GateEnforcementError(CorpusLoaderError):
    """门强制失败：published 状态未提供合法 gate_certificate_id."""


# ────────────────────────────────────────────────────────────────────
# Pydantic schema
# ────────────────────────────────────────────────────────────────────

TypeT = Literal[
    "integer", "number", "string", "boolean",
    "array", "object",
]


class ParamSpec(BaseModel):
    """单个函数参数规格."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    type: TypeT
    min: Optional[float] = None
    max: Optional[float] = None
    nonzero: Optional[bool] = None
    fields: Optional[dict[str, str]] = None  # type=object 时的字段签名（简化：类型名→类型名）
    items: Optional[dict[str, Any]] = None   # type=array 时的元素规格


class ReturnSpec(BaseModel):
    """函数返回值规格.

    允许两种形式：
    1. 简单 type 形式：return: integer / return: {type: integer}
    2. 对象形式：return: {type: object, fields: {...}}
    """
    model_config = ConfigDict(extra="forbid")

    type: TypeT
    fields: Optional[dict[str, str]] = None
    items: Optional[dict[str, Any]] = None

    @classmethod
    def coerce(cls, v: Any) -> "ReturnSpec":
        """从 YAML 兼容多种写法：str / dict."""
        if isinstance(v, str):
            return cls(type=v)  # type: ignore[arg-type]
        if isinstance(v, dict):
            return cls(**v)
        raise ValueError(f"return 字段必须是 str 或 dict，实际 {type(v)}")


class SafetySpec(BaseModel):
    """函数安全契约."""
    model_config = ConfigDict(extra="forbid")

    pure: bool = Field(..., description="无副作用")
    no_io: bool = Field(..., description="不读不写外部 IO")
    deterministic: bool = Field(..., description="同输入同输出")
    no_loops: bool = Field(..., description="无显式循环/递归")


class FunctionDef(BaseModel):
    """单个函数定义（签名+安全标记，不含实现）."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    signature: dict[str, Any]
    safety: SafetySpec
    description: str = Field(..., min_length=1)

    # 派生字段：解析后的签名（property，便于注册）
    @property
    def params(self) -> list[ParamSpec]:
        raw_params = self.signature.get("params", [])
        return [ParamSpec(**p) for p in raw_params]

    @property
    def return_spec(self) -> ReturnSpec:
        return ReturnSpec.coerce(self.signature.get("return"))

    @field_validator("name")
    @classmethod
    def _name_pattern(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError(f"函数名非法: {v!r}（仅允许字母数字与下划线）")
        return v

    @field_validator("version")
    @classmethod
    def _version_pattern(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"version 应为 semver，实际 {v!r}")
        return v


class FunctionLibrary(BaseModel):
    """数学函数库语料（functions.yaml 的顶层 schema）."""
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(..., min_length=1)
    pack_id: str = Field(..., min_length=1)
    kind: Literal["function_lib"]
    license_id: str = Field(..., min_length=1)
    library_version: str = Field(..., min_length=1)
    functions: list[FunctionDef] = Field(..., min_length=1)

    @field_validator("library_version")
    @classmethod
    def _library_version_pattern(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"library_version 应为 semver，实际 {v!r}")
        return v


# ────────────────────────────────────────────────────────────────────
# 加载与校验
# ────────────────────────────────────────────────────────────────────

def parse_library(path: Optional[Path] = None) -> FunctionLibrary:
    """加载并校验函数库 YAML（不写 DB）.

    Args:
        path: YAML 路径；None 用 DEFAULT_FUNCTIONS_YAML。

    Returns:
        FunctionLibrary 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        pydantic.ValidationError: schema 校验失败。
        CorpusLoaderError: 函数名重复。
    """
    if path is None:
        path = DEFAULT_FUNCTIONS_YAML
    if not path.is_file():
        raise FileNotFoundError(f"函数库 YAML 不存在: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CorpusLoaderError(f"YAML 顶层必须是 mapping，实际 {type(raw)}")

    lib = FunctionLibrary(**raw)

    # 函数名唯一性
    names: list[str] = [f.name for f in lib.functions]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise CorpusLoaderError(f"函数名重复: {sorted(dupes)}")

    return lib


def check_license(
    library: FunctionLibrary,
    registry: Optional[SourceRegistry] = None,
) -> SourceRecord:
    """校验 license_id 在登记表中且 approved（宪法 R-Q-18）.

    Returns:
        SourceRecord：当前 license 的完整记录（用于 DB 同步）。

    Raises:
        LicenseNotApprovedError: 未登记 / 未 approved / 已过期。
    """
    if registry is None:
        registry = SourceRegistry.from_yaml()

    lid = library.license_id
    rec = registry.get_license(lid)
    if rec is None:
        raise LicenseNotApprovedError(
            f"license_id={lid!r} 未在 content/sources/registry.yaml 登记"
        )
    if not registry.is_approved(lid):
        raise LicenseNotApprovedError(
            f"license_id={lid!r} 不可用（decision={rec.decision!r} 或已过期）"
        )
    return rec


async def sync_license_to_db(
    license_record: "SourceRecord",
    db: AsyncSession,
) -> None:
    """将 registry.yaml 中的 license 同步到 material_license 表（幂等）.

    宪法 R-Q-18：material_version.license_id 与 corpus_version.license_id 的 FK
    目标是 material_license 表。YAML registry 是真源；DB 是运行时快照。
    本函数在入库 corpus_version 前确保 license 行已存在（ON CONFLICT DO NOTHING）。

    为什么用 ON CONFLICT DO NOTHING 而非 UPDATE：D1 三本账只增不改——
    license 决策变更应通过新增审计行（未来表扩展）而非 UPDATE 历史行。
    """
    stmt = pg_insert(MaterialLicense).values(
        license_id=license_record.license_id,
        source=license_record.source,
        rights_holder=license_record.rights_holder,
        scope=license_record.scope,
        expires_at=license_record.expires_at,
        decision=license_record.decision,
    ).on_conflict_do_nothing(index_elements=["license_id"])
    await db.execute(stmt)


def function_whitelist(
    library: FunctionLibrary,
    strict_safety: bool = True,
) -> dict[str, FunctionDef]:
    """返回安全函数白名单，供 expr_eval 注册.

    Args:
        library: 已加载的函数库。
        strict_safety: True=违反安全契约的函数被排除；False=全部允许（仅用于诊断）。

    Returns:
        {function_name: FunctionDef} 字典。
    """
    whitelist: dict[str, FunctionDef] = {}
    for fn in library.functions:
        if strict_safety:
            s = fn.safety
            if not (s.pure and s.no_io and s.deterministic):
                # 安全契约违反 → 不进白名单（调用方应记录原因）
                continue
        whitelist[fn.name] = fn
    return whitelist


def compute_corpus_version_id(library: FunctionLibrary, path: Path) -> str:
    """计算 corpus_version.version_id（内容寻址 digest）.

    契约 §3 公式三：corpus_version_id = H(content_digest)。
    content_digest 取 YAML 文件本身的字节级 SHA-256，保证同内容同 id（D3）。
    """
    raw_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return "sha256:" + hashlib.sha256(raw_digest.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────
# 入库（corpus_asset + corpus_version 两段式）
# ────────────────────────────────────────────────────────────────────

async def publish_corpus_library(
    yaml_path: Optional[Path] = None,
    db: Optional[AsyncSession] = None,
    gate_certificate_id: Optional[str] = None,
    status: str = "draft",
    registry: Optional[SourceRegistry] = None,
) -> dict[str, Any]:
    """加载 YAML → 校验 license → 入库 corpus_asset + corpus_version.

    Args:
        yaml_path: 函数库 YAML；None 用默认路径。
        db: AsyncSession（必填）。
        gate_certificate_id: 门证书；status='published' 时必填。
        status: draft / quarantined / published / retired。
        registry: 可选的预加载 SourceRegistry；None 时现加载。

    Returns:
        {"asset_id": ..., "version_id": ..., "function_count": ...,
         "whitelist_count": ...}

    Raises:
        ValueError: db 未提供。
        LicenseNotApprovedError: license 未登记或不可用。
        GateEnforcementError: published 缺 gate_certificate_id。
    """
    if db is None:
        raise ValueError("db (AsyncSession) 必填")

    # ── 门强制：published 必须有 gate_certificate_id ──
    if status == "published" and not gate_certificate_id:
        raise GateEnforcementError(
            "门强制失败：status='published' 必须提供合法 gate_certificate_id"
            "（契约 §4 规则 1 / §2.5）"
        )

    if yaml_path is None:
        yaml_path = DEFAULT_FUNCTIONS_YAML

    # ── 加载并校验 YAML ──
    library = parse_library(yaml_path)

    # ── 校验 license 并同步到 DB（确保 FK 满足） ──
    license_record = check_license(library, registry=registry)
    await sync_license_to_db(license_record, db)
    await db.flush()

    # ── 计算 corpus_version_id（内容寻址） ──
    version_id = compute_corpus_version_id(library, yaml_path)

    # ── 创建 corpus_asset 身份 ──
    asset_id = "cul-" + str(ulid.new())
    asset = CorpusAsset(
        asset_id=asset_id,
        kind=library.kind,
        pack_id=library.pack_id,
    )
    db.add(asset)
    await db.flush()

    # ── 构造 lineage（架构 v2 §4.1：函数库的 pipeline 信息） ──
    now = datetime.now(timezone.utc)
    lineage: dict[str, Any] = {
        "tier": "B",
        "pipeline": {
            "id": f"{library.pack_id}.function_lib",
            "version": library.library_version,
        },
        "signed_by": "corpus_loader",
        "signed_at": now.isoformat(),
        "source": {
            "kind": "function_lib",
            "yaml_path": str(yaml_path),
            "schema_version": library.schema_version,
        },
    }

    cv_kwargs: dict[str, Any] = {
        "version_id": version_id,
        "asset_id": asset_id,
        "content_ref": f"yaml:{yaml_path.name}#{version_id}",
        "license_id": library.license_id,
        "lineage": lineage,
        "status": status,
    }

    if status == "published":
        cv_kwargs["gate_certificate_id"] = gate_certificate_id
        cv_kwargs["published_at"] = now

    cv = CorpusVersion(**cv_kwargs)
    db.add(cv)
    await db.commit()

    # ── 返回白名单统计 ──
    whitelist = function_whitelist(library, strict_safety=True)

    return {
        "asset_id": asset_id,
        "version_id": version_id,
        "function_count": len(library.functions),
        "whitelist_count": len(whitelist),
    }


__all__ = [
    "DEFAULT_FUNCTIONS_YAML",
    "CorpusLoaderError",
    "GateEnforcementError",
    "LicenseNotApprovedError",
    "FunctionDef",
    "FunctionLibrary",
    "ParamSpec",
    "ReturnSpec",
    "SafetySpec",
    "TypeT",
    "check_license",
    "compute_corpus_version_id",
    "function_whitelist",
    "parse_library",
    "publish_corpus_library",
    "sync_license_to_db",
]
