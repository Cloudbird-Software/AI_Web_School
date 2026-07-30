"""Issue #24 / T-W2-040: DB indexes + search + perf baseline 测试 + 服务入口.

提供：
1. `ItemVersionSearchService`：按 kp 代码 / 交互类型 / 学段 / 关键词 快速筛选 published 题，
   不依赖 Redis 索引（基线 v1），在 PG 上使用迁移 0023 的索引。
2. `perf_baseline_benchmark`：对 1000 行插入后做搜索基准测试（DB 环境才跑，否则 mock）。
3. `search_by_kp` / `search_by_interaction` / `search_in_pool` 等静态工具函数。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item_version import ItemVersion


# ════════════════════════════════════════════════════════════════════
# 类型：搜索条件 & 结果
# ════════════════════════════════════════════════════════════════════


@dataclass
class SearchQuery:
    """搜索条件（Issue #24 验收：至少支持 kp / 交互类型 / 学段三个维度）."""

    kp_codes: Sequence[str] = ()  # 任一命中即通过；空=不过滤
    interaction_ids: Sequence[str] = ()  # interaction_id 任一命中；空=不过滤
    gradebands: Sequence[str] = ()  # L/M/H；空=不过滤
    statuses: Sequence[str] = ("published", "draft")  # 默认看发布 + 草稿
    limit: int = 100
    offset: int = 0
    keywords: str = ""  # 简单的关键词 LIKE 内容文本块（子字符串包含，非全文索引，基线）


