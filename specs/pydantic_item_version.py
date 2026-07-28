from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class KpRef(BaseModel):
    dimension: str
    code: str


class Objective(BaseModel):
    kp_set: List[KpRef]
    kp_set_mode: str
    cognitive_level: str
    gradeband: str
    graph_release: str


class InteractionRef(BaseModel):
    interaction_id: str
    interaction_params: Dict[str, Any]


class Content(BaseModel):
    blocks: List[Dict[str, Any]]


class ScoringRef(BaseModel):
    scorer_id: str
    scorer_params: Dict[str, Any]


class Lineage(BaseModel):
    tier: str
    pipeline: Dict[str, str]
    signed_by: str
    signed_at: str


class ItemVersionImport(BaseModel):
    item_version_id: str
    item_id: str
    status: str
    objective: Objective
    interaction_ref: InteractionRef
    content: Content
    scoring_ref: ScoringRef
    error_bindings: List[Dict[str, Any]]
    lineage: Lineage
