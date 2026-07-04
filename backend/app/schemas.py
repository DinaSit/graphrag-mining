from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class CandidateStatus(str, Enum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class SourceRef(BaseModel):
    document_id: str
    version_id: str
    fragment_id: str
    page: int = 1
    section: str | None = None
    table: str | None = None
    quote: str | None = None


class DocumentRecord(BaseModel):
    id: str
    filename: str
    document_type: str
    source_label: str | None = None
    access_level: str = "uploaded"
    checksum: str
    current_version_id: str
    status: DocumentStatus = DocumentStatus.pending
    element_count: int = 0
    storage_bucket: str | None = None
    storage_object: str | None = None
    storage_uri: str | None = None
    created_at: str


class DocumentVersion(BaseModel):
    id: str
    document_id: str
    checksum: str
    version_number: int
    status: DocumentStatus = DocumentStatus.pending
    parser: str = "mock"
    created_at: str


class SourceFragment(BaseModel):
    id: str
    document_id: str
    version_id: str
    page: int = 1
    element_type: str = "paragraph"
    section: str | None = None
    text: str
    normalized_text: str
    bbox: list[float] | None = None
    image_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractionCandidate(BaseModel):
    id: str
    type: str = "Claim"
    payload: dict[str, Any]
    source: SourceRef | None = None
    confidence: float = 0.0
    status: CandidateStatus = CandidateStatus.pending_review
    review_note: str | None = None


class Fact(BaseModel):
    id: str
    candidate_id: str | None = None
    material: str
    material_id: str
    experiment_id: str
    sample: str
    process: str
    temperature_c: float | None = None
    duration_h: float | None = None
    property: str
    effect_direction: str
    effect_value: float | None = None
    effect_unit: str | None = None
    result_value: float | None = None
    result_unit: str | None = None
    lab: str
    team: str
    equipment: str | None = None
    confidence: float
    status: str = "approved"
    is_hypothesis: bool = False
    # id фактов с противоположным эффектом по тому же материалу и свойству;
    # оба факта сохраняются, статус conflicting помечает зону разногласий
    conflicts_with: list[str] = Field(default_factory=list)
    source: SourceRef


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str


class GraphPayload(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class QueryFilters(BaseModel):
    materials: list[str] = Field(default_factory=list)
    properties: list[str] = Field(default_factory=list)
    laboratories: list[str] = Field(default_factory=list)
    confidence_min: float = 0.0


class QueryRequest(BaseModel):
    question: str
    filters: QueryFilters = Field(default_factory=QueryFilters)
    include_hypotheses: bool = False
    confidence_min: float = 0.0


class ExperimentRow(BaseModel):
    experiment_id: str
    material: str
    sample: str
    process: str
    temperature_c: float | None
    duration_h: float | None
    property: str
    effect: str
    lab: str
    confidence: float
    source: SourceRef


class QueryResponse(BaseModel):
    summary: str
    experiments: list[ExperimentRow]
    sources: list[SourceRef]
    graph: GraphPayload
    contradictions: list[str]
    gaps: list[str]
    confidence: float
    # Ответ из внешних источников, когда в базе знаний данных нет.
    # Не верифицирован и в граф не записывается: {"answer": str, "url": str}
    web_answer: dict[str, Any] | None = None
    hypotheses: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str
    filters: QueryFilters = Field(default_factory=QueryFilters)
    top_k: int = 8


class SearchHit(BaseModel):
    fragment_id: str
    score: float
    text: str
    source: SourceRef
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    hits: list[SearchHit]


class QueryCondition(BaseModel):
    parameter: str
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None


class QueryEntity(BaseModel):
    type: str
    name: str


class ParsedQuestion(BaseModel):
    intent: str = "compare_experiments"
    material: str | None = None
    property: str | None = None
    temperature_min: float | None = None
    temperature_max: float | None = None
    unit: str = "C"
    process: str | None = None
    equipment: str | None = None
    region: str | None = None
    year_from: int | None = None
    entities: list[QueryEntity] = Field(default_factory=list)
    conditions: list[QueryCondition] = Field(default_factory=list)
    target: QueryCondition | None = None
    keywords: list[str] = Field(default_factory=list)


class OntologyCandidate(BaseModel):
    id: str
    proposed_name: str
    kind: str
    examples: list[str] = Field(default_factory=list)
    similar_existing_types: list[str] = Field(default_factory=list)
    confidence: float
    status: CandidateStatus = CandidateStatus.pending_review
