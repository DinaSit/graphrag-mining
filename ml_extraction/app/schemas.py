"""Копии контрактных моделей из backend/app/schemas.py (зона fullstack).

Сервис намеренно самодостаточен и не импортирует backend: контракт /extract
зафиксирован структурой ExtractRequest/ExtractResponse, как в ml_mock.
При изменении схем в backend — синхронизировать вручную.
"""
from typing import Any

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    document_id: str
    version_id: str
    fragment_id: str
    page: int = 1
    section: str | None = None
    table: str | None = None
    quote: str | None = None


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
    status: str = "pending_review"
    review_note: str | None = None


class ExtractRequest(BaseModel):
    fragments: list[SourceFragment]


class ExtractResponse(BaseModel):
    candidates: list[ExtractionCandidate]


class WebAnswerRequest(BaseModel):
    question: str


class WebAnswerResponse(BaseModel):
    found: bool
    answer: str | None = None
    url: str | None = None


class EmbedRequest(BaseModel):
    texts: list[str]
    kind: str = Field(default="doc", description="doc — индексация фрагментов, query — поисковые запросы")


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimensions: int
    model: str
