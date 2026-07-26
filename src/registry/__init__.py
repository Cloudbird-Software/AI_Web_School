"""双注册表单例入口（T-W1-004）。

加载 specs/contracts/registries/ 下的 interaction.yaml 与 scorer.yaml，
Pydantic 校验后以不可变单例暴露（宪法 D4 双类型注册表纪律）。

为什么单例：注册表是平台级类型系统（架构 v2 §2.3），加载一次即可全局复用；
单例 + frozen Pydantic 模型保证运行时不可变，禁止热改注册表绕过校验门。

宪法 A5/X6：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.registry.loader import (
    DEFAULT_INTERACTION_PATH,
    DEFAULT_SCORER_PATH,
    InteractionRegistry,
    InteractionType,
    ScorerRegistry,
    ScorerType,
    load_interaction_registry,
    load_scorer_registry,
    validate_cross_references,
)

# 模块级单例缓存：首次访问时加载并校验，后续直接返回
# 为什么用模块级而非类属性：避免实例化多份注册表对象绕过单例约束
_interaction_registry: Optional[InteractionRegistry] = None
_scorer_registry: Optional[ScorerRegistry] = None


def get_interaction_registry(
    interaction_path: Optional[Path] = None,
    scorer_path: Optional[Path] = None,
    *,
    reload: bool = False,
) -> InteractionRegistry:
    """获取交互类型注册表单例。

    首次调用时加载 interaction.yaml + scorer.yaml 并执行交叉引用校验
    （宪法 D4）。后续调用直接返回缓存实例。

    Args:
        interaction_path: 自定义 interaction.yaml 路径（主要为测试用）。
            None 时使用默认 specs/contracts/registries/interaction.yaml。
        scorer_path: 自定义 scorer.yaml 路径（主要为测试用）。
            None 时使用默认路径。
        reload: True 时强制重新加载（测试隔离用；生产路径不应使用）。

    Returns:
        InteractionRegistry: 不可变单例。

    Raises:
        FileNotFoundError: 契约文件不存在。
        pydantic.ValidationError: schema 校验失败。
        ValueError: 交叉引用校验失败。
    """
    global _interaction_registry, _scorer_registry
    if _interaction_registry is None or reload:
        _interaction_registry = load_interaction_registry(
            interaction_path or DEFAULT_INTERACTION_PATH
        )
        # 交叉引用校验需要 scorer registry 同时就绪
        if _scorer_registry is None or reload:
            _scorer_registry = load_scorer_registry(
                scorer_path or DEFAULT_SCORER_PATH
            )
        validate_cross_references(_interaction_registry, _scorer_registry)
    return _interaction_registry


def get_scorer_registry(
    interaction_path: Optional[Path] = None,
    scorer_path: Optional[Path] = None,
    *,
    reload: bool = False,
) -> ScorerRegistry:
    """获取评分器注册表单例。

    首次调用时加载 scorer.yaml + interaction.yaml 并执行交叉引用校验。
    与 get_interaction_registry 共享同一份缓存（任一首次访问均会触发
    双注册表加载与校验），保证交叉引用闭合始终成立。

    Args:
        interaction_path: 自定义 interaction.yaml 路径（主要为测试用）。
        scorer_path: 自定义 scorer.yaml 路径（主要为测试用）。
        reload: True 时强制重新加载（测试隔离用）。

    Returns:
        ScorerRegistry: 不可变单例。

    Raises:
        FileNotFoundError: 契约文件不存在。
        pydantic.ValidationError: schema 校验失败。
        ValueError: 交叉引用校验失败。
    """
    global _interaction_registry, _scorer_registry
    if _scorer_registry is None or reload:
        _scorer_registry = load_scorer_registry(
            scorer_path or DEFAULT_SCORER_PATH
        )
        if _interaction_registry is None or reload:
            _interaction_registry = load_interaction_registry(
                interaction_path or DEFAULT_INTERACTION_PATH
            )
        validate_cross_references(_interaction_registry, _scorer_registry)
    return _scorer_registry


def reset_registries() -> None:
    """清空单例缓存（测试隔离专用）。

    生产路径不应调用：注册表加载为幂等操作，重置仅为单测隔离提供入口。
    """
    global _interaction_registry, _scorer_registry
    _interaction_registry = None
    _scorer_registry = None


__all__ = [
    # 模型
    "InteractionRegistry",
    "InteractionType",
    "ScorerRegistry",
    "ScorerType",
    # 加载函数
    "load_interaction_registry",
    "load_scorer_registry",
    "validate_cross_references",
    # 单例入口
    "get_interaction_registry",
    "get_scorer_registry",
    "reset_registries",
    # 路径常量
    "DEFAULT_INTERACTION_PATH",
    "DEFAULT_SCORER_PATH",
]
