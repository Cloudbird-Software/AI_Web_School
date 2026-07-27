"""T-W2-022 黄金数据集 schema 与加载器单元测试.

验收对照（任务卡 T-W2-022）：
  §1 schema.yaml 定义必填字段 template/params/expected_item_version_id/
     expected_content_snapshot ✅（test_schema_yaml_defines_required_fields）
  §2 conftest.py 提供 golden_case fixture，可枚举所有 YAML
     ✅（test_golden_case_fixture_parametrizes_items）
  §3 加载器对格式错误给出明确行号提示
     ✅（test_invalid_yaml_reports_line_number / test_missing_field_reports_field_path）
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tests.golden.conftest import (
    GoldenCase,
    GoldenCaseLoadError,
    discover_golden_case_paths,
    load_golden_case,
)

# ────────────────────────────────────────────────────────────────────
# 测试夹具：构造合法/非法黄金用例文本
# ────────────────────────────────────────────────────────────────────
# 一份最小合法用例（与 tests/golden/instantiation/sample_single_choice.yaml
# 同构，但补齐 expected_content_snapshot，满足 T-W2-022 schema）。
_VALID_CASE_YAML = dedent("""\
    case_id: valid_sample
    description: 单选题——整数加法
    template_version:
      template_version_id: sha256:0000000000000000000000000000000000000000000000000000000000000000
      template_id: tpl-001-single-choice-add
      dsl_version: '1'
      spec:
        objective:
          kp_set:
          - dimension: kp
            code: math.nal.int.add
          kp_set_mode: single
          cognitive_level: apply
          gradeband: L
          graph_release: '2026.1'
        slots:
          a: {type: int, difficulty_relevant: true}
          b: {type: int, difficulty_relevant: true}
        variation_axes: {axes: []}
        presentation:
          blocks:
          - {kind: text, template: '{a} + {b} = ?'}
        answer_program: {expression: a + b, returns: number}
        distractor_rules:
          rules:
          - rule_type: deterministic
            error_type_id: err.calc.add.off-by-one
            expression: a + b + 1
            label: 多1
    params:
      a: 3
      b: 4
    pack_digest: sha256:1111111111111111111111111111111111111111111111111111111111111111
    interaction_id: single_choice
    scorer_id: exact_match
    scorer_params:
      answer: 7
    locale: zh-CN
    corpus_digests: []
    seed: 0
    expected_item_version_id: sha256:2222222222222222222222222222222222222222222222222222222222222222
    expected_content_snapshot:
      blocks:
      - kind: text
        template: '{a} + {b} = ?'
        rendered: 3 + 4 = ?
