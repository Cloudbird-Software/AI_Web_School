"""W0-1: item_version 导入契约校验测试.

验收标准（Issue #20）：
1. specs/item_version_import_schema.json 存在并能正确校验 3 个示例 JSON.
2. specs/pydantic_item_version.py 提供清晰的映射，Pydantic 校验等价于 JSON Schema.
3. 最小语义校验（interaction/scorer 在注册表存在、兼容）.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = PROJECT_ROOT / "specs"
EXAMPLES_DIR = SPECS_DIR / "examples"

SCHEMA_PATH = SPECS_DIR / "item_version_import_schema.json"
PYDANTIC_PATH = SPECS_DIR / "pydantic_item_version.py"

EXAMPLE_FILES = [
    "math_item_example.json",
    "chinese_item_example.json",
    "english_item_example.json",
]


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def import_schema() -> dict:
    """加载 item_version_import_schema.json."""
    assert SCHEMA_PATH.exists(), f"Schema file missing: {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=EXAMPLE_FILES)
def example_data(request) -> tuple[str, dict]:
    """参数化：加载 3 个学科示例，每个返回 (filename, parsed_json)."""
    path = EXAMPLES_DIR / request.param
    assert path.exists(), f"Example file missing: {path}"
    return request.param, json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def item_version_import_cls():
    """动态加载 specs.pydantic_item_version.ItemVersionImport.

    为什么用动态 import：specs/ 可能不在 PYTHONPATH；
    用 sys.path 插入项目根后 import，兼容本地与 CI 环境。
    """
    import sys

    assert PYDANTIC_PATH.exists(), f"Pydantic file missing: {PYDANTIC_PATH}"
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from specs.pydantic_item_version import ItemVersionImport

    return ItemVersionImport


# ────────────────────────────────────────────────────────────────────
# 1. 文件存在性（最基础的检查）
# ────────────────────────────────────────────────────────────────────


class TestFilesExist:
    """Issue #20 交付物文件存在性检查."""

    def test_schema_exists(self):
        assert SCHEMA_PATH.is_file()

    def test_pydantic_exists(self):
        assert PYDANTIC_PATH.is_file()

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_example_exists(self, fname):
        assert (EXAMPLES_DIR / fname).is_file()

    def test_readme_exists(self):
        """specs/README.md 记录导入契约."""
        assert (SPECS_DIR / "README.md").is_file()


# ────────────────────────────────────────────────────────────────────
# 2. JSON Schema 校验（3 个示例）
# ────────────────────────────────────────────────────────────────────


