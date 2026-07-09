from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# Типы узлов доменной онтологии (domain/default/ontology.yaml, владелец —
# инженер знаний). Единственный источник для whitelist меток Neo4j
# (persistence.Neo4jSink) и схемы Query Planner (query_parsing).
ONTOLOGY_LABELS = (
    "Material", "Process", "Equipment", "Property", "NumericParameter", "Condition",
    "Experiment", "Publication", "Expert", "Facility", "Result", "Recommendation", "Region",
)


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
    # Ключ PDF-превью в MinIO (<storage_object>.preview.pdf) для DOCX/PPTX,
    # конвертация через LibreOffice; None — превью нет (не создано/сбой)
    preview_object: str | None = None
    created_at: str
    # Скрытый документ полностью выпадает из ответов (факты, поиск, граф),
    # но данные не удаляются и Neo4j не перестраивается
    hidden: bool = False
    # Эвристические признаки документа (см. pipeline/document_traits.py);
    # None — признак ещё не вычислен
    is_scientific: bool | None = None
    origin: str | None = None  # "ru" | "foreign"
    # Год издания из ТЕКСТА документа (не из даты файла); best-effort эвристика
    # extract_publication_year. None — год не найден или ещё не вычислен
    year: int | None = None


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
    hypotheses: list[str] = Field(default_factory=list)
    # УСТАРЕЛО (совместимость): веб-поиск вынесен в отдельный контур
    # POST /web/answer и больше не заполняет это поле в /ask и /ask/stream
    web_answer: dict[str, Any] | None = None
    # Обе LLM недоступны: человекочитаемая причина; ответ собран без генерации
    llm_error: str | None = None
    # Семантический поиск bge-m3 — работает без LLM, показывается пользователю
    search_hits: list[SearchHit] = Field(default_factory=list)
    # Нашлись ли прямые факты (не гипотезы); по False UI может предложить
    # пользователю независимый веб-поиск (POST /web/answer)
    has_direct_facts: bool = False
    # Смежные факты: полезный контекст, но не прямой ответ на вопрос.
    related_experiments: list[ExperimentRow] = Field(default_factory=list)
    related_sources: list[SourceRef] = Field(default_factory=list)
    related_graph: GraphPayload = Field(default_factory=GraphPayload)
    evidence_status: str = "none"  # direct / partial / none
    # Вопрос не про базу знаний (смолток/оффтоп): полный пайплайн, включая
    # LLM, граф и веб-поиск, не запускался — ответ мгновенный
    offtopic: bool = False
    # Какой веткой собран ответ: "fast" — классический RAG (только семантический
    # поиск, без LLM-планировщика и графа), "full" — полный пайплайн
    pipeline_mode: str = "full"
    # Доля файлов с is_scientific=True среди файлов, на которые ссылаются
    # СНОСКИ ответа (0..1, 2 знака); без цитат — по итоговому списку sources;
    # None — ни у одного файла-источника признак не вычислен
    scientific_share: float | None = None


class SearchHit(BaseModel):
    fragment_id: str
    score: float
    text: str
    source: SourceRef
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryCondition(BaseModel):
    parameter: str
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None


class QueryEntity(BaseModel):
    type: str
    name: str


class ParsedQuestion(BaseModel):
    material: str | None = None
    property: str | None = None
    process: str | None = None
    equipment: str | None = None
    region: str | None = None
    entities: list[QueryEntity] = Field(default_factory=list)
    conditions: list[QueryCondition] = Field(default_factory=list)
    target: QueryCondition | None = None


class DocumentVisibilityRequest(BaseModel):
    hidden: bool


class RejectFactRequest(BaseModel):
    note: str | None = None


class OntologyCandidate(BaseModel):
    id: str
    proposed_name: str
    kind: str
    examples: list[str] = Field(default_factory=list)
    similar_existing_types: list[str] = Field(default_factory=list)
    confidence: float
    status: CandidateStatus = CandidateStatus.pending_review


# SearchHit объявлен ниже QueryResponse: форвард-ссылка резолвится после определения
QueryResponse.model_rebuild()