""")


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    """把内容写入 tmp_path/<name>.yaml 并返回路径."""
    p = tmp_path / f"{name}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ────────────────────────────────────────────────────────────────────
# §1 schema.yaml 契约
# ────────────────────────────────────────────────────────────────────
def test_schema_yaml_defines_required_fields() -> None:
    """schema.yaml 存在并声明四项必填字段（验收 §1）."""
    schema_path = (
        Path(__file__).resolve().parent.parent / "golden" / "schema.yaml"
    )
    assert schema_path.is_file(), "tests/golden/schema.yaml 必须存在"
    import yaml

    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    required = schema.get("required", [])
    # 验收 §1：四项必填字段
    for field in (
        "template_version",
        "params",
        "expected_item_version_id",
        "expected_content_snapshot",
    ):
        assert field in required, f"schema.yaml 必须要求字段 {field}"


# ────────────────────────────────────────────────────────────────────
# §3 加载器：合法用例
# ────────────────────────────────────────────────────────────────────
def test_valid_case_loads_successfully(tmp_path: Path) -> None:
    """合法用例加载成功并返回 GoldenCase（验收 §3 正向）."""
    p = _write_yaml(tmp_path, "valid_sample", _VALID_CASE_YAML)
    case = load_golden_case(p)
    assert isinstance(case, GoldenCase)
    assert case.case_id == "valid_sample"
    assert case.interaction_id == "single_choice"
    assert case.expected_content_snapshot["blocks"][0]["rendered"] == "3 + 4 = ?"


# ────────────────────────────────────────────────────────────────────
# §3 加载器：YAML 语法错误 → 行号
# ────────────────────────────────────────────────────────────────────
def test_invalid_yaml_reports_line_number(tmp_path: Path) -> None:
    """YAML 语法错误时报告行号（验收 §3：明确行号提示）."""
    # 故意制造缩进错误：第二行缩进异常
    bad_yaml = dedent("""\
        case_id: bad
          description: 错误缩进
        """)
    p = _write_yaml(tmp_path, "bad_yaml", bad_yaml)
    with pytest.raises(GoldenCaseLoadError) as exc_info:
        load_golden_case(p)
    err = exc_info.value
    assert err.path == p
    # YAML 错误必须带行号（1-based，>0）
    assert err.line > 0, "YAML 语法错误必须报告行号"
    # 错误消息包含文件路径与行号，便于定位
    msg = str(err)
    assert str(p) in msg
    assert "行" in msg or str(err.line) in msg


# ────────────────────────────────────────────────────────────────────
# §3 加载器：字段缺失 → 字段路径
# ────────────────────────────────────────────────────────────────────
def test_missing_field_reports_field_path(tmp_path: Path) -> None:
    """必填字段缺失时报告字段路径（验收 §3）."""
    # 删除 expected_content_snapshot 整段
    bad_case = _VALID_CASE_YAML.replace(
        "expected_content_snapshot:\n"
        "  blocks:\n"
        "  - kind: text\n"
        "    template: '{a} + {b} = ?'\n"
        "    rendered: 3 + 4 = ?\n",
        "",
    )
    p = _write_yaml(tmp_path, "missing_field", bad_case)
    with pytest.raises(GoldenCaseLoadError) as exc_info:
        load_golden_case(p)
    err = exc_info.value
    assert err.path == p
    # 字段校验失败时 field_path 非空（指向缺失字段）
    assert err.field_path, "字段校验失败必须报告字段路径"
    assert "expected_content_snapshot" in err.field_path


def test_invalid_sha256_pattern_rejected(tmp_path: Path) -> None:
    """pack_digest 不符合 sha256: 格式时被拒绝（验收 §3）."""
    bad_case = _VALID_CASE_YAML.replace(
        "pack_digest: sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "pack_digest: not-a-sha256",
    )
    p = _write_yaml(tmp_path, "bad_digest", bad_case)
    with pytest.raises(GoldenCaseLoadError) as exc_info:
        load_golden_case(p)
    err = exc_info.value
    assert err.field_path == "pack_digest"


def test_top_level_not_mapping_rejected(tmp_path: Path) -> None:
    """顶层不是 mapping 时被拒绝（验收 §3）."""
    p = _write_yaml(tmp_path, "list_top", "- a\n- b\n")
    with pytest.raises(GoldenCaseLoadError) as exc_info:
        load_golden_case(p)
    err = exc_info.value
    assert err.path == p
    assert "mapping" in err.detail


def test_extra_field_rejected(tmp_path: Path) -> None:
    """extra='forbid'：未声明字段被拒绝（schema 冻结纪律）."""
    bad_case = _VALID_CASE_YAML + "unknown_field: 123\n"
    p = _write_yaml(tmp_path, "extra_field", bad_case)
    with pytest.raises(GoldenCaseLoadError):
        load_golden_case(p)


def test_empty_params_rejected(tmp_path: Path) -> None:
    """params 为空 object 被拒绝（min_length=1）."""
    bad_case = _VALID_CASE_YAML.replace(
        "params:\n  a: 3\n  b: 4\n",
        "params: {}\n",
    )
    p = _write_yaml(tmp_path, "empty_params", bad_case)
    with pytest.raises(GoldenCaseLoadError) as exc_info:
        load_golden_case(p)
    assert "params" in exc_info.value.field_path


# ────────────────────────────────────────────────────────────────────
# §2 discover：items/ 不存在时返回空
# ────────────────────────────────────────────────────────────────────
def test_discover_returns_list_type() -> None:
    """discover_golden_case_paths 返回 list（验收 §2 枚举能力）."""
    paths = discover_golden_case_paths()
    assert isinstance(paths, list)
    # 排序保证跨平台一致
    assert paths == sorted(paths)


def test_golden_case_fixture_parametrizes_items() -> None:
    """golden_case fixture 可枚举 items/ 下 YAML（验收 §2）.

    当 items/ 为空（T-W2-023/024 未产出）时，使用 golden_case 的测试
    会被 pytest 跳过；产出样本后自动激活。本测试只验证 fixture 可被
    收集（不依赖 items/ 实际内容）.
    """
    # 通过 pytest 的 fixture 机制验证：golden_case 在 conftest 中已注册
    # 为参数化 fixture，params 来自 discover_golden_case_paths()。
    # 这里验证 discover 函数与 fixture 的契约：fixture 的参数列表
    # 必须等于 discover 返回的路径列表的 stem。
    from tests.golden.conftest import _GOLDEN_CASE_PATHS

    # fixture params 与 discover 一致（模块级缓存）
    assert _GOLDEN_CASE_PATHS == discover_golden_case_paths()
