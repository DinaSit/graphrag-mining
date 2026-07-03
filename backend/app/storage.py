from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.file_storage import MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.normalization import DomainNormalizer
from app.pipeline.parsers import choose_parser
from app.pipeline.providers import DeterministicEmbeddingProvider, MockLLMProvider, RemoteExtractionProvider
from app.pipeline.validation import validate_candidate_numbers
from app.schemas import (
    CandidateStatus,
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    ExtractionCandidate,
    Fact,
    GraphEdge,
    GraphNode,
    GraphPayload,
    OntologyCandidate,
    SearchHit,
    SourceRef,
    SourceFragment,
)


AUTO_APPROVE_THRESHOLD = 0.85


class SourceRequiredError(ValueError):
    pass


class ApplicationStore:
    def __init__(
        self,
        domain_dir: Path,
        postgres_sink: PostgresSink | None = None,
        graph_sink: Neo4jSink | None = None,
        file_storage: MinioFileStorage | None = None,
        extraction_service_url: str | None = None,
    ):
        self.domain_dir = domain_dir
        self.normalizer = DomainNormalizer(domain_dir)
        self.llm = RemoteExtractionProvider(extraction_service_url) if extraction_service_url else MockLLMProvider()
        self.embedder = DeterministicEmbeddingProvider()
        self.postgres_sink = postgres_sink
        self.graph_sink = graph_sink
        self.file_storage = file_storage
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.fragments: dict[str, SourceFragment] = {}
        self.candidates: dict[str, ExtractionCandidate] = {}
        self.facts: dict[str, Fact] = {}
        self.ontology_candidates: dict[str, OntologyCandidate] = {}
        self.fragment_vectors: dict[str, list[float]] = {}

    def add_source_fragment(self, fragment: SourceFragment) -> None:
        self.fragments[fragment.id] = fragment
        self._persist_fragments([fragment])

    def ingest_document(
        self,
        filename: str,
        content: bytes,
        document_type: str | None = None,
        source_label: str | None = None,
        access_level: str = "uploaded",
    ) -> DocumentRecord:
        checksum = hashlib.sha256(content).hexdigest()
        duplicate = next((doc for doc in self.documents.values() if doc.checksum == checksum), None)
        if duplicate:
            return duplicate
        if self.postgres_sink:
            persisted_duplicate = self.postgres_sink.get_document_by_checksum(checksum)
            if persisted_duplicate:
                document, version = persisted_duplicate
                self.documents[document.id] = document
                self.versions[version.id] = version
                return document

        document_id = f"doc-{uuid4().hex[:10]}"
        version_id = f"{document_id}-v1"
        now = _now()
        doc_type = document_type or Path(filename).suffix.lstrip(".") or "text"
        document = DocumentRecord(
            id=document_id,
            filename=filename,
            document_type=doc_type,
            source_label=source_label,
            access_level=access_level,
            checksum=checksum,
            current_version_id=version_id,
            status=DocumentStatus.processing,
            created_at=now,
        )
        version = DocumentVersion(
            id=version_id,
            document_id=document_id,
            checksum=checksum,
            version_number=1,
            status=DocumentStatus.processing,
            parser="auto",
            created_at=now,
        )
        self.documents[document_id] = document
        self.versions[version_id] = version
        if self.file_storage:
            stored = self.file_storage.put_document(document_id, version_id, filename, content)
            if stored:
                document.storage_bucket = stored.bucket
                document.storage_object = stored.object_name
                document.storage_uri = stored.uri
        self._persist_document(document, version)

        parser = choose_parser(filename)
        fragments = parser.parse(document_id, version_id, filename, content)
        version.parser = parser.name
        for fragment in fragments:
            self.fragments[fragment.id] = fragment
        self._persist_fragments(fragments)

        candidates = self.llm.extract_entities(fragments)
        for candidate in candidates:
            self.add_candidate(candidate)

        self.index_fragments(fragments)
        document.status = DocumentStatus.completed
        document.element_count = len(fragments)
        version.status = DocumentStatus.completed
        self._persist_document(document, version)
        return document

    def add_candidate(self, candidate: ExtractionCandidate) -> ExtractionCandidate:
        if candidate.source:
            source_text = candidate.source.quote or self.fragments.get(candidate.source.fragment_id, SourceFragment(
                id=candidate.source.fragment_id,
                document_id=candidate.source.document_id,
                version_id=candidate.source.version_id,
                text="",
                normalized_text="",
            )).text
            candidate.payload["number_validation"] = validate_candidate_numbers(candidate.payload, source_text)
        if candidate.confidence >= AUTO_APPROVE_THRESHOLD:
            candidate.status = CandidateStatus.approved
        elif candidate.confidence < 0.60:
            candidate.status = CandidateStatus.rejected
            candidate.review_note = "Confidence below approval threshold"
        self.candidates[candidate.id] = candidate
        self._persist_candidate(candidate)
        if candidate.status == CandidateStatus.approved:
            self.approve_candidate(candidate.id)
        return candidate

    def approve_candidate(self, candidate_id: str) -> Fact:
        candidate = self.candidates[candidate_id]
        if candidate.source is None:
            raise SourceRequiredError("Факт не может быть утвержден без ссылки на source fragment.")
        candidate.status = CandidateStatus.approved
        fact = self._fact_from_candidate(candidate)
        self.facts[fact.id] = fact
        self._persist_candidate(candidate)
        self._persist_fact(fact)
        return fact

    def reject_candidate(self, candidate_id: str, note: str | None = None) -> ExtractionCandidate:
        candidate = self.candidates[candidate_id]
        candidate.status = CandidateStatus.rejected
        candidate.review_note = note
        self._persist_candidate(candidate)
        return candidate

    def index_fragments(self, fragments: list[SourceFragment]) -> None:
        vectors = self.embedder.embed([fragment.normalized_text for fragment in fragments])
        new_vectors: dict[str, list[float]] = {}
        for fragment, vector in zip(fragments, vectors):
            self.fragment_vectors[fragment.id] = vector
            new_vectors[fragment.id] = vector
        if self.postgres_sink:
            self.postgres_sink.upsert_vectors(new_vectors, self.embedder.name)

    def search(self, query: str, top_k: int = 8) -> list[SearchHit]:
        query_vector = self.embedder.embed([query])[0]
        hits: list[SearchHit] = []
        query_terms = set(query.lower().replace("ё", "е").split())
        for fragment_id, vector in self.fragment_vectors.items():
            fragment = self.fragments[fragment_id]
            semantic = _cosine(query_vector, vector)
            lexical = sum(1 for term in query_terms if term and term in fragment.normalized_text) / max(len(query_terms), 1)
            score = semantic * 0.72 + lexical * 0.28
            if score <= 0:
                continue
            hits.append(
                SearchHit(
                    fragment_id=fragment.id,
                    score=round(score, 4),
                    text=fragment.text,
                    source=SourceRef(
                        document_id=fragment.document_id,
                        version_id=fragment.version_id,
                        fragment_id=fragment.id,
                        page=fragment.page,
                        section=fragment.section,
                        quote=fragment.text[:220],
                    ),
                    metadata=fragment.metadata,
                )
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]

    def get_graph(self, facts: list[Fact] | None = None, entity_id: str | None = None) -> GraphPayload:
        selected = facts if facts is not None else list(self.facts.values())
        if entity_id:
            selected = [
                fact
                for fact in selected
                if entity_id in {fact.material_id, fact.experiment_id, fact.id, fact.source.fragment_id}
            ]
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}

        def node(node_id: str, label: str, node_type: str, **data: Any) -> None:
            nodes.setdefault(node_id, GraphNode(id=node_id, label=label, type=node_type, data=data))

        def edge(source: str, target: str, label: str) -> None:
            edge_id = f"{source}-{label}-{target}"
            edges.setdefault(edge_id, GraphEdge(id=edge_id, source=source, target=target, label=label))

        for fact in selected:
            claim_id = fact.id
            property_id = f"property-{_slug(fact.property)}"
            effect_id = f"effect-{fact.effect_direction}-{fact.experiment_id}"
            source_id = fact.source.fragment_id
            node(fact.material_id, fact.material, "Material")
            node(fact.experiment_id, fact.experiment_id, "Experiment", temperature_c=fact.temperature_c)
            node(claim_id, "Claim", "Claim", confidence=fact.confidence)
            node(property_id, fact.property, "Property")
            node(effect_id, _effect_label(fact), "Effect")
            node(source_id, f"Источник {fact.source.page}", "SourceFragment", document_id=fact.source.document_id)
            node(f"lab-{_slug(fact.lab)}", fact.lab, "Laboratory")
            edge(claim_id, fact.material_id, "ABOUT")
            edge(claim_id, fact.experiment_id, "BASED_ON")
            edge(fact.experiment_id, property_id, "MEASURES")
            edge(fact.experiment_id, effect_id, "PRODUCED")
            edge(source_id, claim_id, "SUPPORTS")
            edge(fact.experiment_id, f"lab-{_slug(fact.lab)}", "CONDUCTED_BY")
        return GraphPayload(nodes=list(nodes.values()), edges=list(edges.values()))

    def persistent_graph(self) -> GraphPayload:
        if self.graph_sink:
            graph = self.graph_sink.get_graph()
            if graph.nodes:
                return graph
        return self.get_graph()

    def persistent_facts(self) -> list[dict[str, Any]]:
        if self.postgres_sink:
            rows = self.postgres_sink.list_facts()
            if rows:
                return rows
        return [fact.model_dump(mode="json") for fact in self.facts.values()]

    def _fact_from_candidate(self, candidate: ExtractionCandidate) -> Fact:
        payload = candidate.payload
        material = self.normalizer.normalize_entity(str(payload.get("material", "Unknown Material"))) or "Unknown Material"
        property_name = self.normalizer.normalize_entity(str(payload.get("property", "unknown property"))) or "unknown property"
        source = candidate.source
        if source is None:
            raise SourceRequiredError("Факт не может быть утвержден без ссылки на source fragment.")
        fact_id = f"claim-{candidate.id.replace('candidate-', '')}"
        return Fact(
            id=fact_id,
            candidate_id=candidate.id,
            material=material,
            material_id=f"material-{_slug(material)}",
            experiment_id=str(payload.get("experiment_id") or f"exp-{uuid4().hex[:8]}"),
            sample=str(payload.get("sample") or "unknown sample"),
            process=str(payload.get("process") or "unknown process"),
            temperature_c=_float_or_none(payload.get("temperature_c")),
            duration_h=_float_or_none(payload.get("duration_h")),
            property=property_name,
            effect_direction=str(payload.get("effect_direction") or "unknown"),
            effect_value=_float_or_none(payload.get("effect_value")),
            effect_unit=payload.get("effect_unit"),
            result_value=_float_or_none(payload.get("result_value")),
            result_unit=payload.get("result_unit"),
            lab=str(payload.get("lab") or "Unknown Lab"),
            team=str(payload.get("team") or "Unknown Team"),
            equipment=payload.get("equipment"),
            confidence=float(payload.get("confidence") or candidate.confidence),
            source=source,
        )

    def _persist_document(self, document: DocumentRecord, version: DocumentVersion) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_document(document, version)

    def _persist_fragments(self, fragments: list[SourceFragment]) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_fragments(fragments)

    def _persist_candidate(self, candidate: ExtractionCandidate) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_candidate(candidate)

    def _persist_fact(self, fact: Fact) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_fact(fact)
        if self.graph_sink:
            self.graph_sink.upsert_fact(fact)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return numerator / (left_norm * right_norm)


def _slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part)


def _effect_label(fact: Fact) -> str:
    direction = {"increase": "рост", "decrease": "снижение", "neutral": "без изменений"}.get(
        fact.effect_direction, fact.effect_direction
    )
    value = f" {fact.effect_value:g}{fact.effect_unit or ''}" if fact.effect_value is not None else ""
    return f"{direction}{value}"
