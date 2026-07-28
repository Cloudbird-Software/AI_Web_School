"""T-W4-021 D 线端到端流水线单元测试.

验收对照：
  §1 ``run_d_pipeline(blueprint_id, params)`` 返回入库后的 item_id 与门证书 id。
  §2 入库题目 scoring_ref 指向 ai_rubric，量规模板嵌入题目元数据（scorer_params）。
  §3 校验门验证量规模板完整性（维度齐全/分值合计正确/等级描述非空）。
  §4 ``make accept TASK=T-W4-021`` 全绿。
  §5 不 import 任何学科包/学段包（宪法 A5/X6）。

测试策略：
  - RubricCompletenessValidator 单元测试：直接构造合法/非法量规 dict，验证 pass/fail。
  - run_d_pipeline 端到端：注册蓝图 + 量规 + 模板 → 跑流水线 → 验 item_id/cert_id
    与 scoring_ref 契约。
  - 门失败路径：注入 fail mock 验证器 → 验证不发布、不签证书。
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.policy.loader import ChainEntry, GatePolicy, ValidatorStep
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
    reset_registry,
)
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)
from src.core.production.blueprint_schema import make_blueprint
from src.core.production.d_line_pipeline import (
    DPipelineResult,
    RubricCompletenessValidator,
    register_d_line_blueprint,
    reset_d_line_registry,
    run_d_pipeline,
)
from src.core.production.rubric_template import (
    RubricDimension,
    RubricLevel,
    RubricTemplate,
)

# ────────────────────────────────────────────────────────────────────
# 路径常量与模板加载
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMPOSITION_TEMPLATE_PATH = (
    _PROJECT_ROOT / "src" / "packs" / "subject-chinese" / "templates" / "composition.yaml"
)
_PICTURE_WRITING_TEMPLATE_PATH = (
    _PROJECT_ROOT / "src" / "packs" / "subject-chinese" / "templates" / "picture_writing.yaml"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


_COMPOSITION_TEMPLATE = _load_yaml(_COMPOSITION_TEMPLATE_PATH)
_PICTURE_WRITING_TEMPLATE = _load_yaml(_PICTURE_WRITING_TEMPLATE_PATH)

# D 线测试用学科包摘要（与 A 线黄金样例同形：sha256:...）
_PACK_DIGEST = "sha256:pack-subject-chinese-d-line-test"


# ────────────────────────────────────────────────────────────────────
# 量规/蓝图夹具
# ────────────────────────────────────────────────────────────────────


def _make_rubric(grade_band: str = "M") -> RubricTemplate:
    """构造一份四维合法量规（内容/结构/语言/书写，各 5 分，3 档）."""
    return RubricTemplate(
        rubric_id=f"sha256:test-d-line-rubric-{grade_band}-v1",
        name=f"D 线测试量规-{grade_band}段",
        grade_band=grade_band,  # type: ignore[arg-type]
        dimensions=[
            RubricDimension(
                id="content", name="内容", max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="主题明确，内容充实", score=5),
                    RubricLevel(level=2, label="合格", description="主题基本明确", score=3),
                    RubricLevel(level=3, label="待改进", description="主题模糊", score=1),
                ],
            ),
            RubricDimension(
                id="structure", name="结构", max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="段落清晰", score=5),
                    RubricLevel(level=2, label="合格", description="段落较清晰", score=3),
                    RubricLevel(level=3, label="待改进", description="段落混乱", score=1),
                ],
            ),
            RubricDimension(
                id="language", name="语言", max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="语言流畅", score=5),
                    RubricLevel(level=2, label="合格", description="语言较通顺", score=3),
                    RubricLevel(level=3, label="待改进", description="语言不畅", score=1),
                ],
            ),
            RubricDimension(
                id="handwriting", name="书写", max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="字迹工整", score=5),
                    RubricLevel(level=2, label="合格", description="字迹较工整", score=3),
                    RubricLevel(level=3, label="待改进", description="字迹潦草", score=1),
                ],
            ),
        ],
        total_max_score=20,
        version="1.0.0",
    )


def _make_composition_blueprint() -> Any:
    """构造一份作文命题蓝图（三学段默认参数化）."""
    return make_blueprint(
        blueprint_id="bp-d-line-composition-test",
        writing_type="composition",
        pack_id="subject-chinese",
        template_version_id=_COMPOSITION_TEMPLATE["template_version_id"],
        rubric_template_id="sha256:test-d-line-rubric-M-v1",
        topic_pool=["春天", "秋天", "成长"],
        time_limit_minutes=40,
    )


def _make_picture_writing_blueprint() -> Any:
    return make_blueprint(
        blueprint_id="bp-d-line-picture-test",
        writing_type="picture_writing",
        pack_id="subject-chinese",
        template_version_id=_PICTURE_WRITING_TEMPLATE["template_version_id"],
        rubric_template_id="sha256:test-d-line-rubric-L-v1",
        topic_pool=["公园", "操场"],
        time_limit_minutes=20,
    )


def _register_composition_blueprint(rubric: RubricTemplate | None = None) -> str:
    """注册作文蓝图到 D 线注册表，返回 blueprint_id."""
    bp = _make_composition_blueprint()
    register_d_line_blueprint(
        bp.blueprint_id,
        blueprint=bp,
        rubric=rubric or _make_rubric("M"),
        template_version=_COMPOSITION_TEMPLATE,
        pack_digest=_PACK_DIGEST,
    )
    return bp.blueprint_id


def _build_rubric_policy(
    *, pack_id: str = "platform", extra_failing: bool = False
) -> GatePolicy:
    """构造含 rubric_completeness 的门策略.

    extra_failing=True 时追加一个总是 fail 的 mock 验证器，用于测试门失败路径.
    """
    steps = [ValidatorStep(validator_id="rubric_completeness", blocking=True)]
    if extra_failing:
        steps.append(ValidatorStep(validator_id="_d_line_fail_mock", blocking=True))
    return GatePolicy(
        policy_version="d-line-test-policy-v1",
        status="frozen-candidate",
        description="D 线测试策略",
        chains=[ChainEntry(pack_id=pack_id, artifact_type="item", validators=steps)],
    )


class _AlwaysFailValidator(Validator):
    """总是 fail 的 mock 验证器（测试门失败路径）."""

    validator_id = "_d_line_fail_mock"
    version = "test-0.1.0"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        return self._timed_result(
            verdict="fail",
            evidence={"reason": "测试用强制失败"},
            confidence=Decimal("1.000"),
            elapsed_ms=1,
        )


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_registry():
    """注册表隔离：每测试前重置 + 注册所需验证器."""
    reset_registry()
    reset_d_line_registry()
    # 注册 D 线量规完整性验证器（platform 桶）
    register_validator("platform", RubricCompletenessValidator)
    # 注册通用验证器（policy loader 默认策略校验需要声明存在）
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    yield
    reset_registry()
    reset_d_line_registry()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_and_seed(async_session: AsyncSession):
    """每测试前清空 gate + content 表，并插入 cert:none 占位行.

    cert:none 占位行：run_gate 失败时 gate_run.certificate_id 用 'cert:none' 作 FK 目标.
    """
    await async_session.execute(
        text(
            "TRUNCATE TABLE "
            "gate_verdict, gate_run, gate_certificate, "
            "item_version, item, item_kp, publication, item_group, "
            "material_version, material, corpus_version, corpus_asset, "
            "material_license, shadow_score "
            "RESTART IDENTITY CASCADE"
        )
    )
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES ('cert:none', 'placeholder-for-failed-run', 'publish',"
            " 'no-policy', 'system')"
        )
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# §3 RubricCompletenessValidator 单元测试
# ────────────────────────────────────────────────────────────────────


class TestRubricCompletenessValidator:
    """验收 §3：量规完整性校验器（维度齐全/分值合计正确/等级描述非空）."""

    @pytest.mark.asyncio
    async def test_valid_rubric_passes(self) -> None:
        """合法量规 → pass."""
        rubric = _make_rubric()
        validator = RubricCompletenessValidator()
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "scoring_ref": {
                    "scorer_id": "ai_rubric",
                    "scorer_params": {"rubric": rubric.to_scorer_params()},
                }
            },
        )
        result = await validator.validate("sha256:iv-test", ctx)
        assert result.verdict == "pass"
        assert "content" in result.evidence["dimensions"]
        assert result.evidence["total_max_score"] == 20

    @pytest.mark.asyncio
    async def test_missing_rubric_fails(self) -> None:
        """scorer_params 缺 rubric → fail."""
        validator = RubricCompletenessValidator()
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={"scoring_ref": {"scorer_id": "ai_rubric", "scorer_params": {}}},
        )
        result = await validator.validate("sha256:iv-test", ctx)
        assert result.verdict == "fail"
        assert "rubric" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_missing_dimensions_fails(self) -> None:
        """量规缺 dimensions → fail（parse_rubric 拒绝）."""
        validator = RubricCompletenessValidator()
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "scoring_ref": {
                    "scorer_id": "ai_rubric",
                    "scorer_params": {"rubric": {"dimensions": [], "total_max_score": 0}},
                }
            },
        )
        result = await validator.validate("sha256:iv-test", ctx)
        assert result.verdict == "fail"

    @pytest.mark.asyncio
    async def test_wrong_score_total_fails(self) -> None:
        """分值合计不正确 → fail."""
        validator = RubricCompletenessValidator()
        bad_rubric = {
            "dimensions": [
                {
                    "id": "content", "name": "内容",
                    "anchors": ["主题明确", "主题模糊"],
                    "score_bands": [
                        {"level": 1, "label": "优秀", "score": 5},
                        {"level": 2, "label": "待改进", "score": 1},
                    ],
                    "error_type_rules": [],
                }
            ],
            # 故意写错：实际维度满分 5，声明 10
            "total_max_score": 10,
        }
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "scoring_ref": {
                    "scorer_id": "ai_rubric",
                    "scorer_params": {"rubric": bad_rubric},
                }
            },
        )
        result = await validator.validate("sha256:iv-test", ctx)
        assert result.verdict == "fail"
        assert "分值合计" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_empty_anchor_fails(self) -> None:
        """等级描述（anchor）为空字符串 → fail."""
        validator = RubricCompletenessValidator()
        bad_rubric = {
            "dimensions": [
                {
                    "id": "content", "name": "内容",
                    "anchors": ["主题明确", ""],  # 第二档描述为空
                    "score_bands": [
                        {"level": 1, "label": "优秀", "score": 5},
                        {"level": 2, "label": "待改进", "score": 1},
                    ],
                    "error_type_rules": [],
                }
            ],
            "total_max_score": 5,
        }
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "scoring_ref": {
                    "scorer_id": "ai_rubric",
                    "scorer_params": {"rubric": bad_rubric},
                }
            },
        )
        result = await validator.validate("sha256:iv-test", ctx)
        assert result.verdict == "fail"
        assert "等级描述为空" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_registered_on_platform(self) -> None:
        """RubricCompletenessValidator 注册在 platform 桶."""
        from src.core.gate.validator import list_validators

        assert "rubric_completeness" in list_validators("platform")


# ────────────────────────────────────────────────────────────────────
# §1/§2 run_d_pipeline 端到端（pass 路径）
# ────────────────────────────────────────────────────────────────────


class TestRunDPipelinePass:
    """验收 §1/§2：流水线返回 item_id + cert_id；scoring_ref 指向 ai_rubric."""

    @pytest.mark.asyncio
    async def test_returns_item_id_and_cert_id(
        self, async_session: AsyncSession
    ) -> None:
        """§1：run_d_pipeline 返回入库后的 item_id 与门证书 id."""
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy()
        result = await run_d_pipeline(
            bp_id,
            {"topic": "春天", "grade_band": "M"},
            db=async_session,
            policy=policy,
        )
        assert isinstance(result, DPipelineResult)
        assert result.final_verdict == "pass"
        assert result.item_id is not None and result.item_id
        assert result.cert_id is not None and result.cert_id
        assert result.item_version_id

    @pytest.mark.asyncio
    async def test_scoring_ref_points_to_ai_rubric(
        self, async_session: AsyncSession
    ) -> None:
        """§2：入库题目 scoring_ref.scorer_id == 'ai_rubric'."""
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy()
        result = await run_d_pipeline(
            bp_id,
            {"topic": "秋天", "grade_band": "H"},
            db=async_session,
            policy=policy,
        )
        # 从 DB 回读 item_version.scoring_ref
        row = (
            await async_session.execute(
                text(
                    "SELECT scoring_ref, gate_certificate_id, status"
                    " FROM item_version WHERE item_version_id = :ivid"
                ),
                {"ivid": result.item_version_id},
            )
        ).one()
        scoring_ref = row._mapping["scoring_ref"]
        assert scoring_ref["scorer_id"] == "ai_rubric"
        # 量规嵌入 scorer_params
        assert "rubric" in scoring_ref["scorer_params"]
        assert scoring_ref["scorer_params"]["rubric"]["total_max_score"] == 20
        # published 状态 + 门证书
        assert row._mapping["status"] == "published"
        assert row._mapping["gate_certificate_id"] == result.cert_id

    @pytest.mark.asyncio
    async def test_rubric_embedded_in_metadata(
        self, async_session: AsyncSession
    ) -> None:
        """§2：量规模板嵌入题目元数据（scorer_params.rubric 含完整维度）."""
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy()
        result = await run_d_pipeline(
            bp_id,
            {"topic": "成长", "grade_band": "M"},
            db=async_session,
            policy=policy,
        )
        row = (
            await async_session.execute(
                text("SELECT scoring_ref FROM item_version WHERE item_version_id = :ivid"),
                {"ivid": result.item_version_id},
            )
        ).one()
        rubric = row._mapping["scoring_ref"]["scorer_params"]["rubric"]
        dim_ids = [d["id"] for d in rubric["dimensions"]]
        assert dim_ids == ["content", "structure", "language", "handwriting"]
        for dim in rubric["dimensions"]:
            assert dim["anchors"], f"{dim['id']} anchors 为空"
            assert dim["score_bands"], f"{dim['id']} score_bands 为空"

    @pytest.mark.asyncio
    async def test_gate_certificate_persisted(
        self, async_session: AsyncSession
    ) -> None:
        """门证书落 gate_certificate 表（签发即 append-only）."""
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy()
        result = await run_d_pipeline(
            bp_id,
            {"topic": "春天", "grade_band": "L"},
            db=async_session,
            policy=policy,
        )
        row = (
            await async_session.execute(
                text(
                    "SELECT cert_id, artifact_ref, cert_type"
                    " FROM gate_certificate WHERE cert_id = :cid"
                ),
                {"cid": result.cert_id},
            )
        ).one()
        assert row._mapping["cert_id"] == result.cert_id
        assert row._mapping["artifact_ref"] == result.item_version_id
        assert row._mapping["cert_type"] == "publish"

    @pytest.mark.asyncio
    async def test_deterministic_item_version_id(
        self, async_session: AsyncSession
    ) -> None:
        """同输入同 item_version_id（D3 可复现）."""
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy()
        r1 = await run_d_pipeline(
            bp_id, {"topic": "春天", "grade_band": "M"},
            db=async_session, policy=policy,
        )
        # 重置 DB 跑第二次（同输入应得同 item_version_id；item_id 因 ULID 不同）
        # SET CONSTRAINTS ALL IMMEDIATE：让 savepoint 内 deferred FK/约束触发器先 fire，
        # 否则 TRUNCATE item 会因 "pending trigger events" 失败——async_session savepoint 隔离下
        # publish_item_version 的 commit 实为 RELEASE SAVEPOINT，item 行仍在外层事务里，
        # item 表的 deferred constraint trigger events 未 fire 时 TRUNCATE 被拒。
        await async_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await async_session.execute(
            text("TRUNCATE TABLE item_version, item, gate_verdict, gate_run, gate_certificate RESTART IDENTITY CASCADE")
        )
        await async_session.execute(
            text(
                "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
                " policy_version, issued_by)"
                " VALUES ('cert:none', 'placeholder', 'publish', 'no-policy', 'system')"
            )
        )
        await async_session.commit()
        r2 = await run_d_pipeline(
            bp_id, {"topic": "春天", "grade_band": "M"},
            db=async_session, policy=policy,
        )
        assert r1.item_version_id == r2.item_version_id

    @pytest.mark.asyncio
    async def test_picture_writing_pipeline(
        self, async_session: AsyncSession
    ) -> None:
        """看图写话蓝图同样可跑通流水线."""
        bp = _make_picture_writing_blueprint()
        register_d_line_blueprint(
            bp.blueprint_id,
            blueprint=bp,
            rubric=_make_rubric("L"),
            template_version=_PICTURE_WRITING_TEMPLATE,
            pack_digest=_PACK_DIGEST,
        )
        policy = _build_rubric_policy()
        result = await run_d_pipeline(
            bp.blueprint_id,
            {"picture_ref": "asset://pic/park.png", "prompt": "图上有什么？", "grade_band": "L"},
            db=async_session,
            policy=policy,
        )
        assert result.final_verdict == "pass"
        assert result.item_id is not None
        assert result.cert_id is not None


# ────────────────────────────────────────────────────────────────────
# §3 门失败路径：不发布、不签证书
# ────────────────────────────────────────────────────────────────────


class TestRunDPipelineGateFail:
    """验收 §3：门失败时不入库、不签证书（量规完整性是阻断项）."""

    @pytest.mark.asyncio
    async def test_gate_fail_no_publish(self, async_session: AsyncSession) -> None:
        """门失败 → item_id/cert_id 为 None，不入库 item_version."""
        register_validator("platform", _AlwaysFailValidator)
        bp_id = _register_composition_blueprint()
        policy = _build_rubric_policy(extra_failing=True)
        result = await run_d_pipeline(
            bp_id,
            {"topic": "春天", "grade_band": "M"},
            db=async_session,
            policy=policy,
        )
        assert result.final_verdict == "fail"
        assert result.item_id is None
        assert result.cert_id is None
        assert result.item_version_id  # 实例化产物仍有值（便于诊断）
        # item_version 表无该行（未发布）
        count = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM item_version WHERE item_version_id = :ivid"),
                {"ivid": result.item_version_id},
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_rubric_completeness_blocks_publish(
        self, async_session: AsyncSession
    ) -> None:
        """量规不完整 → rubric_completeness fail → 不发布.

        通过直接调 RubricCompletenessValidator 验证：构造缺 rubric 的 item_version，
        validator 应 fail（阻断发布）.
        """
        validator = RubricCompletenessValidator()
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "scoring_ref": {"scorer_id": "ai_rubric", "scorer_params": {}},
                "item_version_id": "sha256:bad-iv",
            },
        )
        result = await validator.validate("sha256:bad-iv", ctx)
        assert result.verdict == "fail"
        assert validator.blocking is True  # 阻断项


# ────────────────────────────────────────────────────────────────────
# §5 不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """验收 §5：src/core/production/ 禁止 import 学科包/学段包（宪法 A5/X6）."""

    def test_d_line_pipeline_no_packs_import(self) -> None:
        """d_line_pipeline.py 不 import 学科包/学段包."""
        production_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "production"
        )
        assert production_dir.is_dir()
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(production_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(production_dir)))
        assert not violations, (
            f"core/production 存在学科包 import（违反 A5）：{violations}"
        )

    def test_d_line_pipeline_no_llm_sdk_import(self) -> None:
        """核心域 production 禁止 import openai/deepseek/anthropic（X6 等价）."""
        production_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "production"
        )
        pattern = re.compile(
            r"^\s*(?:from\s+(?:openai|deepseek|anthropic)"
            r"|import\s+(?:openai|deepseek|anthropic))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(production_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(production_dir)))
        assert not violations, (
            f"core/production 直接 import LLM SDK（违反 X6）：{violations}"
        )
