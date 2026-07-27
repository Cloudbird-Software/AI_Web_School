"""干扰项规则与错误绑定生成器（T-W2-003）。

按 distractor_rules（DSL 第六块，T-W2-001 DistractorRule）生成显式选项列表，
并为每个选项绑定 error_type_id。两种规则：
  - deterministic：用安全表达式求值器（T-W2-002）求值 expression 得干扰项值；
  - corpus_sample：返回带 corpus_ref 的占位（等待 B 线语料装配，T-W2-017+）。

碰撞检查（验收 §3）：生成的干扰项值与正解值（answer_value）相等时抛
DistractorCollisionError；抽样场景可经 allow_collision=True 配置容差
（仍记录 collision 标记，B 线接入后再行处置）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from src.core.instantiation.distractor.generator import (
    DistractorCollisionError,
    DistractorOption,
    DistractorResult,
    generate_distractors,
)

__all__ = [
    "DistractorCollisionError",
    "DistractorOption",
    "DistractorResult",
    "generate_distractors",
]
