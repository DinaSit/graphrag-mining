from __future__ import annotations

import hashlib
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.file_storage import MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.normalization import DomainNormalizer
from app.pipeline.parsers import choose_parser
from app.pipeline.providers import (
    DeterministicEmbeddingProvider,
    MockLLMProvider,
    RemoteEmbeddingProvider,
    RemoteExtractionProvider,
)
from app.pipeline.validation import load_validation_rules, validate_candidate_numbers
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
        # Пороги кандидатов и диапазоны правдоподобия — из validation-rules.yaml
        # (владелец — инженер знаний); дефолты сохраняют прежнее поведение
        self.validation_rules = load_validation_rules(domain_dir)
        thresholds = self.validation_rules.get("thresholds", {})
        self.auto_approve_threshold = float(thresholds.get("auto_approve", AUTO_APPROVE_THRESHOLD))
        self.reject_threshold = float(thresholds.get("review_min", 0.60))
        self.llm = RemoteExtractionProvider(extraction_service_url) if extraction_service_url else MockLLMProvider()
        # Провайдер эмбеддингов выбирается окружением: EMBEDDINGS_URL задан —
        # внешний сервис (Yandex v2, 256), не задан — детерминированный baseline (64)
        embeddings_url = os.environ.get("EMBEDDINGS_URL")
        self.embedder = RemoteEmbeddingProvider(embeddings_url) if embeddings_url else DeterministicEmbeddingProvider()
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

    def hydrate_from_postgres(self) -> None:
        """Восстанавливает состояние из PostgreSQL после перезапуска backend-а."""
        if not self.postgres_sink or not self.postgres_sink.enabled:
            return
        state = self.postgres_sink.load_state()
        self.documents.update(state["documents"])
        self.versions.update(state["versions"])
        self.fragments.update(state["fragments"])
        self.candidates.update(state["candidates"])
        self.facts.update(state["facts"])
        self.fragment_vectors.update(state["vectors"])
        # Фрагменты без векторов (например, после смены модели эмбеддингов)
        # индексируются заново в фоне: старт backend не блокируется
        missing = [fragment for fid, fragment in self.fragments.items() if fid not in self.fragment_vectors]
        if missing:
            threading.Thread(target=self._reindex_missing, args=(missing,), daemon=True, name="reindex-missing").start()

    def _reindex_missing(self, fragments: list[SourceFragment]) -> None:
        try:
            self.index_fragments(fragments)
            print(f"hydrate: переиндексировано {len(fragments)} фрагментов")
        except Exception as exc:
            print(f"hydrate: переиндексация {len(fragments)} фрагментов не удалась: {exc}")

    def add_source_fragment(self, fragment: SourceFragment) -> None:
        self.fragments[fragment.id] = fragment
        self._persist_fragments([fragment])

    def delete_document(self, document_id: str) -> dict:
        """Удаляет документ со всем, что из него извлечено: фрагменты, кандидаты,
        факты, узлы графа. Общие сущности остаются, если на них ссылаются другие
        документы; осиротевшие вершины вычищаются.
        """
        document = self.documents.get(document_id)
        if document is None:
            raise KeyError(document_id)

        fragment_ids = [fid for fid, f in list(self.fragments.items()) if f.document_id == document_id]
        fact_ids = [fid for fid, f in list(self.facts.items()) if f.source.document_id == document_id]
        candidate_ids = [
            cid for cid, c in list(self.candidates.items())
            if c.source is not None and c.source.document_id == document_id
        ]

        for fid in fragment_ids:
            self.fragments.pop(fid, None)
            self.fragment_vectors.pop(fid, None)
        for cid in candidate_ids:
            self.candidates.pop(cid, None)
        for fid in fact_ids:
            self.facts.pop(fid, None)
        self.versions.pop(document.current_version_id, None)
        self.documents.pop(document_id, None)

        # Снять пометку спора у оппонентов удалённых фактов
        removed = set(fact_ids)
        for fact in list(self.facts.values()):
            if removed & set(fact.conflicts_with):
                fact.conflicts_with = [fid for fid in fact.conflicts_with if fid not in removed]
                if not fact.conflicts_with and fact.status == "conflicting":
                    fact.status = "approved"
                self._persist_fact(fact)

        if self.postgres_sink:
            self.postgres_sink.delete_document_data(document_id)
        if self.graph_sink:
            self.graph_sink.delete_document(document_id, fact_ids)
        if self.file_storage:
            self.file_storage.delete_document(document_id)

        return {"document_id": document_id, "fragments": len(fragment_ids),
                "candidates": len(candidate_ids), "facts": len(fact_ids)}

    def find_document_by_checksum(self, checksum: str) -> DocumentRecord | None:
        duplicate = next((doc for doc in list(self.documents.values()) if doc.checksum == checksum), None)
        if duplicate:
            return duplicate
        if self.postgres_sink:
            persisted = self.postgres_sink.get_document_by_checksum(checksum)
            if persisted:
                document, version = persisted
                self.documents[document.id] = document
                self.versions[version.id] = version
                return document
        return None

    def ingest_document(
        self,
        filename: str,
        content: bytes,
        document_type: str | None = None,
        source_label: str | None = None,
        access_level: str = "uploaded",
    ) -> DocumentRecord:
        checksum = hashlib.sha256(content).hexdigest()
        duplicate = self.find_document_by_checksum(checksum)
        if duplicate:
            return duplicate

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

        fragments: list[SourceFragment] = []
        try:
            parser = choose_parser(filename)
            fragments = parser.parse(document_id, version_id, filename, content)
            version.parser = parser.name
            document.element_count = len(fragments)
            for fragment in fragments:
                self.fragments[fragment.id] = fragment
            self._persist_fragments(fragments)
            self._persist_document(document, version)

            candidates = self.llm.extract_entities(fragments)
            for candidate in candidates:
                self.add_candidate(candidate)

            self.index_fragments(fragments)
            document.status = DocumentStatus.completed
            version.status = DocumentStatus.completed
            self._persist_document(document, version)
            return document
        except Exception:
            document.status = DocumentStatus.failed
            document.element_count = len(fragments)
            version.status = DocumentStatus.failed
            self._persist_document(document, version)
            raise

    def add_candidate(self, candidate: ExtractionCandidate) -> ExtractionCandidate:
        if candidate.source:
            source_text = candidate.source.quote or self.fragments.get(candidate.source.fragment_id, SourceFragment(
                id=candidate.source.fragment_id,
                document_id=candidate.source.document_id,
                version_id=candidate.source.version_id,
                text="",
                normalized_text="",
            )).text
            candidate.payload["number_validation"] = validate_candidate_numbers(
                candidate.payload, source_text, self.validation_rules
            )
        number_validation = candidate.payload.get("number_validation", {})
        quality_issues = _candidate_quality_issues(candidate.payload)
        if quality_issues:
            candidate.review_note = "Кандидат требует проверки: " + "; ".join(quality_issues)
        elif candidate.confidence >= self.auto_approve_threshold:
            if number_validation.get("validated", True):
                candidate.status = CandidateStatus.approved
            else:
                # «Ошибки в числах недопустимы»: сомнительные числа не проходят
                # в граф автоматически — только через эксперта
                candidate.review_note = "Числа требуют проверки: " + "; ".join(number_validation.get("issues", [])[:3])
        elif candidate.confidence < self.reject_threshold:
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
        self._mark_conflicts(fact)
        self.facts[fact.id] = fact
        self._persist_candidate(candidate)
        self._persist_fact(fact)
        self._project_semantics(fact, candidate)
        return fact

    def _mark_conflicts(self, fact: Fact) -> None:
        """Фиксирует противоречие: тот же материал и свойство, противоположный эффект.

        Оба факта остаются в базе как есть — статус conflicting лишь помечает
        зону разногласий и хранит ссылки на оппонентов (модель верификации из плана).
        """
        fact_direction = _normalize_effect_direction(fact.effect_direction)
        opposite = {"increase": "decrease", "decrease": "increase"}.get(fact_direction)
        if opposite is None:
            return
        fact_key = self._fact_conflict_key(fact)
        # list(): факты добавляются из фоновых воркеров параллельно
        for other in list(self.facts.values()):
            other_direction = _normalize_effect_direction(other.effect_direction)
            if (
                self._fact_conflict_key(other) == fact_key
                and other_direction == opposite
            ):
                fact.status = other.status = "conflicting"
                if other.id not in fact.conflicts_with:
                    fact.conflicts_with.append(other.id)
                if fact.id not in other.conflicts_with:
                    other.conflicts_with.append(fact.id)
                self._persist_fact(other)

    def _fact_conflict_key(self, fact: Fact) -> tuple[str, str]:
        material = self.normalizer.normalize_entity(fact.material) or fact.material
        property_name = self.normalizer.normalize_entity(fact.property) or fact.property
        return _canonical_text(material), _canonical_text(property_name)

    def _project_semantics(self, fact: Fact, candidate: ExtractionCandidate) -> None:
        """Переносит извлечённые сущности и связи онтологии из payload в Neo4j."""
        if not self.graph_sink:
            return
        entities = [
            {"type": item.get("type"), "name": self.normalizer.normalize_entity(str(item.get("name", ""))) or ""}
            for item in candidate.payload.get("entities", [])
            if isinstance(item, dict)
        ]
        relations = [
            {
                "subject": self.normalizer.normalize_entity(str(item.get("subject", ""))) or "",
                "predicate": item.get("predicate"),
                "object": self.normalizer.normalize_entity(str(item.get("object", ""))) or "",
            }
            for item in candidate.payload.get("relations", [])
            if isinstance(item, dict)
        ]
        if entities or relations:
            self.graph_sink.upsert_semantics(fact.id, entities, relations)

    def reject_candidate(self, candidate_id: str, note: str | None = None) -> ExtractionCandidate:
        candidate = self.candidates[candidate_id]
        candidate.status = CandidateStatus.rejected
        candidate.review_note = note
        self._persist_candidate(candidate)
        return candidate

    def index_fragments(self, fragments: list[SourceFragment]) -> None:
        # Пачками: большой документ не влезает в таймаут одного запроса,
        # а результат фиксируется по мере готовности, а не в конце.
        # 16 длинных фрагментов на CPU укладываются в таймаут с запасом
        for start in range(0, len(fragments), 16):
            chunk = fragments[start : start + 16]
            vectors = self.embedder.embed([fragment.normalized_text for fragment in chunk])
            new_vectors: dict[str, list[float]] = {}
            for fragment, vector in zip(chunk, vectors):
                self.fragment_vectors[fragment.id] = vector
                new_vectors[fragment.id] = vector
            if self.postgres_sink:
                self.postgres_sink.upsert_vectors(new_vectors, self.embedder.name)

    def search(self, query: str, top_k: int = 8) -> list[SearchHit]:
        if not self.postgres_sink or not self.postgres_sink.enabled:
            raise RuntimeError("Семантический поиск требует PostgreSQL (pgvector).")
        # Для запросов используется query-режим модели, если провайдер его поддерживает
        embed_query = getattr(self.embedder, "embed_query", self.embedder.embed)
        query_vector = embed_query([query])[0]
        # Близость считает pgvector; кандидатов берём с запасом,
        # финальный порядок определяет гибридный скоринг с лексической добавкой
        candidates = self.postgres_sink.search_vectors(query_vector, top_k * 3)
        query_terms = set(query.lower().replace("ё", "е").split())
        hits: list[SearchHit] = []
        for fragment_id, semantic in candidates:
            fragment = self.fragments.get(fragment_id)
            if fragment is None:
                continue
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
            effect_direction=_normalize_effect_direction(payload.get("effect_direction")),
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


def _candidate_quality_issues(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if _is_missing_value(payload.get("material")):
        issues.append("material не извлечён")
    if _is_missing_value(payload.get("property")):
        issues.append("property не извлечён")
    return issues


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower().replace("ё", "е")
    return text in {"", "не указано", "unknown", "unknown material", "unknown property", "n/a", "none", "null"}


def _normalize_effect_direction(value: Any) -> str:
    text = str(value or "unknown").strip().lower().replace("ё", "е")
    aliases = {
        "increase": "increase",
        "increased": "increase",
        "рост": "increase",
        "увеличение": "increase",
        "повышение": "increase",
        "decrease": "decrease",
        "decreased": "decrease",
        "снижение": "decrease",
        "уменьшение": "decrease",
        "падение": "decrease",
        "neutral": "neutral",
        "no_change": "neutral",
        "без изменений": "neutral",
        "нет изменений": "neutral",
    }
    return aliases.get(text, text or "unknown")


def _canonical_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("ё", "е").split())


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