class TestJsonSchemaValidation:
    """3 个示例 JSON 通过 item_version_import_schema.json 校验."""

    def test_schema_is_valid_draft7(self, import_schema):
        """Schema 本身必须是合法的 Draft-07 JSON Schema."""
        validator_cls = jsonschema.validators.validator_for(import_schema)
        validator_cls.check_schema(import_schema)  # 无异常 = schema 合法

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_example_passes_schema(self, import_schema, fname):
        """每个示例文件单独通过 JSON Schema 校验."""
        data = json.loads((EXAMPLES_DIR / fname).read_text(encoding="utf-8"))
        jsonschema.validate(instance=data, schema=import_schema)  # 无异常 = 通过

    def test_missing_required_fails(self, import_schema):
        """缺失必填顶层字段应抛出 ValidationError."""
        bad = json.loads(
            (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
        )
        del bad["item_version_id"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=import_schema)

    def test_invalid_status_enum_fails(self, import_schema):
        """非法 status enum 应失败."""
        bad = json.loads(
            (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
        )
        bad["status"] = "INVALID_STATUS_XYZ"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=import_schema)

    def test_invalid_cognitive_level_fails(self, import_schema):
        """非法 cognitive_level enum 应失败."""
        bad = json.loads(
            (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
        )
        bad["objective"]["cognitive_level"] = "wrong_level"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=import_schema)


# ────────────────────────────────────────────────────────────────────
# 3. Pydantic 模型校验（与 JSON Schema 等价）
# ────────────────────────────────────────────────────────────────────


class TestPydanticMapping:
    """specs/pydantic_item_version.py 提供清晰的 loader 集成映射."""

    def test_pydantic_imports_cleanly(self, item_version_import_cls):
        """模型能干净地 import 且非 None."""
        assert item_version_import_cls is not None
        # 检查必备子模型导出
        from specs.pydantic_item_version import (
            Objective,
            InteractionRef,
            Content,
            ScoringRef,
            ErrorBindings,
            Lineage,
            Pipeline,
        )
        for cls in (Objective, InteractionRef, Content, ScoringRef, ErrorBindings, Lineage, Pipeline):
            assert cls is not None

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_example_passes_pydantic(self, item_version_import_cls, fname):
        """3 个示例都通过 Pydantic 校验，字段可访问."""
        data = json.loads((EXAMPLES_DIR / fname).read_text(encoding="utf-8"))
        obj = item_version_import_cls.model_validate(data)
        # 基本字段可访问
        assert obj.item_version_id == data["item_version_id"]
        assert obj.item_id == data["item_id"]
        assert obj.status == data["status"]
        # 六大块字段类型正确
        assert obj.objective.kp_set  # 非空
        assert isinstance(obj.interaction_ref.interaction_id, str)
        assert isinstance(obj.content.blocks, list)
        assert isinstance(obj.scoring_ref.scorer_id, str)
        assert isinstance(obj.error_bindings.root, list)
        assert obj.lineage.pipeline.id  # 非空

    def test_pydantic_rejects_bad_cognitive_level(self, item_version_import_cls):
        """Pydantic 也拒绝非法 cognitive_level（与 JSON Schema 一致）."""
        from pydantic import ValidationError

        bad = json.loads(
            (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
        )
        bad["objective"]["cognitive_level"] = "wrong_level"
        with pytest.raises(ValidationError):
            item_version_import_cls.model_validate(bad)

    def test_pydantic_rejects_extra_fields(self, item_version_import_cls):
        """extra=forbid：额外顶层字段被拒绝."""
        from pydantic import ValidationError

        bad = json.loads(
            (EXAMPLES_DIR / "math_item_example.json").read_text(encoding="utf-8")
        )
        bad["__extra_bogus_field__"] = 123
        with pytest.raises(ValidationError):
            item_version_import_cls.model_validate(bad)


# ────────────────────────────────────────────────────────────────────
# 4. 注册表交叉引用（语义校验，Issue #20 语义检查）
# ────────────────────────────────────────────────────────────────────


class TestRegistryCrossReference:
    """示例中的 interaction_id / scorer_id 在注册表存在且互相兼容."""

    @pytest.fixture(scope="class")
    def registries(self):
        """加载 interaction 与 scorer 注册表（src/registry/loader.py）."""
        import sys

        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from src.registry.loader import (
            load_interaction_registry,
            load_scorer_registry,
            validate_cross_references,
        )

        ir = load_interaction_registry()
        sr = load_scorer_registry()
        validate_cross_references(ir, sr)  # 注册表自身先自检
        return ir, sr

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_interaction_id_registered(self, registries, fname):
        """示例的 interaction_id 在注册表中 status=active."""
        ir, _sr = registries
        data = json.loads((EXAMPLES_DIR / fname).read_text(encoding="utf-8"))
        iid = data["interaction_ref"]["interaction_id"]
        t = ir.get_interaction(iid)  # KeyError = 未注册
        assert t.status == "active", f"interaction {iid} 不是 active"

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_scorer_id_registered(self, registries, fname):
        """示例的 scorer_id 在注册表中 status=active."""
        _ir, sr = registries
        data = json.loads((EXAMPLES_DIR / fname).read_text(encoding="utf-8"))
        sid = data["scoring_ref"]["scorer_id"]
        s = sr.get_scorer(sid)  # KeyError = 未注册
        assert s.status == "active", f"scorer {sid} 不是 active"

    @pytest.mark.parametrize("fname", EXAMPLE_FILES)
    def test_scorer_compatible_with_interaction(self, registries, fname):
        """scorer_id ∈ interaction.compatible_scorers（兼容矩阵匹配）."""
        ir, _sr = registries
        data = json.loads((EXAMPLES_DIR / fname).read_text(encoding="utf-8"))
        iid = data["interaction_ref"]["interaction_id"]
        sid = data["scoring_ref"]["scorer_id"]
        t = ir.get_interaction(iid)
        assert sid in t.compatible_scorers, (
            f"scorer {sid} 不在 interaction {iid} 的 compatible_scorers 列表中: "
            f"{t.compatible_scorers}"
        )
