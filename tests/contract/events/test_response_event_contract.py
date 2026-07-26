"""契约测试：response_event 事件契约（specs/contracts/events/response_event.md）。

验收标准（T-W0-004）：schema 可解析、必填字段存在（R-D-02 全要素）。
"""
import json
import re
from pathlib import Path

CONTRACT = Path("specs/contracts/events/response_event.md")

CORE_FIELDS = [
    "event_id", "student_alias_id", "item_version_id", "scene", "raw_payload",
    "duration_ms", "scoring_trace", "error_inferences", "session_id", "created_at",
]


def text():
    return CONTRACT.read_text(encoding="utf-8")


def extract_json_schema():
    """提取 §5 的 JSON Schema 代码块。"""
    m = re.search(r"## 5\..*?```json\n(.*?)```", text(), re.S)
    assert m, "未找到 §5 JSON Schema 代码块"
    return json.loads(m.group(1))


def test_file_exists_and_sections():
    t = text()
    for section in ("## 1. 表定义", "## 2. 写入与存储规则", "## 3. scoring_trace", "## 4. error_inferences", "## 5."):
        assert section in t, f"缺章节 {section}"


def test_json_schema_parseable():
    schema = extract_json_schema()
    assert schema["type"] == "object"
    assert "properties" in schema


def test_core_fields_required():
    """R-D-02：每条作答必须记录题目身份与版本、场景、耗时、评分、错误推断。"""
    schema = extract_json_schema()
    for field in CORE_FIELDS:
        assert field in schema["required"], f"required 缺 {field}"
        assert field in schema["properties"], f"properties 缺 {field}"


def test_scene_enum():
    """D5：场景三值，分场景独立估计禁止混估。"""
    schema = extract_json_schema()
    assert set(schema["properties"]["scene"]["enum"]) == {"practice", "diagnosis", "measurement"}


def test_append_only_and_archive_rules_stated():
    """D1/A3：append-only 与 Parquet 开放归档必须写入契约文本。"""
    t = text()
    assert "append-only" in t
    assert "UPDATE" in t and "DELETE" in t
    assert "Parquet" in t


def test_no_pii_fields():
    """D7：事件表只允许 student_alias_id，禁止直接标识字段。"""
    t = text()
    for forbidden in ("student_name", "real_name", "phone", "id_card"):
        assert forbidden not in t, f"事件契约出现疑似 PII 字段 {forbidden}"
    assert "student_alias_id" in t
