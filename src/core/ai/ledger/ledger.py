"""T-W4-008 LLM 调用台账（append-only JSONL）.

每次 AI 调用经总线时记台账（架构 v2 §4.8）：
- 任务/模型/prompt 版本/token/成本/关联产物 id；
- append-only：仅追加，禁止 UPDATE/DELETE（D7 审计账，独立于 D1 三本账）；
- 按 artifact_ref（item_revision_id）归集单题全生命周期 AI 成本（T-W4-010 消费）。

存储选型：JSONL 文件而非 DB 表。
- AI 台账是审计账（§4.8），不在 D1 三本账（内容/作答/校验）之列，无需 DB 触发器强制；
- JSONL append-only 天然不可变（追加写），与「只增不改」语义对齐；
- 查询由内存索引满足（单题全生命周期调用量有限，无需 OLAP）；
- 测试隔离用 tmp_path 注入，不污染开发库。

成本单价：内置模型单价表（人民币，per 1K tokens）；调用方可覆盖。
单价来源：DeepSeek 官网 2026 公开价；gpt-4o 按 OpenAI 公开价 * 7（汇率估算）。
生产应从配置读取，此处内置仅 for 默认可用。

宪法 A5：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

import hashlib
import json
import os
import ulid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.core.ai.ledger.schemas import LedgerEntry, TaskStage, now_utc

# 模型单价表（人民币，per 1K tokens）；生产应外置配置，此处内置保默认可用
# 来源：DeepSeek 官网公开价；gpt-4o 按 OpenAI 公开价 * 7 汇率估算
_MODEL_PRICING_CNY_PER_1K: dict[str, dict[str, float]] = {
    "deepseek-chat": {"in": 0.001, "out": 0.002},
    "deepseek-reasoner": {"in": 0.004, "out": 0.016},
    "gpt-4o": {"in": 0.1225, "out": 0.49},  # $0.0175/$0.07 * 7
    # 未知模型零成本（避免 KeyError 阻断调用）
}

_DEFAULT_LEDGER_PATH = Path(".agent/telemetry/ai_ledger.jsonl")


def compute_cost_cny(model: str, token_in: int, token_out: int) -> float:
    """按模型单价表计算人民币成本（T-W4-010 归集依赖此函数一致）.

    未知模型返回 0.0 并由调用方在 raw_meta 标记，避免阻断调用。
    """
    pricing = _MODEL_PRICING_CNY_PER_1K.get(model)
    if pricing is None:
        return 0.0
    return round(
        pricing["in"] * token_in / 1000.0 + pricing["out"] * token_out / 1000.0,
        6,
    )


def hash_prompt(prompt: str) -> str:
    """prompt 的 sha256 hex 前 16 位（不存原文，防 PII 残留）."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


class Ledger:
    """append-only JSONL 台账.

    一个 Ledger 实例绑定一个文件路径；record_call 追加写，query_* 读全文件过滤。
    为什么不用 DB：见模块 docstring；测试用 tmp_path 注入实现隔离。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 不在构造时创建文件，首次 record_call 时由 open(a) 创建

    @property
    def path(self) -> Path:
        return self._path

    def record_call(
        self,
        *,
        task_level: str,
        task_name: str,
        provider: str,
        model: str,
        prompt: str,
        token_in: int,
        token_out: int,
        duration_ms: float,
        prompt_version: str = "v1",
        task_stage: TaskStage = "other",
        fallback: bool = False,
        artifact_ref: Optional[str] = None,
        cost_cny: Optional[float] = None,
        created_at: Optional[datetime] = None,
        raw_meta: Optional[dict[str, Any]] = None,
    ) -> str:
        """追加写入一条台账记录，返回 call_id.

        Args:
            task_level: L0/L1/L2/L3（路由档位）。
            task_name: 业务任务名（draft_passage / validate / score / rescore）。
            provider: 供应商名（deepseek/litellm/stub）。
            model: 实际命中的模型标识。
            prompt: 调用 prompt（仅算 hash，不存原文，防 PII 残留）。
            token_in/token_out: token 用量。
            duration_ms: 调用耗时（毫秒）。
            prompt_version: prompt 模板版本。
            task_stage: 成本归集阶段（T-W4-010 按此分桶）。
            fallback: 是否走了 fallback 供应商。
            artifact_ref: 关联产物 id（item_revision_id，T-W4-010 归集键）。
            cost_cny: 显式成本（None 时按模型单价表算）。
            created_at: 调用时间戳（None 时取当前 UTC）。
            raw_meta: 供应商原始响应子集（禁止含 PII）。

        Returns:
            call_id（ULID，全局唯一）。
        """
        call_id = str(ulid.new())
        if cost_cny is None:
            cost_cny = compute_cost_cny(model, token_in, token_out)
        if created_at is None:
            created_at = now_utc()
        entry = LedgerEntry(
            call_id=call_id,
            task_level=task_level,
            task_name=task_name,
            task_stage=task_stage,
            provider=provider,
            model=model,
            prompt_hash=hash_prompt(prompt),
            prompt_version=prompt_version,
            token_in=token_in,
            token_out=token_out,
            cost_cny=cost_cny,
            duration_ms=duration_ms,
            fallback=fallback,
            artifact_ref=artifact_ref,
            created_at=created_at,
            raw_meta=raw_meta or {},
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.to_jsonl() + "\n")
        return call_id

    def _iter_entries(self) -> Iterable[LedgerEntry]:
        """惰性遍历所有台账记录（文件不存在时返回空迭代）."""
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield LedgerEntry.model_validate_json(line)

    def query_all(self) -> list[LedgerEntry]:
        """返回全部台账记录（按 created_at 升序）."""
        return sorted(self._iter_entries(), key=lambda e: e.created_at)

    def query_by_artifact(self, artifact_ref: str) -> list[LedgerEntry]:
        """按 artifact_ref（item_revision_id）查询全生命周期调用（T-W4-010 消费）.

        Returns:
            匹配的台账记录列表（按 created_at 升序）。
        """
        return [
            e
            for e in self._iter_entries()
            if e.artifact_ref == artifact_ref
        ]


# ── 模块级默认实例（生产用；测试用 set_default_ledger 注入 tmp_path 实例）──

_default_ledger: Optional[Ledger] = None


def get_default_ledger() -> Ledger:
    """获取默认台账实例（懒建，路径从 AI_LEDGER_PATH 环境变量或默认路径）."""
    global _default_ledger
    if _default_ledger is None:
        path = Path(
            os.environ.get("AI_LEDGER_PATH", str(_DEFAULT_LEDGER_PATH))
        )
        _default_ledger = Ledger(path)
    return _default_ledger


def set_default_ledger(ledger: Optional[Ledger]) -> None:
    """注入默认台账实例（测试隔离用；传 None 重置）."""
    global _default_ledger
    _default_ledger = ledger


def record_call(
    *,
    task_level: str,
    task_name: str,
    provider: str,
    model: str,
    prompt: str,
    token_in: int,
    token_out: int,
    duration_ms: float,
    **kwargs: Any,
) -> str:
    """模块级 record_call（转发到默认实例，签名对齐 Ledger.record_call）."""
    return get_default_ledger().record_call(
        task_level=task_level,
        task_name=task_name,
        provider=provider,
        model=model,
        prompt=prompt,
        token_in=token_in,
        token_out=token_out,
        duration_ms=duration_ms,
        **kwargs,
    )


def query_by_artifact(artifact_ref: str) -> list[LedgerEntry]:
    """模块级查询（转发到默认实例，T-W4-010 消费此接口）."""
    return get_default_ledger().query_by_artifact(artifact_ref)
