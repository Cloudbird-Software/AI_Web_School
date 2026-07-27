"""T-W2-037 paper 追溯表与卷码/QR/题短码单元测试.

覆盖任务卡 4 条验收标准：
1. 迁移 0009 创建 paper/paper_item 表（含 placement_token、item_version_id）
2. trace_codes.py 提供 generate_paper_code / generate_qr_payload / generate_item_short_code
   QR payload 不含实例明文
3. 短码可校验并回溯到 paper_item → item_version → gate_certificate → 签发人
4. 单元测试覆盖生成、校验、回溯

迁移测试策略：
- 不依赖真实 DB（迁移本身由 make migrate-check 验证）
- 单元测试只测 ORM 模型类映射与 trace_codes.py 函数
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.render.trace_codes import (
    build_trace_chain,
    extract_paper_spec_id,
    generate_item_short_code,
    generate_paper_code,
    generate_qr_payload,
    generate_qr_svg,
    verify_item_short_code,
    verify_paper_code,
    verify_qr_payload,
)


# ════════════════════════════════════════════════════════════════════
# 1. 卷码生成与校验（验收 #2）
# ════════════════════════════════════════════════════════════════════

class TestPaperCode:
    def test_generate_returns_27_chars(self):
        """卷码 = 26 ULID + 1 校验位 = 27 字符."""
        code = generate_paper_code()
        assert len(code) == 27

    def test_generate_with_explicit_ulid(self):
        """显式传 ULID 时卷码可复现（用合法 Crockford base32 字符串）."""
        # Crockford base32 字符集：0-9 A-Z 剔除 I/L/O/U
        ulid = "01H3K7X9P0Q1R2S3T4V5W6X7YZ"
        code = generate_paper_code(ulid=ulid)
        assert code.startswith(ulid)
        assert len(code) == 27

    def test_verify_accepts_valid_code(self):
        """生成的卷码可通过校验."""
        code = generate_paper_code(ulid="01H3K7X9P0Q1R2S3T4V5W6X7YZ")
        assert verify_paper_code(code) is True

    def test_verify_rejects_wrong_length(self):
        """长度不符拒绝."""
        assert verify_paper_code("01H3K7X9P0Q") is False
        assert verify_paper_code("") is False

    def test_verify_rejects_tampered_code(self):
        """篡改卷码任一位→校验失败（Luhn 单错检测 ~100%）."""
        code = generate_paper_code(ulid="01H3K7X9P0Q1R2S3T4V5W6X7YZ")
        # 篡改第 5 位（base32 内部字符，避免破坏字符集）
        tampered = code[:4] + ("B" if code[4] != "B" else "C") + code[5:]
        assert verify_paper_code(tampered) is False

    def test_verify_rejects_invalid_alphabet(self):
        """ULID 部分含非 base32 字符（如 I/L/O/U）拒绝."""
        # I 在 Crockford base32 中被剔除
        code = "I" + "0" * 25 + "0"
        assert verify_paper_code(code) is False

    def test_invalid_ulid_length_raises(self):
        with pytest.raises(ValueError, match="ULID 长度必须 26"):
            generate_paper_code(ulid="too_short")

    def test_invalid_ulid_char_raises(self):
        with pytest.raises(ValueError, match="ULID 含非法字符"):
            # 含 'I'（Crockford base32 剔除）
            generate_paper_code(ulid="I" + "0" * 25)


# ════════════════════════════════════════════════════════════════════
# 2. QR payload（验收 #2：不含实例明文）
# ════════════════════════════════════════════════════════════════════

class TestQrPayload:
    def test_payload_contains_spec_id_and_checksum(self):
        """QR payload = spec_id + 1 位校验位."""
        spec_id = "spec-2026-W30-math-M-001"
        payload = generate_qr_payload(spec_id)
        assert payload.startswith(spec_id)
        assert len(payload) == len(spec_id) + 1

    def test_payload_verifies(self):
        payload = generate_qr_payload("spec-001")
        assert verify_qr_payload(payload) is True

    def test_extract_spec_id_round_trip(self):
        """生成的 payload 能提取回原 spec_id."""
        spec_id = "spec-2026-W30-math-M-001"
        payload = generate_qr_payload(spec_id)
        extracted = extract_paper_spec_id(payload)
        assert extracted == spec_id

    def test_extract_rejects_tampered(self):
        """篡改 payload → 提取返回 None."""
        payload = generate_qr_payload("spec-001")
        # 篡改最后一位（校验位）
        last = payload[-1]
        new_last = str((int(last) + 1) % 10)
        tampered = payload[:-1] + new_last
        assert extract_paper_spec_id(tampered) is None

    def test_empty_spec_id_raises(self):
        with pytest.raises(ValueError, match="paper_spec_id 不能为空"):
            generate_qr_payload("")

    def test_payload_no_item_version_leak(self):
        """验收 #2：QR payload 不含 item_version_id 等实例明文.

        构造一个明显含 item_version_id 的输入，确认 payload 不包含它。
        """
        item_version_id = "iv-LEAK-001"
        spec_id = "spec-safe-001"
        payload = generate_qr_payload(spec_id)
        assert item_version_id not in payload
        assert "iv-" not in payload
        assert "item_version" not in payload

    def test_qr_svg_generated(self):
        """generate_qr_svg 产出合法 <svg>...</svg>."""
        payload = generate_qr_payload("spec-001")
        svg = generate_qr_svg(payload)
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")
        assert "path" in svg.lower() or "rect" in svg.lower()


# ════════════════════════════════════════════════════════════════════
# 3. 题短码生成与校验（验收 #2/#3）
# ════════════════════════════════════════════════════════════════════

class TestItemShortCode:
    def test_short_code_is_7_chars(self):
        """题短码 = 6 base32 + 1 校验位 = 7 字符."""
        code = generate_item_short_code("pi-001")
        assert len(code) == 7

    def test_short_code_uses_crockford_alphabet(self):
        """短码主体使用 Crockford base32 字符集（无 I/L/O/U）."""
        # 多个 id 取样避免偶然
        for pid in ["pi-001", "pi-002", "pi-abc", "01H3K7X9P0Q1R2S3T4U5V6W7XY"]:
            code = generate_item_short_code(pid)
            body = code[:6]
            for ch in body:
                assert ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ", (
                    f"短码 {code} 含非 Crockford 字符 {ch}"
                )

    def test_short_code_deterministic(self):
        """同 paper_item_id → 同短码（确定性）."""
        code1 = generate_item_short_code("pi-001")
        code2 = generate_item_short_code("pi-001")
        assert code1 == code2

    def test_different_ids_produce_different_codes(self):
        """不同 paper_item_id → 不同短码."""
        code1 = generate_item_short_code("pi-001")
        code2 = generate_item_short_code("pi-002")
        assert code1 != code2

    def test_verify_accepts_valid_code(self):
        code = generate_item_short_code("pi-001")
        assert verify_item_short_code(code) is True

    def test_verify_rejects_wrong_length(self):
        assert verify_item_short_code("ABCDEF") is False  # 6 字符
        assert verify_item_short_code("ABCDEFGH") is False  # 8 字符

    def test_verify_rejects_tampered(self):
        """篡改任一位→校验失败."""
        code = generate_item_short_code("pi-001")
        # 翻转第一位（base32 内）
        first = code[0]
        new_first = "B" if first != "B" else "C"
        tampered = new_first + code[1:]
        assert verify_item_short_code(tampered) is False

    def test_empty_paper_item_id_raises(self):
        with pytest.raises(ValueError, match="paper_item_id 不能为空"):
            generate_item_short_code("")


# ════════════════════════════════════════════════════════════════════
# 4. 回溯链构造（验收 #3）
# ════════════════════════════════════════════════════════════════════

class TestTraceChain:
    """短码 → paper_item → item_version → gate_certificate → 签发人."""

    def test_build_trace_chain_with_certificate(self):
        """完整回溯链（含签发证书）."""
        paper_item_row = {
            "paper_item_id": "pi-001",
            "item_short_code": "ABCDEF1",
            "item_version_id": "iv-001",
            "paper_id": "p-001",
            "item_number": 1,
        }
        item_version_row = {
            "item_version_id": "iv-001",
            "item_id": "i-001",
            "gate_certificate_id": "cert-001",
            "status": "published",
            "lineage": {"tier": "C", "signed_by": "ai-pipeline-v1"},
        }
        cert_row = {
            "cert_id": "cert-001",
            "issued_by": "validator-orchestrator-v1",
            "issued_at": "2026-07-27T10:00:00Z",
            "policy_version": "v1.0",
        }
        chain = build_trace_chain(paper_item_row, item_version_row, cert_row)
        assert chain["item_short_code"] == "ABCDEF1"
        assert chain["paper_item_id"] == "pi-001"
        assert chain["paper_id"] == "p-001"
        assert chain["item_number"] == 1
        assert chain["item_version_id"] == "iv-001"
        assert chain["item_id"] == "i-001"
        assert chain["gate_certificate_id"] == "cert-001"
        assert chain["issued_by"] == "validator-orchestrator-v1"
        assert chain["policy_version"] == "v1.0"
        assert chain["lineage"]["tier"] == "C"

    def test_build_trace_chain_without_certificate(self):
        """item_version 未签发证书时（draft 状态）回溯链证书字段为 None."""
        paper_item_row = {
            "paper_item_id": "pi-002",
            "item_short_code": "XYZAB12",
            "item_version_id": "iv-002",
            "paper_id": "p-002",
            "item_number": 2,
        }
        item_version_row = {
            "item_version_id": "iv-002",
            "item_id": "i-002",
            "gate_certificate_id": None,
            "status": "draft",
            "lineage": {"tier": "A"},
        }
        chain = build_trace_chain(paper_item_row, item_version_row, None)
        assert chain["gate_certificate_id"] is None
        assert chain["issued_by"] is None
        assert chain["issued_at"] is None
        assert chain["policy_version"] is None

    def test_trace_chain_end_to_end(self):
        """端到端：生成短码 → 用短码作为反查键 → 构造回溯链."""
        paper_item_id = "01H3K7X9P0Q1R2S3T4U5V6W7XY"
        short_code = generate_item_short_code(paper_item_id)
        # 模拟 DB 行（实际系统由查询填充）
        paper_item_row = {
            "paper_item_id": paper_item_id,
            "item_short_code": short_code,
            "item_version_id": "iv-001",
            "paper_id": "p-001",
            "item_number": 1,
        }
        item_version_row = {
            "item_version_id": "iv-001",
            "item_id": "i-001",
            "gate_certificate_id": "cert-001",
            "status": "published",
            "lineage": {"tier": "C", "pipeline": {"id": "subject-math.b_assembler", "version": "1.0"}},
        }
        cert_row = {
            "cert_id": "cert-001",
            "issued_by": "validator-v1",
            "issued_at": "2026-07-27",
            "policy_version": "v1.0",
        }
        chain = build_trace_chain(paper_item_row, item_version_row, cert_row)
        # 短码可校验
        assert verify_item_short_code(chain["item_short_code"]) is True
        # 链含完整 4 跳：短码→paper_item→item_version→cert
        assert chain["item_short_code"] == short_code
        assert chain["paper_item_id"] == paper_item_id
        assert chain["item_version_id"] == "iv-001"
        assert chain["gate_certificate_id"] == "cert-001"
        assert chain["issued_by"] == "validator-v1"
        assert chain["lineage"]["pipeline"]["id"] == "subject-math.b_assembler"


# ════════════════════════════════════════════════════════════════════
# 5. ORM 模型与迁移对齐（验收 #1）
# ════════════════════════════════════════════════════════════════════

class TestOrmMigrationAlignment:
    """验证 ORM 模型与迁移 DDL 逐字对齐（避免漂移）."""

    def test_paper_model_has_required_columns(self):
        """Paper ORM 含迁移 0009 的所有列."""
        from src.core.models.paper import Paper
        cols = {c.name for c in Paper.__table__.columns}
        expected = {
            "paper_id", "paper_code", "paper_spec_id", "paper_title",
            "gradeband", "subject_pack_id", "weekly_batch_id",
            "kp_snapshot_ref", "seed", "rendered_snapshot_path",
            "created_at", "created_by",
        }
        assert cols == expected, f"Paper 列不符：差集 {expected ^ cols}"

    def test_paper_item_model_has_required_columns(self):
        """PaperItem ORM 含迁移 0009 的所有列（含 placement_token / item_version_id）."""
        from src.core.models.paper_item import PaperItem
        cols = {c.name for c in PaperItem.__table__.columns}
        expected = {
            "paper_item_id", "paper_id", "item_version_id",
            "placement_token", "item_number", "item_short_code",
            "created_at",
        }
        assert cols == expected, f"PaperItem 列不符：差集 {expected ^ cols}"

    def test_paper_item_fk_to_item_version(self):
        """PaperItem.item_version_id 是 FK→item_version（验收 #1）."""
        from src.core.models.paper_item import PaperItem
        col = PaperItem.__table__.columns["item_version_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "item_version"
        assert fk.column.name == "item_version_id"

    def test_paper_item_fk_to_paper(self):
        """PaperItem.paper_id 是 FK→paper."""
        from src.core.models.paper_item import PaperItem
        col = PaperItem.__table__.columns["paper_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "paper"

    def test_paper_code_unique(self):
        """paper_code 有 UNIQUE 约束."""
        from src.core.models.paper import Paper
        col = Paper.__table__.columns["paper_code"]
        assert col.unique is True

    def test_item_short_code_unique(self):
        """item_short_code 有 UNIQUE 约束."""
        from src.core.models.paper_item import PaperItem
        col = PaperItem.__table__.columns["item_short_code"]
        assert col.unique is True

    def test_migration_file_exists(self):
        """迁移 0009 文件存在."""
        p = Path(__file__).parent.parent.parent / "alembic" / "versions" / "0009_paper_trace.py"
        assert p.is_file(), f"迁移文件不存在：{p}"

    def test_migration_revision_chain(self):
        """迁移 0009 的 down_revision = 0008."""
        import importlib.util
        p = Path(__file__).parent.parent.parent / "alembic" / "versions" / "0009_paper_trace.py"
        spec = importlib.util.spec_from_file_location("_m0009", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "0009"
        assert mod.down_revision == "0008"


# ════════════════════════════════════════════════════════════════════
# 6. 学科零特判（A5：核心域不 import 学科包）
# ════════════════════════════════════════════════════════════════════

class TestNoSubjectPackImports:
    """trace_codes.py 是核心域，禁止 import 学科包."""

    def test_trace_codes_no_pack_imports(self):
        import inspect
        from src.core.render import trace_codes as tc
        src_text = inspect.getsource(tc)
        # 检查行首 import/from 语句
        assert re.search(r"(?m)^\s*(?:from\s+src\.packs|import\s+src\.packs)", src_text) is None

    def test_paper_model_no_pack_imports(self):
        import inspect
        from src.core.models import paper as paper_mod
        src_text = inspect.getsource(paper_mod)
        assert re.search(r"(?m)^\s*(?:from\s+src\.packs|import\s+src\.packs)", src_text) is None

    def test_paper_item_model_no_pack_imports(self):
        import inspect
        from src.core.models import paper_item as pi_mod
        src_text = inspect.getsource(pi_mod)
        assert re.search(r"(?m)^\s*(?:from\s+src\.packs|import\s+src\.packs)", src_text) is None