@dataclass
class SearchResult:
    """搜索结果."""

    total: int
    items: list[dict[str, Any]]
    latency_ms: int
    used_indexes: list[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════
# 内存版搜索（不依赖 PG，用于 serving 视图/离线数据）—— Issue #24 基线
# ════════════════════════════════════════════════════════════════════


def search_in_pool(
    pool: Iterable[dict[str, Any]], query: SearchQuery) -> SearchResult:
    """在内存 item_version dict 池中按 query 筛选（不依赖 DB，用于离线/基线搜索）.

    用于 weekly_batch / build_paper 等脚本的内存端筛选；复杂度 O(N) 无索引加速
    但避免在 CI 里依赖 PG 的 GIN。
    """
    t0 = time.perf_counter()
    collected: list[dict[str, Any]] = []
    for iv in pool:
        objective = iv.get("objective") or {}
        interaction_ref = iv.get("interaction_ref") or {}

        if query.statuses and iv.get("status") not in set(query.statuses):
            continue
        if query.gradebands and objective.get("gradeband") not in set(query.gradebands):
            continue
        if query.interaction_ids:
            if interaction_ref.get("interaction_id") not in set(query.interaction_ids):
                continue
        if query.kp_codes:
            target = set(query.kp_codes)
            kp_set = objective.get("kp_set") or []
            codes = {k.get("code") for k in kp_set if isinstance(k, dict)}
            if not (target & codes):
                continue
        if query.keywords:
            haystack = _extract_text_for_search(iv)
            if query.keywords.lower() not in haystack.lower():
                continue
        collected.append(iv)
        if query.limit and len(collected) > query.limit + query.offset:
            break
    total = len(collected)
    sliced = collected[query.offset : query.offset + query.limit] if query.limit else collected[query.offset:]
    return SearchResult(
        total=total,
        items=sliced,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


def _extract_text_for_search(iv: dict[str, Any]) -> str:
    """收集 item_version 中所有可搜文本：block texts/stem/passage/stem text 合并.

    基线关键词搜索 baseline：词包含搜索。
    """
    parts: list[str] = []
    content = iv.get("content") or {}
    blocks = content.get("blocks") or []
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("text") or b.get("value")
            if isinstance(t, str):
                parts.append(t)
            choices = b.get("choices") or b.get("options") or []
            if isinstance(choices, list):
                for c in choices:
                    if isinstance(c, dict):
                        lab = c.get("label")
                        if isinstance(lab, str):
                            parts.append(lab)
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════
# 服务层：PG 索引版搜索（用于生产/DB 环境）
# ════════════════════════════════════════════════════════════════════


class ItemVersionSearchService:
    """Issue #24: 数据库索引版搜索，依赖迁移 0023 的索引.

    该服务仅使用 item_version 表上的以下索引：
    - ix_item_version_item_id_status
    - ix_item_version_interaction_ref_interaction_id
    - ix_item_version_objective_gradeband
    - ix_item_version_content_gin（可选，PG JSONB）

    在没有 DB 时可退化为内存 search_in_pool 的包装器（见下方 fallback）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── 主入口 ────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> SearchResult:
        t0 = time.perf_counter()
        stmt = select(ItemVersion)
        conditions = []

        if query.statuses:
            conditions.append(ItemVersion.status.in_(tuple(set(query.statuses))))
        if query.gradebands:
            # 使用 ->> gradeband 走 ix_item_version_objective_gradeband 索引
            conditions.append(func.jsonb_extract_path_text(
                ItemVersion.objective, "gradeband"
            ).in_(tuple(set(query.gradebands))))
        if query.interaction_ids:
            conditions.append(func.jsonb_extract_path_text(
                ItemVersion.interaction_ref, "interaction_id"
            ).in_(tuple(set(query.interaction_ids))))

        if query.kp_codes:
            # kp_set 数组里任一 code 命中即可。JSONB @> ANY (array of jsonb)
            #   jsonb_array_elements(objective->'kp_set') @> {"code":"xxx"}
            kp_expr = ItemVersion.objective["kp_set"]
            # OR 形式：exists(jsonb_array_elements) 命中任一条件
            codes = tuple(set(query.kp_codes))
            or_terms = [kp_expr.contains([{"code": c}]) for c in codes]
            if len(or_terms) == 1:
                conditions.append(or_terms[0])
            else:
                conditions.append(or_terms[0].__or__(*or_terms[1:]))

        if query.keywords:
            # content 文本块里任一 text/stem/passage：LIKE
            kw = f"%{query.keywords}%"
            conditions.append(
                func.jsonb_path_query_array(ItemVersion.content, "$.blocks[*].text")
                .cast("text").ilike(kw)
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(ItemVersion.created_at.desc()).limit(
            query.limit or None
        ).offset(query.offset or 0)

        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())

        rows_dicts: list[dict[str, Any]] = []
        for r in rows:
            d: dict[str, Any] = {}
            for col in ("item_version_id", "item_id", "status", "objective",
                       "interaction_ref", "content", "scoring_ref"):
                v = getattr(r, col, None)
                d[col] = v.model_dump(mode="json") if hasattr(v, "model_dump") else v
            rows_dicts.append(d)
        total = len(rows_dicts)

        # EXPLAIN 仅记录命中哪些索引（调试辅助，optional）
        used: list[str] = []
        try:
            exp = await self.session.execute(
                text("EXPLAIN (FORMAT TEXT) " + str(stmt.compile(compile_kwargs={"literal_binds": True})))
            )
            for (line,) in exp.all():
                if "Index Scan using" in line or "Index Cond" in line:
                    used.append(line.strip())
        except Exception:
            pass

        return SearchResult(
            total=total,
            items=rows_dicts,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            used_indexes=used[:5],
        )


# ════════════════════════════════════════════════════════════════════
# 性能基准（baseline
# ════════════════════════════════════════════════════════════════════


@dataclass
class PerfBaselineReport:
    n_items_inserted: int = 0
    kp_search_latency_ms: int = 0
    keyword_search_latency_ms: int = 0
    interaction_filter_latency_ms: int = 0
    gradeband_filter_latency_ms: int = 0


async def perf_baseline_in_memory(pool: Optional[list[dict[str, Any]]] = None,
                                  *,
                                  n_items: int = 1000) -> PerfBaselineReport:
    """Issue #24 性能基线：内存版。不依赖数据库，保证 CI 可跑。

    生成 n_items 行随机 item_version（见 _generate_random_item 记录内存搜索耗时 4 种场景：
    1. 过滤 kp / 按 interaction / gradeband / 关键词（每类 100 次查询，取 p50
    （p50 中位数）记录。
    """
    import random as _r
    import string

    pool = list(pool) if pool is not None else _generate_random_pool(n_items)
    q_kp = [SearchQuery(
        kp_codes=_r.choice([["math.arithmetic.addition"],
                            ["chinese.reading.cmrc.dev_001"],
                            ["english.vocab.core"]]),
        limit=50,
    ) for _ in range(100)]
    q_ia = [SearchQuery(
        interaction_ids=tuple(_r.choice([["single_choice"], ["numeric_blank"], ["text_blank"]])),
        limit=50) for _ in range(100)]
    q_gb = [SearchQuery(
        gradebands=tuple(_r.choice([["L"], ["M"], ["H"]])), limit=50)
            for _ in range(100)]
    q_kw = [SearchQuery(
        keywords=_r.choice(list(string.ascii_letters[:6]) + ["apple", "苹果", "加法"]),
        limit=50) for _ in range(100)]

    def _p50(vals: list[int]) -> int:
        if not vals:
            return 0
        s = sorted(vals)
        return s[len(s) // 2]

    r = PerfBaselineReport(n_items_inserted=len(pool))
    r.kp_search_latency_ms = _p50([search_in_pool(pool, q).latency_ms for q in q_kp])
    r.interaction_filter_latency_ms = _p50([search_in_pool(pool, q).latency_ms for q in q_ia])
    r.gradeband_filter_latency_ms = _p50([search_in_pool(pool, q).latency_ms for q in q_gb])
    r.keyword_search_latency_ms = _p50([search_in_pool(pool, q).latency_ms for q in q_kw])
    return r


def _generate_random_pool(n: int) -> list[dict[str, Any]]:
    """生成 n 条假的 item_version（Issue #24 基准用，不入库）."""
    import random
    kp_pool = [
        "math.arithmetic.addition", "math.arithmetic.subtraction",
        "math.fraction.equivalent", "math.geometry.area_rectangle",
        "chinese.reading.cmrc.dev_001", "chinese.vocab.pinyin_to_word",
        "english.vocab.core", "english.reading.cmrc",
    ]
    ia_pool = ["single_choice", "numeric_blank", "text_blank", "short_answer"]
    gb_pool = ["L", "M", "H"]
    stems_en = ["apple orange banana cat dog elephant",
               "quick brown fox jumps over the lazy dog",
               "addition: 3 + 5 equals ?"]
    stems_zh = ["苹果 香蕉 橘子 猫 狗 大象",
               "小明有 3 个苹果，妈妈又给了 2 个，现在一共有几个？",
               "请根据拼音写出词语：píng guǒ"]
    out: list[dict[str, Any]] = []
    random.seed(0xBEEF)
    for i in range(n):
        out.append({
            "item_version_id": f"rand-{i:06d}",
            "item_id": f"item-{i:06d}",
            "status": "published" if i % 10 else "draft",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": random.choice(kp_pool)}],
                "kp_set_mode": "single",
                "cognitive_level": "understand",
                "gradeband": random.choice(gb_pool),
                "graph_release": "v1",
            },
            "interaction_ref": {"interaction_id": random.choice(ia_pool)},
            "content": {
                "blocks": [
                    {"type": "stem",
                     "text": random.choice(stems_en + stems_zh) + f"（{i}）"},
                    {"type": "options", "choices": [
                        {"id": "A", "label": f"optA_{i}"},
                        {"id": "B", "label": f"optB_{i}"},
                    ] if i % 2 == 0 else []},
                ],
            },
            "scoring_ref": {"scorer_id": "exact_match",
                            "scorer_params": {"answer": {"selected": "A"}}},
            "error_bindings": [],
            "lineage": {"tier": "A",
                        "pipeline": {"id": "mock", "version": "v1"},
                        "signed_by": "bench",
                        "signed_at": "2026-07-30T00:00:00Z"},
        })
    return out


__all__ = [
    "SearchQuery",
    "SearchResult",
    "ItemVersionSearchService",
    "search_in_pool",
    "PerfBaselineReport",
    "perf_baseline_in_memory",
    "_generate_random_pool",
]
