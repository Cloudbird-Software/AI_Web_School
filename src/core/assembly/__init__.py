"""§4.4 组装域 v1（T-W3-assembly S1/S2）.

组卷引擎 = 约束集四维编译（profile）+ 候选筛选（candidates）
+ 确定性预算装填求解（solver）+ 曝光账本双轨（exposure）。

- R-Z-01：三用途同一引擎同一题库，差异收敛为版本化 Profile；
  确定性 = 快照 id + Profile 版本 + 种子。
- R-Z-02：题量/知识点配比/目标正确率区间/序列梯度单调/曝光互斥/题组≤6。
- R-Z-03：诊断 Profile 孤立题强制、每知识点≥3、多点关系声明核验；
  已知冲突（约20题×每点≥3）编译期软目标化裁决并留档。
- §4.4 铁律：不可行返回结构化冲突原因（InfeasibleError.report），
  禁止静默放松。

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）；
学科 overlay 由调用方以 dict 传入。
"""
from src.core.assembly.candidates import (
    CandidateItem,
    candidate_from_serving_row,
    load_candidates,
)
from src.core.assembly.exposure import (
    queue_exposed_item_version_ids,
    queue_exposed_template_version_ids,
    record_paper_exposures,
    record_student_exposures,
    student_exposed_item_version_ids,
    student_exposed_template_version_ids,
)
from src.core.assembly.profile import (
    MAX_ITEMS_PER_GROUP,
    Adjudication,
    AssemblyProfile,
    ConstraintSet,
    ContentMixRule,
    ItemCountRule,
    KpQuota,
    ProfileConflictError,
    compile_profile,
    diagnosis_profile,
)
from src.core.assembly.solver import (
    AssemblyResult,
    ConflictReason,
    ConflictReport,
    InfeasibleError,
    assemble,
)

__all__ = [
    # profile
    "AssemblyProfile",
    "ConstraintSet",
    "ItemCountRule",
    "KpQuota",
    "ContentMixRule",
    "Adjudication",
    "ProfileConflictError",
    "compile_profile",
    "diagnosis_profile",
    "MAX_ITEMS_PER_GROUP",
    # candidates
    "CandidateItem",
    "candidate_from_serving_row",
    "load_candidates",
    # solver
    "assemble",
    "AssemblyResult",
    "InfeasibleError",
    "ConflictReport",
    "ConflictReason",
    # exposure
    "queue_exposed_item_version_ids",
    "queue_exposed_template_version_ids",
    "student_exposed_item_version_ids",
    "student_exposed_template_version_ids",
    "record_paper_exposures",
    "record_student_exposures",
]
