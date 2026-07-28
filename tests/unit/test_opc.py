"""tests/unit/test_opc.py — T-W0-003 验收单测

覆盖任务卡三条验收标准：
  a. owner 冲突任务板 → `opc board` 退出码 1 且输出指出冲突双方
  b. `opc dispatch T-W0-001` prompt 文件含 .agent/rules/core.md 全文与任务卡路径
  c. n≥20、通过率 50% 的遥测 → `opc dashboard` 输出降权标记 ⛔

实现策略：用 importlib 把 tools/opc 当模块加载，monkeypatch ROOT 指向 tmp_path，
避免污染真实 tasks/ 与 .agent/telemetry/。
"""
import importlib.util
import json
import os
import textwrap
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace

import pytest

ROOT_PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPC_PATH = os.path.join(ROOT_PROJECT, "tools", "opc")


def _load_opc(tmp_path, monkeypatch):
    """加载 tools/opc 为模块并把 ROOT 重定向到 tmp_path。

    为什么不用 spec_from_file_location：tools/opc 无 .py 后缀，
    默认 suffix-based loader 不识别，必须显式给 SourceFileLoader。
    """
    loader = SourceFileLoader("opc_under_test", OPC_PATH)
    spec = importlib.util.spec_from_loader("opc_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    return mod


def _write(path, content):
    """写文件，自动创建父目录。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _minimal_card(tid, owner_module="tools/", depends_on="[]"):
    """最小合规任务卡（含 parse_card 能识别的全部字段）。"""
    return f"""# 任务卡
```yaml
id: {tid}
wave: W0
title: 测试卡 {tid}
spec: []
context_paths: []
deliverables: []
acceptance: make accept TASK={tid}
accept_script:
model_floor: T1
token_budget: 400k
owner_module: {owner_module}
depends_on: {depends_on}
non_goals: [test]
escalation: fail×2 → 升梯队 → Judge → 人类
```

## 目标说明
测试卡

## 验收标准（逐条可执行）
1. 测试
"""


# ────────────────────────────────────────────────────────────────────
# 验收标准 a：owner 冲突 → board 退出码 1 且输出指出冲突双方
# ────────────────────────────────────────────────────────────────────
def test_board_owner_conflict_exit1_and_names_both_parties(tmp_path, monkeypatch, capsys):
    """两张进行中卡同占 src/foo → board 退出 1 且输出含双方 id 与 owner_module。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w0" / "T-W0-101.md"), _minimal_card("T-W0-101", "src/foo"))
    _write(str(tmp_path / "tasks" / "w0" / "T-W0-102.md"), _minimal_card("T-W0-102", "src/foo"))
    board = textwrap.dedent("""\
        # 任务板（唯一调度事实源）

        ## 进行中
        | 任务卡 | owner_module | 模型 | 开始日期 |
        |---|---|---|---|
        | T-W0-101 | src/foo | T1 | 2026-07-26 |
        | T-W0-102 | src/foo | T1 | 2026-07-26 |

        ## 就绪（按优先级）
        | 任务卡 | 标题 | model_floor | 依赖 |
        |---|---|---|---|
        """)
    _write(str(tmp_path / "tasks" / "board.md"), board)

    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    # 冲突双方 id 都要出现
    assert "T-W0-101" in out
    assert "T-W0-102" in out
    # 冲突的 owner_module 也要出现
    assert "src/foo" in out
    assert "冲突" in out


def test_board_no_conflict_when_only_ready_section_shares_owner(tmp_path, monkeypatch, capsys):
    """回归保护：就绪段两张卡同 owner 不应误判为进行中冲突（旧正则的 bug）。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w0" / "T-W0-201.md"), _minimal_card("T-W0-201", "src/bar"))
    _write(str(tmp_path / "tasks" / "w0" / "T-W0-202.md"), _minimal_card("T-W0-202", "src/bar"))
    board = textwrap.dedent("""\
        # 任务板

        ## 进行中
        | 任务卡 | owner_module | 模型 | 开始日期 |
        |---|---|---|---|

        ## 就绪（按优先级）
        | 任务卡 | 标题 | model_floor | 依赖 |
        |---|---|---|---|
        | T-W0-201 | a | T1 | — |
        | T-W0-202 | b | T1 | — |
        """)
    _write(str(tmp_path / "tasks" / "board.md"), board)
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "✅" in out
    assert "冲突" not in out


# ────────────────────────────────────────────────────────────────────
# 验收标准 b：dispatch prompt 含 core.md 全文与任务卡路径
# ────────────────────────────────────────────────────────────────────
def test_dispatch_prompt_contains_core_fulltext_and_card_path(tmp_path, monkeypatch, capsys):
    """dispatch T-W0-001 渲染的 prompt 文件含 core.md 全文与任务卡相对路径。"""
    opc = _load_opc(tmp_path, monkeypatch)
    core_content = (
        "# 工程规则（测试桩）\n"
        "铁律1 三本账只增不改\n"
        "铁律2 未过校验门禁止入已发布区\n"
        "特殊字符：中文 标点 ⛔ ✅ ❌ —— 应原样保留\n"
    )
    _write(str(tmp_path / ".agent" / "rules" / "core.md"), core_content)
    role_content = (
        "# 角色：Builder\n"
        "任务卡：{{TASK_CARD_PATH}}（先完整读它）\n"
        "工程规则：.agent/rules/core.md（已注入）\n"
    )
    _write(str(tmp_path / ".agent" / "roles" / "builder.md"), role_content)
    _write(str(tmp_path / "tasks" / "w0" / "T-W0-001.md"), _minimal_card("T-W0-001", "tools/"))

    opc.cmd_dispatch(SimpleNamespace(task_id="T-W0-001", dry_run=True))

    prompt_path = tmp_path / "tasks" / ".outbox" / "T-W0-001-builder-prompt.md"
    assert prompt_path.exists(), "prompt 文件未生成"
    content = prompt_path.read_text(encoding="utf-8")
    # core.md 全文嵌入（特殊字符也须保留）
    assert core_content in content, "prompt 缺 core.md 全文"
    # 任务卡相对路径出现
    rel_card = os.path.join("tasks", "w0", "T-W0-001.md")
    assert rel_card in content, f"prompt 缺任务卡路径 {rel_card}"


def test_dispatch_unknown_task_exits1(tmp_path, monkeypatch, capsys):
    """dispatch 不存在的任务卡 → 退出 1 且给出明确错误。"""
    opc = _load_opc(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        opc.cmd_dispatch(SimpleNamespace(task_id="T-W0-999", dry_run=True))
    assert exc.value.code == 1
    assert "T-W0-999" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────────────
# 验收标准 c：n≥20、通过率 50% → dashboard 输出降权标记 ⛔
# ────────────────────────────────────────────────────────────────────
def test_dashboard_demotion_flag_when_low_pass_rate(tmp_path, monkeypatch, capsys):
    """20 行同模型同任务类型、10 PASS 10 FAIL（通过率 50%<60%）→ 输出 ⛔。"""
    opc = _load_opc(tmp_path, monkeypatch)
    rows = []
    for i in range(20):
        rows.append({
            "task_id": f"T-W2-{i:03d}",
            "wave": "W2",
            "role": "builder",
            "model": "deepseek/deepseek-v4-pro",
            "model_tier": "T1",
            "task_type": "impl",
            "owner_module": "core/instantiation",
            "started_at": "2026-08-01T09:00:00Z",
            "finished_at": "2026-08-01T11:30:00Z",
            "tokens_in": 1000,
            "tokens_out": 100,
            "cost_cny": 1.0,
            "attempts": 1,
            "escalated": False,
            "gate_results": {"unit": "pass", "contract": "pass", "golden": "pass"},
            "verifier_verdict": "PASS" if i < 10 else "FAIL",
            "pr": f"#{100+i}",
            "merged": i < 10,
            "misreport": False,
        })
    lines = "".join(json.dumps(r) + "\n" for r in rows)
    _write(str(tmp_path / ".agent" / "telemetry" / "events-202608.jsonl"), lines)

    opc.cmd_dashboard(None)
    out = capsys.readouterr().out
    assert "⛔" in out, f"通过率 50% 未触发降权标记；输出:\n{out}"
    # 同时验证阈值门槛的描述与样本数
    assert "n= 20" in out or "n=20" in out
    assert "deepseek/deepseek-v4-pro" in out


def test_dashboard_no_demotion_when_pass_rate_high(tmp_path, monkeypatch, capsys):
    """回归保护：通过率 70%（≥60%）不应触发降权标记。"""
    opc = _load_opc(tmp_path, monkeypatch)
    rows = []
    for i in range(20):
        rows.append({
            "task_id": f"T-W2-{i:03d}",
            "model": "deepseek/deepseek-v4-pro",
            "model_tier": "T1",
            "task_type": "impl",
            "verifier_verdict": "PASS" if i < 14 else "FAIL",
            "merged": i < 14,
            "cost_cny": 1.0,
            "tokens_in": 1000,
            "tokens_out": 100,
        })
    lines = "".join(json.dumps(r) + "\n" for r in rows)
    _write(str(tmp_path / ".agent" / "telemetry" / "events-202608.jsonl"), lines)
    opc.cmd_dashboard(None)
    out = capsys.readouterr().out
    assert "⛔" not in out


def test_dashboard_empty_telemetry(tmp_path, monkeypatch, capsys):
    """无遥测文件 → 友好提示，不报错。"""
    opc = _load_opc(tmp_path, monkeypatch)
    opc.cmd_dashboard(None)
    out = capsys.readouterr().out
    assert "暂无遥测数据" in out


# ────────────────────────────────────────────────────────────────────
# board 备注 ②（W2 遗留）：验证卡 T-W*-T0X 格式与 prerequisites 字段校验
# ────────────────────────────────────────────────────────────────────
def _minimal_verification_card(tid, owner_module="tests/acceptance/x",
                               validates="[]", prerequisites="[]"):
    """最小合规验证卡（`# 验证卡` 头 + validates/prerequisites 字段）。"""
    return f"""# 验证卡
```yaml
id: {tid}
wave: W2
owner_module: {owner_module}
title: 验证卡 {tid}
validates: {validates}
prerequisites: {prerequisites}
parallel_group: W2a
model_floor: T1
token_budget: 400k
validation_script:
```

## 验证策略
测试验证卡
"""


def _empty_board():
    """空进行中/就绪段的合规任务板（仅用于 board 校验，无调度内容）。"""
    return textwrap.dedent("""\
        # 任务板（唯一调度事实源）

        ## 进行中
        | 任务卡 | owner_module | 模型 | 开始日期 |
        |---|---|---|---|

        ## 就绪（按优先级）
        | 任务卡 | 标题 | model_floor | 依赖 |
        |---|---|---|---|
        """)


def test_board_accepts_valid_verification_card(tmp_path, monkeypatch, capsys):
    """验证卡头 + 验证卡 id + 合法 prerequisites/validates 引用 → board 通过。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-001.md"),
           _minimal_card("T-W2-001", "tools/"))
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-T01.md"),
           _minimal_verification_card(
               "T-W2-T01",
               validates="[T-W2-001]",
               prerequisites="[T-W2-001]"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 0
    assert "✅" in capsys.readouterr().out


def test_board_accepts_w01_style_verification_id(tmp_path, monkeypatch, capsys):
    """board 备注 P10：W1 验证卡文件名 T-W01-T0x（W01 而非 W1）须被格式正则接受。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w1" / "T-W01-T01.md"),
           _minimal_verification_card("T-W01-T01"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "不一致" not in out


def test_board_rejects_verification_header_with_task_id(tmp_path, monkeypatch, capsys):
    """`# 验证卡` 头但 id 为任务卡格式（T-W2-099）→ 格式不一致，退出 1。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-099.md"),
           _minimal_verification_card("T-W2-099"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "T-W2-099" in out
    assert "验证卡头标记(True)" in out
    assert "id 格式(False)" in out


def test_board_rejects_verification_id_with_task_header(tmp_path, monkeypatch, capsys):
    """id 为验证卡格式（T-W2-T01）但头为 `# 任务卡` → 格式不一致，退出 1。"""
    opc = _load_opc(tmp_path, monkeypatch)
    # _minimal_card 产出 `# 任务卡` 头；传验证卡格式 id 制造不一致
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-T01.md"),
           _minimal_card("T-W2-T01", "tools/"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "T-W2-T01" in out
    assert "验证卡头标记(False)" in out
    assert "id 格式(True)" in out


def test_board_rejects_dangling_prerequisites(tmp_path, monkeypatch, capsys):
    """验证卡 prerequisites 引用不存在的卡 → 退出 1 并指出引用。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-T01.md"),
           _minimal_verification_card(
               "T-W2-T01", prerequisites="[T-W2-999]"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "prerequisites" in out
    assert "T-W2-999" in out


def test_board_rejects_dangling_validates(tmp_path, monkeypatch, capsys):
    """验证卡 validates 引用不存在的卡 → 退出 1 并指出引用。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w2" / "T-W2-T01.md"),
           _minimal_verification_card(
               "T-W2-T01", validates="[T-W2-999]"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "validates" in out
    assert "T-W2-999" in out


def test_board_prerequisites_can_reference_other_verification_cards(
    tmp_path, monkeypatch, capsys
):
    """prerequisites 可引用其他验证卡（如 T-W4-T05 依赖 T-W4-T01..T04）。"""
    opc = _load_opc(tmp_path, monkeypatch)
    _write(str(tmp_path / "tasks" / "w4" / "T-W4-T01.md"),
           _minimal_verification_card("T-W4-T01"))
    _write(str(tmp_path / "tasks" / "w4" / "T-W4-T05.md"),
           _minimal_verification_card(
               "T-W4-T05", prerequisites="[T-W4-T01]"))
    _write(str(tmp_path / "tasks" / "board.md"), _empty_board())
    with pytest.raises(SystemExit) as exc:
        opc.cmd_board(None)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "✅" in out
