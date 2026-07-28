"""§4.4 双向细目表约束 schema（T-W4-027）.

架构 v2 §4.4「测量(预留)」行：「双向细目表（内容×认知×题量×难度）= 单元格计数
约束」。本模块落地该 schema 的纯数据模型与校验，供 T-W4-028 CP-SAT 求解器
编译为约束、T-W4-029 测量卷产出做合规校验。

双向细目表（Two-Way Specification Table）：
- 第一维：内容（知识点编码 content_code，可任意层级深度的点分树形 code）
- 第二维：认知层级（Bloom 六级，与 Objective.cognitive_level 同口径）
- 每个单元格 {target_count, difficulty_min, difficulty_max}：
  - target_count：该单元格目标题数
  - difficulty_min/max：题目难度区间（p_correct 口径，越大越易；与
    item_param.params.difficulty、AssemblyProfile.target_p_correct_range
    同口径，避免单位混估）

为什么 difficulty 用 p_correct 而非「难度系数（越大越难）」：
- 平台既有参数体系（ctt.py / bayesian_shrinkage.py / item_param.params.difficulty）
  一致采用「p_correct = 答对率，越大越易」；
- 双向细目表的难度区间要与候选池 p_correct_prior 直接比较，同单位才能编译为
  CP-SAT 约束（T-W4-028）；
- 命名「difficulty」沿用任务卡原文，但语义= p_correct 区间（注释明示）。

校验规则（任务卡验收标准 §2）：
1. 全部单元格 target_count 之和 > 0（空表无组卷意义）
2. 单个单元格 difficulty_min ≤ difficulty_max（区间合法）
3. 维度编码存在性：content_code 必须存在于所引用图谱的 kp_node.code 集合
   （validate_against_graph 显式调用，不在构造期强制——构造期不知图谱范围）

序列化（任务卡验收标准 §3）：
- JSON / YAML 序列化无损（Pydantic model_dump / model_validate 往返；
  YAML 经 JSON 中转，pyyaml 安全 load）

宪法 A5/A7：本模块不 import 任何学科包/学段包（学科零特判）；
content_code 是字符串编码，由调用方从学科包图谱加载后传入。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# 认知层级六值（与 Objective.cognitive_level 同口径，避免单位混估）
CognitiveLevel = str  # 见下方 _COGNITIVE_LEVELS 校验
_COGNITIVE_LEVELS: frozenset[str] = frozenset(
    {"remember", "understand", "apply", "analyze", "evaluate", "create"}
)


class SpecCell(BaseModel):
    """双向细目表单元格：内容×认知 的目标题量与难度区间.

    Attributes:
        content_code: 知识点编码（任意层级点分树形 code，如
            "math.nal.decimal.compare"）；存在性由 SpecTable.validate_against_graph
            显式校验，构造期不强制。
        cognitive_level: 认知层级（Bloom 六级，与 Objective.cognitive_level 同集）。
        target_count: 该单元格目标题数（≥0；0 表示该单元格不要求题量）。
        difficulty_min: 题目难度下限（p_correct 口径，[0.0, 1.0]，越大越易）。
        difficulty_max: 题目难度上限（p_correct 口径，[0.0, 1.0]，越大越易）。
    """

    model_config = ConfigDict(extra="forbid")

    content_code: str = Field(min_length=1)
    cognitive_level: str
    target_count: int = Field(ge=0)
    difficulty_min: float = Field(ge=0.0, le=1.0)
    difficulty_max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_cognitive_level_domain(self) -> "SpecCell":
        if self.cognitive_level not in _COGNITIVE_LEVELS:
            raise ValueError(
                f"cognitive_level {self.cognitive_level!r} 越域；"
                f"合法域 {sorted(_COGNITIVE_LEVELS)}"
            )
        return self

    @model_validator(mode="after")
    def _check_difficulty_range(self) -> "SpecCell":
        # 任务卡验收标准 §2：单个单元格 difficulty_min ≤ difficulty_max
        if self.difficulty_min > self.difficulty_max:
            raise ValueError(
                f"单元格 [{self.content_code}/{self.cognitive_level}] "
                f"difficulty_min={self.difficulty_min} > "
                f"difficulty_max={self.difficulty_max}"
            )
        return self


class SpecTable(BaseModel):
    """双向细目表：单元格集合 + 引用元数据.

    一份 SpecTable = 一次测量卷的内容×认知×题量×难度目标分布。
    表本身只增不改（D1 风格，ORM 层物理强制；本 Pydantic 模型只负责 schema）。

    Attributes:
        spec_table_id: 表 id（与 ORM 主键一致；ULID 或语义 id）。
        spec_table_version: 表版本（同 id 改版需递增版本；D1 版本账）。
        gradeband: 学段（L/M/H，与 AssemblyProfile.gradeband 同集）。
        graph_release: 引用的知识图谱 release id（维度编码存在性校验依据）。
        cells: 单元格列表；(content_code, cognitive_level) 唯一。
        total_count: 派生字段，所有 cells 的 target_count 之和；构造期校验 >0。
    """

    model_config = ConfigDict(extra="forbid")

    spec_table_id: str = Field(min_length=1)
    spec_table_version: str = Field(min_length=1)
    gradeband: str
    graph_release: str = Field(min_length=1)
    cells: list[SpecCell] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_gradeband_domain(self) -> "SpecTable":
        if self.gradeband not in {"L", "M", "H"}:
            raise ValueError(
                f"gradeband {self.gradeband!r} 越域；合法域 ['L', 'M', 'H']"
            )
        return self

    @model_validator(mode="after")
    def _check_total_count_positive(self) -> "SpecTable":
        # 任务卡验收标准 §2：全部单元格 target_count 之和 > 0
        total = self.total_count
        if total <= 0:
            raise ValueError(
                f"细目表所有单元格 target_count 之和 = {total}，必须 > 0"
            )
        return self

    @model_validator(mode="after")
    def _check_cell_uniqueness(self) -> "SpecTable":
        # 同一 (content_code, cognitive_level) 唯一：否则单元格语义重叠，组卷
        # 时无法判定题量归属。允许同一 content_code 在不同 cognitive_level 出现。
        seen: set[tuple[str, str]] = set()
        dupes: list[str] = []
        for c in self.cells:
            key = (c.content_code, c.cognitive_level)
            if key in seen:
                dupes.append(f"{c.content_code}/{c.cognitive_level}")
            seen.add(key)
        if dupes:
            raise ValueError(
                f"细目表单元格 (content_code, cognitive_level) 重复：{dupes}"
            )
        return self

    @property
    def total_count(self) -> int:
        """所有单元格 target_count 之和（派生量）."""
        return sum(c.target_count for c in self.cells)

    # ────────────────────────────────────────────────────────────────
    # 维度编码存在性校验（任务卡验收标准 §2：维度编码存在性校验，引用知识图谱）
    # ────────────────────────────────────────────────────────────────

    def validate_against_graph(
        self, valid_content_codes: Iterable[str]
    ) -> list[str]:
        """校验所有 cell.content_code 存在于给定图谱编码集合.

        为什么不在构造期强制：构造期 SpecTable 不持有图谱快照（图谱是独立
        版本化资产，graph_release 字段只是引用指针）；调用方在组卷前以
        ``graph_release`` 对应的 kp_node.code 集合调用本方法做存在性校验。
        本方法与 ORM 层 FK 不同：图谱节点本身是版本化的（graph_release），
        跨版本存在性需运行期校验，无法用静态 FK 表达。

        Args:
            valid_content_codes: 图谱 release 中的合法 content_code 集合
                （通常来自 KpNode.code 列查询）。

        Returns:
            不存在的 content_code 列表（空列表表示全部存在）。

        Raises:
            ValueError: 若存在未知编码（含未知编码列表详情）。
        """
        valid = set(valid_content_codes)
        unknown = sorted({c.content_code for c in self.cells if c.content_code not in valid})
        if unknown:
            raise ValueError(
                f"细目表引用了图谱 {self.graph_release!r} 中不存在的 content_code："
                f"{unknown}（共 {len(unknown)} 个）；请检查 graph_release 或 cells"
            )
        return unknown

    def cell_at(self, content_code: str, cognitive_level: str) -> Optional[SpecCell]:
        """按 (content_code, cognitive_level) 取单元格；不存在返回 None."""
        for c in self.cells:
            if c.content_code == content_code and c.cognitive_level == cognitive_level:
                return c
        return None

    # ────────────────────────────────────────────────────────────────
    # 序列化（任务卡验收标准 §3：JSON/YAML 无损）
    # ────────────────────────────────────────────────────────────────

    def to_json(self) -> str:
        """序列化为 JSON 字符串（确定性：sort_keys，ensure_ascii=False）.

        为什么 sort_keys=True：序列化无损 + 跨进程一致（指纹/比对友好），
        与 AssemblyProfile.digest() 同手法。
        """
        import json

        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str) -> "SpecTable":
        """从 JSON 字符串反序列化（与 to_json 互逆）."""
        import json

        return cls.model_validate(json.loads(data))

    def to_yaml(self) -> str:
        """序列化为 YAML 字符串（经 JSON 中转，保留类型；sort_keys 确定性）.

        为什么经 JSON 中转：Pydantic model_dump 后含 Python 类型（如 int/float），
        pyyaml dump 时 int 1 可能被识为 '1' 字符串的不确定性链路；JSON dump
        先归一为 JSON 类型（int/float/str/list/dict/null），yaml.safe_load 回来
        仍是同型，无损往返。与既有项目 yaml 用法（registries 加载）一致。
        """
        import json

        json_obj = json.loads(self.to_json())
        return yaml.safe_dump(json_obj, sort_keys=True, allow_unicode=True)

    @classmethod
    def from_yaml(cls, data: str) -> "SpecTable":
        """从 YAML 字符串反序列化（与 to_yaml 互逆）."""
        obj = yaml.safe_load(data)
        return cls.model_validate(obj)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（给 ORM 层 JSONB 落库用；与 model_dump(mode='json') 一致）."""
        return self.model_dump(mode="json")


__all__ = [
    "CognitiveLevel",
    "SpecCell",
    "SpecTable",
]
