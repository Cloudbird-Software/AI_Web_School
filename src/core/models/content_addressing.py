"""内容寻址纯函数（T-W1-003 / 契约 §3 公式一/二/三）.

宪法 D3：内容寻址确定性——同一输入必产生同一输出，是可复现与去重提示的物理基础。
本模块仅提供三个无副作用纯函数，不依赖 DB / IO；任何调用方对同一组参数应得同一 id。

为什么不用 hashlib.blake2b 或更快哈希：SHA-256 是契约 §3 默认承诺的摘要算法
（digest 命名以 sha256: 前缀对外暴露），跨语言/跨实现兼容性优于 blake 系列。

为什么规范化 JSON 用 sort_keys + separators=(",", ":")：键序固定避免 dict 顺序
漂移；最紧分隔符避免空格空白差异；ensure_ascii=False 让中文字符以 UTF-8 字节
直接落摘要，与 PG 中 JSONB 的存储语义一致。

返回值约定：所有函数返回 "sha256:" + hex Digest 的字符串，与契约 §2.1/§2.2
"sha256:..." 字面量示例一致。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化（D3 可复现的基础）.

    Args:
        obj: 任意可被 json.dumps 序列化的 Python 对象。

    Returns:
        规范化后的 JSON 字符串（键序升序、无多余空白、UTF-8 直出）。

    Notes:
        - 嵌套 dict 内部键序由 sort_keys=True 递归保证。
        - 数字精度由调用方负责（contract §3 公式一要求 normalized_params 用
          定点/分数运算；本函数仅做序列化，不做数值规范化）。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sha256_hex(payload: str) -> str:
    """计算 SHA-256 hex 摘要，并加 sha256: 前缀。"""
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────
# 公式一：A/B 级实例 item_version_id
# ────────────────────────────────────────────────────────────────────

def compute_instance_id(
    template_version_digest: str,
    normalized_params: dict,
    pack_digest: str,
    engine_digest: str,
    corpus_digests: list[str],
    locale: str,
) -> str:
    """契约 §3 公式一：A/B 级实例的 item_version_id.

    H( template_version_digest, normalized_params, pack_digest,
       engine_digest, corpus_digests, locale )

    Args:
        template_version_digest: 母题版本摘要（sha256:...）。
        normalized_params: 规范化实例化参数（定点/分数运算结果，避免浮点漂移）。
        pack_digest: 所属学科包摘要。
        engine_digest: 实例化引擎摘要。
        corpus_digests: 语料库版本摘要链（被本实例引用的语料版本，按引用顺序）。
        locale: 语言/地区（zh-CN / en-US 等）。

    Returns:
        "sha256:" + hex digest。

    Notes:
        - 字段顺序固定为 (tvd, np, pd, ed, cd, l)；任何字段变化必须导致 id 变化。
        - corpus_digests 作为 list 进入规范化 JSON，元素顺序影响 id（语料版本链
          的顺序是谱系的一部分）。
    """
    canonical = _canonical_json({
        "tvd": template_version_digest,
        "np": normalized_params,
        "pd": pack_digest,
        "ed": engine_digest,
        "cd": corpus_digests,
        "l": locale,
    })
    return _sha256_hex(canonical)


# ────────────────────────────────────────────────────────────────────
# 公式二：C/D 级 item_version_id（规范化内容快照哈希）
# ────────────────────────────────────────────────────────────────────

def compute_canonical_item_version_id(
    objective: dict,
    interaction_ref: dict,
    content: dict,
    scoring_ref: dict,
    error_bindings: dict,
    locale: str,
) -> str:
    """契约 §3 公式二：C/D 级 item_version_id.

    H( canonical( objective, interaction_ref, content, scoring_ref,
                  error_bindings ), locale )

    Args:
        objective: 知识标注集（契约 §2.2.1）。
        interaction_ref: 交互类型 + 交互参数。
        content: 题面语义 AST + 素材版本引用。
        scoring_ref: 评分器 + 评分参数。
        error_bindings: 选项/评分维度 → 错误类型 + 置信规则。
        locale: 语言/地区。

    Returns:
        "sha256:" + hex digest。

    Notes:
        - error_bindings 形参类型保持为 dict 与 content/writer.py 现有调用对齐
          （实际数据可以是 list[dict]；canonical JSON 对 dict 与 list 均规范化）。
          签名以 dict 标注仅为类型提示稳定性，不限制运行时类型。
        - 同一内容（六块完全一致 + 同 locale）必得同一 id（D3）；重复命题/粘贴
          产生同 id，入库时作去重提示而非拒绝。
    """
    canonical = _canonical_json({
        "o": objective,
        "ir": interaction_ref,
        "c": content,
        "sr": scoring_ref,
        "eb": error_bindings,
        "l": locale,
    })
    return _sha256_hex(canonical)


# ────────────────────────────────────────────────────────────────────
# 公式三：material_version_id / corpus_version_id
# ────────────────────────────────────────────────────────────────────

def compute_material_version_id(content_digest: str) -> str:
    """契约 §3 公式三：素材/语料版本的 id.

    H( content_digest )

    为什么直接对 content_digest 取哈希而非返回原值：调用方可能传入任意对象存储
    引用（如 "minio:materials/sha256:abc"），统一再哈希一次保证返回值是规范
    的 sha256:hex 形式，与公式一/二返回值格式一致，便于下游统一处理。

    Args:
        content_digest: 对象存储内容哈希（或带前缀的引用）。

    Returns:
        "sha256:" + hex digest。
    """
    return _sha256_hex(content_digest)


__all__ = [
    "compute_instance_id",
    "compute_canonical_item_version_id",
    "compute_material_version_id",
]
