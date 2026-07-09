from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.file_storage import PREVIEW_SUFFIX, MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.document_traits import classify_document, extract_publication_year
from app.pipeline.normalization import (
    JUNK_VALUES,
    DomainNormalizer,
    canonical_text,
    clean_extracted,
    direction_label,
    float_or_none,
    slug,
)
from app.pipeline.office_render import convert_office_to_pdf
from app.pipeline.parsers import choose_parser, extension_of
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


try:
    from psycopg.errors import UniqueViolation
except ImportError:  # pragma: no cover
    UniqueViolation = None

log = logging.getLogger(__name__)

AUTO_APPROVE_THRESHOLD = 0.85

# Форматы, для которых при инжесте строится PDF-превью (LibreOffice уже в образе).
# PDF рендерится браузером как есть, у прочих форматов превью нет
PREVIEW_SOURCE_EXTENSIONS = frozenset({".docx", ".docm", ".pptx"})


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
        # внешний сервис (bge-m3 в ml-extraction, размерность EMBEDDING_DIM=1024),
        # не задан — детерминированный baseline (64)
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
        # Дедуп по checksum: проверка и регистрация документа атомарны,
        # иначе два ingest-воркера создают дубликаты одного файла
        self._ingest_lock = threading.Lock()
        # Документы, удаление которых уже началось: reprocess и toggle видимости
        # сверяются с этим набором под тем же локом, иначе записи, пришедшие во
        # время удаления, ре-INSERT-ят документ-призрак в PG/Neo4j
        self._deleting: set[str] = set()

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
        self.ontology_candidates.update(state.get("ontology_candidates", {}))
        # Признаки документов, загруженных до появления эвристики, дочитываются
        # синхронно: счёт по фрагментам в памяти, объём — единицы документов.
        # Группировка фрагментов по документу делается один раз на оба бэкфила,
        # а не полным сканом стора на каждый документ
        fragments_by_document = self._fragments_by_document()
        self._backfill_document_traits(fragments_by_document)
        # Год издания — отдельным проходом: документ мог получить is_scientific
        # до появления поля year, поэтому бэкфил года не совмещён с traits
        self._backfill_document_years(fragments_by_document)
        # Мусорные значения ('не указано', 'unknown'…) в старых фактах чистятся
        # у источника единожды при старте (идемпотентно)
        self._backfill_junk_facts()
        # Фрагменты без векторов (например, после смены модели эмбеддингов)
        # индексируются заново в фоне: старт backend не блокируется
        missing = [fragment for fid, fragment in self.fragments.items() if fid not in self.fragment_vectors]
        if missing:
            threading.Thread(target=self._reindex_missing, args=(missing,), daemon=True, name="reindex-missing").start()
        # DOCX/PPTX, загруженные до появления PDF-превью, конвертируются в фоне
        # (как _reindex_missing): старт backend не блокируется, идемпотентно —
        # документ с уже проставленным preview_object пропускается
        if self.file_storage and self.file_storage.enabled:
            no_preview = [
                document for document in list(self.documents.values())
                if document.storage_object
                and document.preview_object is None
                and extension_of(document.filename) in PREVIEW_SOURCE_EXTENSIONS
            ]
            if no_preview:
                threading.Thread(
                    target=self._backfill_previews, args=(no_preview,), daemon=True, name="preview-backfill"
                ).start()

    def _fragments_by_document(self) -> dict[str, list[SourceFragment]]:
        """Фрагменты, сгруппированные по документу, в порядке (page, id):
        порядок из PG не гарантирован — восстанавливается по странице."""
        grouped: dict[str, list[SourceFragment]] = {}
        for fragment in list(self.fragments.values()):
            grouped.setdefault(fragment.document_id, []).append(fragment)
        for fragments in grouped.values():
            fragments.sort(key=lambda fragment: (fragment.page, fragment.id))
        return grouped

    def _backfill_document_traits(self, fragments_by_document: dict[str, list[SourceFragment]] | None = None) -> None:
        """Вычисляет is_scientific/origin документам, у которых признаков ещё нет."""
        if fragments_by_document is None:
            fragments_by_document = self._fragments_by_document()
        for document in list(self.documents.values()):
            if document.is_scientific is not None:
                continue
            fragments = fragments_by_document.get(document.id)
            if not fragments:
                continue
            document.is_scientific, document.origin = classify_document(fragments)
            version = self.versions.get(document.current_version_id)
            if version is not None:
                try:
                    self._persist_document(document, version)
                except Exception:
                    # Признаки справочные: сбой записи не должен ронять старт,
                    # бэкфил повторится при следующем запуске
                    log.exception("Не удалось сохранить признаки документа %s", document.id)

    def _backfill_document_years(self, fragments_by_document: dict[str, list[SourceFragment]] | None = None) -> None:
        """Проставляет year документам, у которых он ещё не вычислен (None).

        Идемпотентно: документ с уже известным годом пропускается, повторный
        старт ничего не пишет. Год из документа без найденного года останется
        None и будет пересчитываться при каждом старте — это дёшево (единицы
        документов, счёт по фрагментам в памяти) и позволяет подхватить год,
        если эвристику позже улучшат.
        """
        if fragments_by_document is None:
            fragments_by_document = self._fragments_by_document()
        filled = 0
        for document in list(self.documents.values()):
            if document.year is not None:
                continue
            fragments = fragments_by_document.get(document.id)
            if not fragments:
                continue
            year = extract_publication_year(fragments, document.filename)
            if year is None:
                continue
            document.year = year
            version = self.versions.get(document.current_version_id)
            if version is not None:
                try:
                    self._persist_document(document, version)
                except Exception:
                    # Год справочный: сбой записи не роняет старт, повторится
                    log.exception("Не удалось сохранить год документа %s", document.id)
            filled += 1
        if filled:
            log.info("backfill: год издания проставлен %d документам", filled)

    def _backfill_junk_facts(self) -> None:
        """Чистит мусорные значения текстовых полей существующих фактов до ''
        (единый clean_extracted) и персистит. Идемпотентно: уже чистое поле
        не меняется, так что повторный старт не пишет ничего."""
        cleaned = 0
        for fact in list(self.facts.values()):
            updates: dict[str, Any] = {}
            for field in ("material", "process", "sample", "lab", "team"):
                value = getattr(fact, field)
                new_value = clean_extracted(value)
                if new_value != value:
                    updates[field] = new_value
            property_clean = clean_extracted(fact.property)
            if property_clean != fact.property:
                updates["property"] = property_clean
            # effect_direction 'unknown'/мусор → '' (в подписи такого направления не бывает)
            if _normalize_effect_direction(fact.effect_direction) == "unknown" and fact.effect_direction != "":
                updates["effect_direction"] = ""
            equipment_clean = clean_extracted(fact.equipment) or None
            if equipment_clean != fact.equipment:
                updates["equipment"] = equipment_clean
            if "material" in updates:
                updates["material_id"] = f"material-{slug(updates['material'])}"
            if not updates:
                continue
            updated = fact.model_copy(update=updates)
            self.facts[fact.id] = updated
            try:
                self._persist_fact(updated)
            except Exception:
                # Гигиена справочная: сбой записи не роняет старт, повторится
                log.exception("Не удалось сохранить очищенный факт %s", fact.id)
            cleaned += 1
        if cleaned:
            log.info("backfill: очищено мусорных значений в %d фактах", cleaned)

    def _reindex_missing(self, fragments: list[SourceFragment]) -> None:
        try:
            self.index_fragments(fragments)
            log.info("hydrate: переиндексировано %d фрагментов", len(fragments))
        except Exception:
            log.exception("hydrate: переиндексация %d фрагментов не удалась", len(fragments))

    def _make_preview(self, document: DocumentRecord, content: bytes) -> None:
        """Строит PDF-превью DOCX/PPTX (LibreOffice) и кладёт его в MinIO рядом
        с оригиналом (<storage_object>.preview.pdf), проставляя preview_object.

        Превью необязательно: сбой конвертации или недоступный MinIO не бросают —
        preview_object просто остаётся None (лог пишут convert/put_preview).
        """
        if not (self.file_storage and self.file_storage.enabled and document.storage_object):
            return
        suffix = extension_of(document.filename)
        if suffix not in PREVIEW_SOURCE_EXTENSIONS:
            return
        pdf_bytes = convert_office_to_pdf(content, suffix)
        if pdf_bytes is None:
            return
        preview_object = f"{document.storage_object}{PREVIEW_SUFFIX}"
        if self.file_storage.put_preview(preview_object, pdf_bytes):
            document.preview_object = preview_object

    def _backfill_previews(self, documents: list[DocumentRecord]) -> None:
        """Фоновый бэкфил PDF-превью для существующих DOCX/PPTX: скачать оригинал
        из MinIO, сконвертировать, положить превью, персистнуть preview_object.
        Ошибки по одному документу логируются и не прерывают остальные."""
        built = 0
        for document in documents:
            try:
                assert document.storage_object is not None  # отфильтровано вызывающим
                original = self.file_storage.get_object(document.storage_object)
                if original is None:
                    continue
                self._make_preview(document, original)
                if document.preview_object is None:
                    continue
                version = self.versions.get(document.current_version_id)
                if version is not None:
                    self._persist_document(document, version)
                built += 1
            except Exception:
                log.exception("backfill: PDF-превью документа %s не создано", document.id)
        if built:
            log.info("backfill: PDF-превью создано для %d документов", built)

    def add_source_fragment(self, fragment: SourceFragment) -> None:
        self.fragments[fragment.id] = fragment
        self._persist_fragments([fragment])

    # --- Скрытие документов: единая точка фильтрации для всех путей ответа ---

    def hidden_document_ids(self) -> set[str]:
        """id скрытых документов; снапшот — воркеры мутируют documents параллельно."""
        return {doc_id for doc_id, document in list(self.documents.items()) if document.hidden}

    def is_visible_fact(self, fact: Fact) -> bool:
        document = self.documents.get(fact.source.document_id)
        return document is None or not document.hidden

    def visible_facts(self) -> list[Fact]:
        """Факты из нескрытых документов — единственный источник фактов для ответов."""
        hidden = self.hidden_document_ids()
        return [fact for fact in list(self.facts.values()) if fact.source.document_id not in hidden]

    def set_document_visibility(self, document_id: str, hidden: bool) -> DocumentRecord:
        """Скрывает/показывает документ. Данные не удаляются, Neo4j не перестраивается."""
        # Проверка и persist атомарны под общим с delete_document локом
        # (по образцу reprocess_document): toggle, догнавший удаление, не должен
        # ре-INSERT-ить строку только что удалённого документа в PG
        with self._ingest_lock:
            if not self._document_alive(document_id):
                raise KeyError(document_id)
            document = self.documents[document_id]
            document.hidden = hidden
            version = self.versions.get(document.current_version_id)
            if version is not None:
                self._persist_document(document, version)
        return document

    def random_visible_fact(self) -> Fact | None:
        """Случайный approved-факт из нескрытого документа; None, если таких нет."""
        pool = [fact for fact in self.visible_facts() if fact.status == "approved"]
        if not pool:
            return None
        # random.choice допустим: интерактивная фича UI, не workflow-скрипт
        return random.choice(pool)

    def delete_document(self, document_id: str) -> dict:
        """Удаляет документ со всем, что из него извлечено: фрагменты, кандидаты,
        факты, узлы графа. Общие сущности остаются, если на них ссылаются другие
        документы; осиротевшие вершины вычищаются.
        """
        with self._ingest_lock:
            document = self.documents.get(document_id)
            if document is None or document_id in self._deleting:
                raise KeyError(document_id)
            # Тумбстоун ставится до чистки внешних хранилищ: reprocess под тем же
            # локом видит его и отбрасывает результаты извлечения, а не ре-INSERT-ит
            # только что удалённые строки
            self._deleting.add(document_id)

        try:
            fragment_ids = [fid for fid, f in list(self.fragments.items()) if f.document_id == document_id]
            fact_ids = [fid for fid, f in list(self.facts.items()) if f.source.document_id == document_id]
            candidate_ids = [
                cid for cid, c in list(self.candidates.items())
                if c.source is not None and c.source.document_id == document_id
            ]

            # Сначала внешние хранилища: при сбое память не тронута,
            # и клиент может повторить удаление
            if self.postgres_sink:
                self.postgres_sink.delete_document_data(document_id)
            if self.graph_sink:
                self.graph_sink.delete_document(document_id, fact_ids)
            if self.file_storage:
                self.file_storage.delete_document(document_id)

            for fid in fragment_ids:
                self.fragments.pop(fid, None)
                self.fragment_vectors.pop(fid, None)
            for cid in candidate_ids:
                self.candidates.pop(cid, None)
            for fid in fact_ids:
                self.facts.pop(fid, None)
            self.versions.pop(document.current_version_id, None)
            self.documents.pop(document_id, None)
        finally:
            self._deleting.discard(document_id)

        # Снять пометку спора у оппонентов удалённых фактов
        removed = set(fact_ids)
        for fact in list(self.facts.values()):
            if removed & set(fact.conflicts_with):
                fact.conflicts_with = [fid for fid in fact.conflicts_with if fid not in removed]
                if not fact.conflicts_with and fact.status == "conflicting":
                    fact.status = "approved"
                self._persist_fact(fact)

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
        with self._ingest_lock:
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
        try:
            if self.file_storage:
                stored = self.file_storage.put_document(document_id, version_id, filename, content)
                if stored:
                    document.storage_bucket = stored.bucket
                    document.storage_object = stored.object_name
                    document.storage_uri = stored.uri
            self._persist_document(document, version)
        except Exception as exc:
            # Второй backend-процесс мог записать тот же файл: UNIQUE(checksum)
            # в PG — последний рубеж дедупа, возвращаем существующий документ
            existing = self._existing_on_unique_violation(exc, checksum, document_id, version_id)
            if existing:
                return existing
            document.status = version.status = DocumentStatus.failed
            try:
                self._persist_document(document, version)
            except Exception:
                log.exception("Не удалось сохранить статус failed документа %s", document_id)
            raise

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
            # Признаки документа считаются по готовым фрагментам один раз
            document.is_scientific, document.origin = classify_document(fragments)
            document.year = extract_publication_year(fragments, document.filename)
            # PDF-превью DOCX/PPTX — после успешного парсинга; сбой не роняет
            # инжест (preview_object останется None)
            self._make_preview(document, content)
            document.status = DocumentStatus.completed
            version.status = DocumentStatus.completed
            self._persist_document(document, version)
            return document
        except Exception:
            document.status = DocumentStatus.failed
            document.element_count = len(fragments)
            version.status = DocumentStatus.failed
            try:
                self._persist_document(document, version)
            except Exception:
                # Причина сбоя важнее статуса: исходное исключение не подменяется
                log.exception("Не удалось сохранить статус failed документа %s", document_id)
            raise

    def _existing_on_unique_violation(
        self, exc: Exception, checksum: str, document_id: str, version_id: str
    ) -> DocumentRecord | None:
        if UniqueViolation is None or not isinstance(exc, UniqueViolation):
            return None
        self.documents.pop(document_id, None)
        self.versions.pop(version_id, None)
        if self.file_storage:
            try:
                self.file_storage.delete_document(document_id)
            except Exception:
                log.exception("Не удалось убрать файл проигравшего дубля %s из MinIO", document_id)
        return self.find_document_by_checksum(checksum)

    def add_candidate(self, candidate: ExtractionCandidate) -> ExtractionCandidate:
        if candidate.source:
            # Числа сверяются с полным текстом фрагмента: цитата обрезана
            # до 220 символов и заведомо не содержит всех значений
            fragment = self.fragments.get(candidate.source.fragment_id)
            source_text = (fragment.text if fragment else "") or candidate.source.quote or ""
            candidate.payload["number_validation"] = validate_candidate_numbers(
                candidate.payload, source_text, self.validation_rules
            )
        number_validation = candidate.payload.get("number_validation", {})
        quality_issues = _candidate_quality_issues(candidate.payload)
        if candidate.source is None:
            # Инвариант системы: факт существует только со ссылкой на первоисточник,
            # поэтому кандидат без source не может быть approved — даже если
            # статус уже проставлен снаружи (бэкфилл через /candidates)
            quality_issues.append("нет ссылки на source fragment")
            if candidate.status == CandidateStatus.approved:
                candidate.status = CandidateStatus.pending_review
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

    def reprocess_document(self, document_id: str) -> int:
        """Повторное извлечение по сохранённым фрагментам документа.

        Возвращает число принятых в обработку кандидатов. Отклонённые экспертом
        кандидаты не перезаписываются: решение эксперта сильнее пере-извлечения.
        """
        with self._ingest_lock:
            if document_id in self._deleting:
                raise KeyError(document_id)
            document = self.documents[document_id]
            version = self.versions[document.current_version_id]
            fragments = [f for f in list(self.fragments.values()) if f.document_id == document_id]
            document.status = version.status = DocumentStatus.processing
            document.element_count = len(fragments)
            self._persist_document(document, version)
        try:
            candidates = self.llm.extract_entities(fragments)
            accepted = 0
            for candidate in candidates:
                # Документ могли удалить, пока шло извлечение (окно — минуты):
                # проверка и запись атомарны под общим с delete_document локом,
                # иначе документ-призрак воскресает в PG/Neo4j
                with self._ingest_lock:
                    if not self._document_alive(document_id):
                        return accepted
                    existing = self.candidates.get(candidate.id)
                    if existing is not None and existing.status == CandidateStatus.rejected:
                        continue
                    self.add_candidate(candidate)
                    accepted += 1
            with self._ingest_lock:
                if not self._document_alive(document_id):
                    return accepted
                document.status = version.status = DocumentStatus.completed
                self._persist_document(document, version)
            return accepted
        except Exception:
            with self._ingest_lock:
                if self._document_alive(document_id):
                    document.status = version.status = DocumentStatus.failed
                    try:
                        self._persist_document(document, version)
                    except Exception:
                        log.exception("Не удалось сохранить статус failed документа %s", document_id)
            raise

    def _document_alive(self, document_id: str) -> bool:
        """Документ существует и не находится в процессе удаления."""
        return document_id in self.documents and document_id not in self._deleting

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
        return canonical_text(material), canonical_text(property_name)

    def _project_semantics(self, fact: Fact, candidate: ExtractionCandidate) -> None:
        """Переносит извлечённые сущности и связи онтологии из payload в Neo4j."""
        if not self.graph_sink:
            return
        # Сущности с мусорными именами ('не указано', 'unknown'…) в граф не идут
        entities = [
            {"type": item.get("type"), "name": clean_extracted(self.normalizer.normalize_entity(clean_extracted(item.get("name"))))}
            for item in candidate.payload.get("entities", [])
            if isinstance(item, dict)
        ]
        entities = [entity for entity in entities if entity["name"]]
        # Ребро, чьё имя-конец мусорное, тоже не проецируется
        relations = [
            {
                "subject": clean_extracted(self.normalizer.normalize_entity(clean_extracted(item.get("subject")))),
                "predicate": item.get("predicate"),
                "object": clean_extracted(self.normalizer.normalize_entity(clean_extracted(item.get("object")))),
            }
            for item in candidate.payload.get("relations", [])
            if isinstance(item, dict)
        ]
        relations = [rel for rel in relations if rel["subject"] and rel["object"]]
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
        hidden = self.hidden_document_ids()
        hits: list[SearchHit] = []
        for fragment_id, semantic in candidates:
            fragment = self.fragments.get(fragment_id)
            if fragment is None:
                continue
            # Фрагменты скрытых документов отбрасываются ДО среза top_k
            if fragment.document_id in hidden:
                continue
            # ё→е с обеих сторон: normalized_text парсеров букву ё сохраняет
            haystack = fragment.normalized_text.replace("ё", "е")
            lexical = sum(1 for term in query_terms if term and term in haystack) / max(len(query_terms), 1)
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

    # Семантические сущности, попадающие в ответный граф КГ: только эти типы
    # (Material/Property/Effect/Laboratory/SourceFragment отдельными узлами нет)
    _GRAPH_SEMANTIC_TYPES = frozenset({"Process", "Equipment", "Condition"})

    def get_graph(self, facts: list[Fact] | None = None) -> GraphPayload:
        # Без явного списка граф строится только по видимым фактам:
        # скрытые документы не попадают в визуализацию
        selected = facts if facts is not None else self.visible_facts()
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        # Дедуп узлов по (тип, каноническая подпись): «медь» из пяти фактов —
        # один узел; ключ → стабильный id
        node_ids: dict[tuple[str, str], str] = {}

        def node(node_type: str, label: str, **data: Any) -> str | None:
            """Создаёт (или переиспользует) узел; мусорная подпись узла не рождает.
            Возвращает id узла или None, если подпись мусорная."""
            if _is_junk_label(label):
                return None
            key = (node_type, canonical_text(label))
            existing = node_ids.get(key)
            if existing is not None:
                return existing
            base_id = f"{node_type.lower()}-{slug(label) or len(node_ids)}"
            # Разные подписи, схлопнувшиеся в один slug, не должны делить id-узел
            node_id = base_id
            suffix = 2
            while node_id in nodes:
                node_id = f"{base_id}-{suffix}"
                suffix += 1
            node_ids[key] = node_id
            nodes[node_id] = GraphNode(id=node_id, label=label, type=node_type, data=data)
            return node_id

        def edge(source: str, target: str, label: str) -> None:
            edge_id = f"{source}-{label}-{target}"
            edges.setdefault(edge_id, GraphEdge(id=edge_id, source=source, target=target, label=label))

        for fact in selected:
            # Claim: id факта как узел-id (дедуп по id, а не по подписи —
            # разные факты с одинаковой подписью остаются разными утверждениями)
            claim_id = fact.id
            if claim_id not in nodes:
                nodes[claim_id] = GraphNode(
                    id=claim_id, label=_claim_label(fact), type="Claim",
                    data={"confidence": fact.confidence, "title": _claim_title(fact)},
                )
            # Material → Claim
            material_id = node("Material", fact.material)
            if material_id is not None:
                edge(material_id, claim_id, "ABOUT")
            # Process → Claim
            process_id = node("Process", fact.process)
            if process_id is not None:
                edge(process_id, claim_id, "USED_IN")
            # Equipment → Claim
            equipment_id = node("Equipment", fact.equipment or "")
            if equipment_id is not None:
                edge(equipment_id, claim_id, "USED_IN")
            # Семантические сущности из payload (Process/Equipment/Condition) → Claim
            for entity_type, entity_name in self._semantic_entities(fact):
                entity_id = node(entity_type, entity_name)
                if entity_id is not None:
                    edge(entity_id, claim_id, "MENTIONS")
            # Claim → Document (один узел на документ, а не на фрагмент;
            # дедуп по document_id — одинаковые короткие имена не сливаются)
            document_id, document_label = self._document_node(fact.source)
            doc_node_id = f"document-{document_id}"
            if doc_node_id not in nodes:
                nodes[doc_node_id] = GraphNode(
                    id=doc_node_id, label=document_label, type="Document",
                    data={"document_id": document_id},
                )
            edge(claim_id, doc_node_id, "CITES")
        return GraphPayload(nodes=list(nodes.values()), edges=list(edges.values()))

    def _semantic_entities(self, fact: Fact) -> list[tuple[str, str]]:
        """Process/Equipment/Condition с чистыми именами из payload кандидата факта.
        Прочие типы (Material/Property/…) в ответный граф отдельными узлами не идут."""
        if not fact.candidate_id:
            return []
        candidate = self.candidates.get(fact.candidate_id)
        if candidate is None:
            return []
        result: list[tuple[str, str]] = []
        for item in candidate.payload.get("entities", []):
            if not isinstance(item, dict):
                continue
            entity_type = item.get("type")
            if entity_type not in self._GRAPH_SEMANTIC_TYPES:
                continue
            name = clean_extracted(self.normalizer.normalize_entity(clean_extracted(item.get("name"))))
            if name:
                result.append((entity_type, name))
        return result

    def _document_node(self, source: SourceRef) -> tuple[str, str]:
        """Один узел Document на документ: короткое имя файла без расширения (~20 симв.)."""
        document = self.documents.get(source.document_id)
        if document is None or not document.filename:
            return source.document_id, source.document_id
        name = document.filename.rsplit(".", 1)[0].strip()
        if len(name) > 20:
            name = name[:20].rstrip() + "…"
        return source.document_id, name

    def _fact_from_candidate(self, candidate: ExtractionCandidate) -> Fact:
        payload = candidate.payload
        # Гигиена у источника: мусорные значения ('не указано', 'unknown'…)
        # чистятся до '' единым clean_extracted, а не превращаются в дефолт-заглушку
        material = clean_extracted(self.normalizer.normalize_entity(clean_extracted(payload.get("material"))))
        property_name = clean_extracted(self.normalizer.normalize_entity(clean_extracted(payload.get("property"))))
        source = candidate.source
        if source is None:
            raise SourceRequiredError("Факт не может быть утвержден без ссылки на source fragment.")
        fact_id = f"claim-{candidate.id.replace('candidate-', '')}"
        effect_direction = _normalize_effect_direction(payload.get("effect_direction"))
        if effect_direction == "unknown":
            effect_direction = ""
        return Fact(
            id=fact_id,
            candidate_id=candidate.id,
            material=material,
            material_id=f"material-{slug(material)}",
            experiment_id=str(payload.get("experiment_id") or f"exp-{uuid4().hex[:8]}"),
            sample=clean_extracted(payload.get("sample")),
            process=clean_extracted(payload.get("process")),
            temperature_c=float_or_none(payload.get("temperature_c")),
            duration_h=float_or_none(payload.get("duration_h")),
            property=property_name,
            effect_direction=effect_direction,
            effect_value=float_or_none(payload.get("effect_value")),
            effect_unit=payload.get("effect_unit"),
            result_value=float_or_none(payload.get("result_value")),
            result_unit=payload.get("result_unit"),
            lab=clean_extracted(payload.get("lab")),
            team=clean_extracted(payload.get("team")),
            equipment=clean_extracted(payload.get("equipment")) or None,
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


def _is_junk_label(value: str | None) -> bool:
    # Единый список мусорных подписей — JUNK_VALUES из normalization
    return str(value or "").strip().casefold().replace("ё", "е") in JUNK_VALUES


def _effect_label(fact: Fact) -> str:
    value = f" {fact.effect_value:g}{fact.effect_unit or ''}" if fact.effect_value is not None else ""
    return f"{direction_label(fact.effect_direction)}{value}"


def _claim_label(fact: Fact) -> str:
    """Короткая подпись узла Claim — суть утверждения: "<property>: <направление>";
    без извлечённого направления — просто property (без висящего двоеточия);
    если property не извлечён — начало цитаты источника."""
    if not _is_junk_label(fact.property):
        direction = direction_label(_normalize_effect_direction(fact.effect_direction))
        if direction and direction != "unknown":
            return f"{fact.property}: {direction}"
        return fact.property
    quote = (fact.source.quote or "").strip()
    if quote:
        return quote[:40] + ("…" if len(quote) > 40 else "")
    return fact.id


def _claim_title(fact: Fact) -> str:
    """Полное описание утверждения для data.title узла Claim (тултип UI)."""
    context = ", ".join(part for part in (fact.material, fact.process) if not _is_junk_label(part))
    if not _is_junk_label(fact.property):
        effect = _effect_label(fact).strip()
        body = f"{fact.property}: {effect}" if effect else fact.property
    else:
        body = (fact.source.quote or "").strip() or fact.id
    return f"{context} — {body}" if context else body
