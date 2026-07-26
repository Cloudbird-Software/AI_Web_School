"""核心域 ORM 模型包（T-W1-003）.

九实体 SQLAlchemy ORM + Pydantic schema + 内容寻址纯函数：
- Item / ItemVersion（统一内容模型核心）
- ItemTemplate / ItemTemplateVersion（A/B 级母题）
- Material / MaterialVersion（素材：身份+版本两段式，D1 全版本化）
- ItemGroup（题组/testlet，≤6 题，R-Z-06）
- CorpusAsset / CorpusVersion（语料库：身份+版本两段式）

内容寻址（§3 公式一/二/三）：compute_instance_id / compute_canonical_item_version_id
/ compute_material_version_id —— 宪法 D3 确定性要求。

宪法 A5/A7：本包禁止 import 任何学科包/学段包（核心域零学科特判）；
仅复用 registries/* 与 src/core/* 内部模块。tests/unit/test_orm_models.py
含静态扫描测试 test_no_subject_pack_imports_in_models 兜底此约束。
"""
from __future__ import annotations

# 内容寻址纯函数（§3 公式一/二/三）
from src.core.models.content_addressing import (
    compute_canonical_item_version_id,
    compute_instance_id,
    compute_material_version_id,
)

# 共享 Base 与 PG ENUM 类型
from src.core.models._base import (
    Base,
    item_tier_enum,
    item_template_version_status_enum,
    item_version_status_enum,
    material_kind_enum,
    material_license_decision_enum,
)

# 九实体 ORM + Pydantic schema
from src.core.models.item import Item, ItemPydantic
from src.core.models.item_version import (
    Content,
    CorpusRef,
    ErrorBindings,
    InteractionRef,
    ItemVersion,
    ItemVersionPydantic,
    KpRef,
    Lineage,
    Objective,
    Pipeline,
    ScoringRef,
    StepRef,
)
from src.core.models.item_template import ItemTemplate, ItemTemplatePydantic
from src.core.models.item_template_version import (
    ItemTemplateVersion,
    ItemTemplateVersionPydantic,
)
from src.core.models.material import Material, MaterialPydantic
from src.core.models.material_license import (
    MaterialLicense,
    MaterialLicensePydantic,
)
from src.core.models.material_version import (
    MaterialVersion,
    MaterialVersionPydantic,
)
from src.core.models.item_group import ItemGroup, ItemGroupPydantic
from src.core.models.corpus_asset import CorpusAsset, CorpusAssetPydantic
from src.core.models.corpus_version import CorpusVersion, CorpusVersionPydantic

__all__ = [
    # Base + ENUM
    "Base",
    "item_tier_enum",
    "item_version_status_enum",
    "item_template_version_status_enum",
    "material_kind_enum",
    "material_license_decision_enum",
    # 九实体 ORM
    "Item",
    "ItemVersion",
    "ItemTemplate",
    "ItemTemplateVersion",
    "Material",
    "MaterialVersion",
    "ItemGroup",
    "CorpusAsset",
    "CorpusVersion",
    # 支撑表 ORM（material_version/corpus_version FK 依赖）
    "MaterialLicense",
    # Pydantic schema
    "ItemPydantic",
    "ItemVersionPydantic",
    "ItemTemplatePydantic",
    "ItemTemplateVersionPydantic",
    "MaterialPydantic",
    "MaterialVersionPydantic",
    "ItemGroupPydantic",
    "CorpusAssetPydantic",
    "CorpusVersionPydantic",
    "MaterialLicensePydantic",
    # 六大块 Pydantic 子模型
    "Objective",
    "KpRef",
    "StepRef",
    "InteractionRef",
    "Content",
    "ScoringRef",
    "ErrorBindings",
    "Lineage",
    "Pipeline",
    "CorpusRef",
    # 内容寻址
    "compute_instance_id",
    "compute_canonical_item_version_id",
    "compute_material_version_id",
]
