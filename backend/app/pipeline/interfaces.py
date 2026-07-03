from __future__ import annotations

from typing import Protocol

from app.schemas import (
    ExtractionCandidate,
    GraphPayload,
    ParsedQuestion,
    QueryRequest,
    SearchHit,
    SourceFragment,
)


class ParserAdapter(Protocol):
    name: str

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        """Convert an uploaded file to canonical source fragments."""


class LLMProvider(Protocol):
    name: str

    def extract_entities(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        """Extract candidate facts from canonical fragments."""

    def extract_relations(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        """Extract candidate relations from canonical fragments."""

    def parse_question(self, question: str) -> ParsedQuestion:
        """Convert a natural language question to a structured query."""

    def summarize_results(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        """Summarize retrieved evidence."""

    def generate_answer(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        """Generate a final answer from evidence."""


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for texts."""


class VectorStore(Protocol):
    def upsert(self, fragments: list[SourceFragment]) -> None:
        """Insert or update fragment vectors."""

    def delete(self, fragment_ids: list[str]) -> None:
        """Delete fragment vectors."""

    def search(self, query: str, top_k: int = 8) -> list[SearchHit]:
        """Search vectors by semantic similarity."""

    def hybrid_search(self, query: str, top_k: int = 8) -> list[SearchHit]:
        """Combine lexical and vector search."""


class GraphStore(Protocol):
    def get_graph(self, entity_id: str | None = None, depth: int = 2) -> GraphPayload:
        """Return a graph neighborhood."""

