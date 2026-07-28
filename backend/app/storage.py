"""Хранилище приложения: рабочее состояние в памяти + запись во внешние хранилища.

ApplicationStore — единственная точка изменения данных. Состояние процесса
(documents/fragments/candidates/facts/vectors) считается рабочим набором, его
персистентная копия живёт в PostgreSQL и Neo4j, оригиналы файлов — в MinIO.
Восстановление после перезапуска и бэкфилы вынесены в pipeline/hydration,
проекция фактов в граф ответа — в pipeline/graph_view, сборка факта из
кандидата — в pipeline/fact_builder.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.file_storage import PREVIEW_SUFFIX, MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline import hydration
from app.pipeline.document_traits import classify_document_llm, detect_origin, extract_publication_year
from app.pipeline.fact_builder import SourceRequiredError, candidate_field_issues, fact_from_candidate
from app.pipeline.graph_view import build_graph
from app.pipeline.normalization import (
    DomainNormalizer,
    canonical_text,
    clean_extracted,
    normalize_effect_direction,
)
from app.pipeline.office_render import PREVIEW_SOURCE_EXTENSIONS, convert_office_to_pdf
from app.pipeline.parsers import choose_parser, extension_of
from app.pipeline.providers import (
    DeterministicEmbeddingProvider,
    RemoteEmbeddingProvider,
    RemoteExtractionProvider,
)
from app.pipeline.validation import load_validation_rules, quote_in_source, validate_candidate_numbers
from app.schemas import (
    CandidateStatus,
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    ExtractionCandidate,
    Fact,
    GraphPayload,
    SearchHit,
    SourceRef,
    SourceFragment,
)


try:
    from psycopg.errors import UniqueViolation
except ImportError:  # pragma: no cover
    UniqueViolation = None

log = logging.getLogger(__name__)

__all__ = ["ApplicationStore", "SourceRequiredError"]


def _row_ordinal(fragment: SourceFragment) -> int:
    """Номер строки внутри таблицы; без него порядок строк восстановить нечем."""
    try:
        return int((fragment.metadata or {}).get("row"))
    except (TypeError, ValueError):
        return 0


def _row_values_with_units(header: str, row: str) -> str:
    """Пары «значение единица» из строки таблицы по шапке той же колонки.

    Проверка чисел ищет единицу вплотную к числу, а в таблице единица задана
    заголовком колонки («Температура, оС») и стоит в другом фрагменте. Пары
    собираются только при совпадении числа колонок: парсер отбрасывает пустые
    ячейки, и при расхождении длин значение попало бы под чужую единицу.
    """
    heads = [cell.strip() for cell in header.split("|")]
    cells = [cell.strip() for cell in row.split("|")]
    if len(heads) < 2 or len(heads) != len(cells):
        return ""
    pairs = []
    for head, cell in zip(heads, cells):
        if "," not in head or not cell:
            continue
        unit = head.rsplit(",", 1)[1].strip()
        if unit:
            pairs.append(f"{cell} {unit}")
    return "\n".join(pairs)


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
        # Диапазоны правдоподобия чисел — из validation-rules.yaml (владелец —
        # инженер знаний). Порогов по самооценке модели больше нет: судьбу
        # кандидата решают проверки, см. add_candidate
        self.validation_rules = load_validation_rules(domain_dir)
        # Извлечение возможно только через сервис: заглушки нет намеренно —
        # молча выдуманные факты хуже явного отказа при загрузке документа
        self.llm = RemoteExtractionProvider(extraction_service_url) if extraction_service_url else None
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
        # Шапка таблицы по (document_id, номер таблицы): нужна проверке чисел
        self._table_headers: dict[tuple[str, str], str] = {}
        # Идентификаторы фрагментов, у которых эмбеддинг посчитан. Сами векторы
        # хранит PostgreSQL (таблица fragment_vectors) — близость считает pgvector
        # своим индексом, в память они не поднимаются; здесь нужен только ответ
        # на вопрос «у этого фрагмента вектор уже есть?» для дозаполнения при старте
        self.vectorized_fragment_ids: set[str] = set()
        # Дедуп по checksum: проверка и регистрация документа атомарны,
        # иначе два ingest-воркера создают дубликаты одного файла
        self._ingest_lock = threading.Lock()
        # Документы, удаление которых уже началось: reprocess и toggle видимости
        # сверяются с этим набором под тем же локом, иначе записи, выполненные
        # во время удаления, повторно вставляют удалённый документ в PG/Neo4j
        self._deleting: set[str] = set()

    def hydrate_from_postgres(self) -> None:
        """Восстанавливает состояние из PostgreSQL после перезапуска backend-а."""
        hydration.hydrate(self)

    # --- Фрагменты ---

    def fragments_by_document(self) -> dict[str, list[SourceFragment]]:
        """Фрагменты, сгруппированные по документу, в порядке (page, id):
        порядок из PG не гарантирован — восстанавливается по странице."""
        grouped: dict[str, list[SourceFragment]] = {}
        for fragment in list(self.fragments.values()):
            grouped.setdefault(fragment.document_id, []).append(fragment)
        for fragments in grouped.values():
            fragments.sort(key=lambda fragment: (fragment.page, fragment.id))
        return grouped

    def fragments_of(self, document_id: str) -> list[SourceFragment]:
        """Все фрагменты одного документа в порядке (page, id) — как в
        fragments_by_document: порядок из PG/дикта не гарантирован."""
        fragments = [f for f in list(self.fragments.values()) if f.document_id == document_id]
        fragments.sort(key=lambda f: (f.page, f.id))
        return fragments

    def add_source_fragment(self, fragment: SourceFragment) -> None:
        self.fragments[fragment.id] = fragment
        self._table_headers.clear()
        self._persist_fragments([fragment])

    # --- Признаки документа и PDF-превью ---

    def apply_traits(self, document: DocumentRecord, fragments: list[SourceFragment]) -> bool:
        """Пересчитывает признаки документа: происхождение и год (эвристики) +
        тип/научность/обоснование (LLM). Возвращает True, если LLM дала вердикт;
        при недоступной LLM прежние тип/научность не затираются."""
        document.origin = detect_origin(fragments)
        document.year = extract_publication_year(fragments, document.filename)
        traits = classify_document_llm(fragments, document.filename)
        if traits is None:
            return False
        document.doc_type = traits["doc_type"]
        document.is_scientific = traits["is_scientific"]
        document.trait_reason = traits["trait_reason"]
        return True

    def refresh_document_traits(self, document_id: str) -> None:
        """Переоценка признаков документа по сохранённым фрагментам: происхождение
        и год (эвристики) + тип/научность (LLM с обоснованием). Часть повторной
        обработки; недоступная LLM оставляет прежние тип/научность."""
        document = self.documents.get(document_id)
        if document is None:
            return
        fragments = self.fragments_of(document_id)
        if not fragments:
            return
        self.apply_traits(document, fragments)
        self.persist_document_quiet(document)

    def make_preview(self, document: DocumentRecord, content: bytes) -> None:
        """Строит PDF-превью DOCX/PPTX (LibreOffice) и сохраняет его в MinIO рядом
        с оригиналом (<storage_object>.preview.pdf), проставляя preview_object.

        Превью необязательно: сбой конвертации или недоступный MinIO не приводят
        к исключению — preview_object остаётся None (лог пишут convert/put_preview).
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
        # (по образцу reprocess_document): переключение видимости, выполненное
        # во время удаления, не должно повторно вставлять строку только что
        # удалённого документа в PG
        with self._ingest_lock:
            if not self._document_alive(document_id):
                raise KeyError(document_id)
            document = self.documents[document_id]
            document.hidden = hidden
            self.persist_current(document)
        return document

    def random_visible_fact(self) -> Fact | None:
        """Случайный approved-факт из нескрытого документа; None, если таких нет."""
        pool = [fact for fact in self.visible_facts() if fact.status == "approved"]
        if not pool:
            return None
        # random.choice допустим: интерактивная функция UI, не workflow-скрипт
        return random.choice(pool)

    # --- Жизненный цикл документа ---

    def delete_document(self, document_id: str) -> dict:
        """Удаляет документ со всем, что из него извлечено: фрагменты, кандидаты,
        факты, узлы графа. Общие сущности остаются, если на них ссылаются другие
        документы; осиротевшие вершины вычищаются.
        """
        with self._ingest_lock:
            document = self.documents.get(document_id)
            if document is None or document_id in self._deleting:
                raise KeyError(document_id)
            # Маркер удаления ставится до очистки внешних хранилищ: reprocess
            # под тем же локом видит его и отбрасывает результаты извлечения,
            # а не вставляет повторно только что удалённые строки
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
                self.vectorized_fragment_ids.discard(fid)
            for cid in candidate_ids:
                self.candidates.pop(cid, None)
            for fid in fact_ids:
                self.facts.pop(fid, None)
            self.versions.pop(document.current_version_id, None)
            self.documents.pop(document_id, None)
        finally:
            self._deleting.discard(document_id)

        # Снять пометку противоречия у фактов, конфликтовавших с удалёнными
        removed = set(fact_ids)
        for fact in list(self.facts.values()):
            if removed & set(fact.conflicts_with):
                fact.conflicts_with = [fid for fid in fact.conflicts_with if fid not in removed]
                if not fact.conflicts_with and fact.status == "conflicting":
                    fact.status = "approved"
                self.persist_fact(fact)

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
            self.persist_document(document, version)
        except Exception as exc:
            # Второй backend-процесс мог записать тот же файл: UNIQUE(checksum)
            # в PG — итоговая гарантия дедупликации, возвращаем существующий документ
            existing = self._existing_on_unique_violation(exc, checksum, document_id, version_id)
            if existing:
                return existing
            document.status = version.status = DocumentStatus.failed
            try:
                self.persist_document(document, version)
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
                self._table_headers.clear()
            self._persist_fragments(fragments)
            self.persist_document(document, version)

            candidates = self._extract(fragments)
            for candidate in candidates:
                self.add_candidate(candidate)

            self.index_fragments(fragments)
            # Признаки документа считаются по готовым фрагментам один раз.
            # Тип и научность — LLM по титульнику; недоступная LLM оставляет
            # None (прочерк), фоновый бэкфил дооценит при следующем старте
            self.apply_traits(document, fragments)
            # PDF-превью DOCX/PPTX — после успешного парсинга; сбой не прерывает
            # инжест (preview_object останется None)
            self.make_preview(document, content)
            document.status = DocumentStatus.completed
            version.status = DocumentStatus.completed
            self.persist_document(document, version)
            return document
        except Exception:
            document.status = DocumentStatus.failed
            document.element_count = len(fragments)
            version.status = DocumentStatus.failed
            try:
                self.persist_document(document, version)
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
            fragments = self.fragments_of(document_id)
            document.status = version.status = DocumentStatus.processing
            document.element_count = len(fragments)
            self.persist_document(document, version)
        try:
            candidates = self._extract(fragments)
            accepted = 0
            for candidate in candidates:
                # Документ могли удалить, пока шло извлечение (окно — минуты):
                # проверка и запись атомарны под общим с delete_document локом,
                # иначе удалённый документ повторно появляется в PG/Neo4j
                with self._ingest_lock:
                    if not self._document_alive(document_id):
                        return accepted
                    existing = self.candidates.get(candidate.id)
                    # Решение эксперта сильнее пере-извлечения: отклонённые и
                    # правленные руками кандидаты моделью не перезаписываются
                    if existing is not None and (
                        existing.status == CandidateStatus.rejected
                        or existing.payload.get("edited_by_human")
                    ):
                        continue
                    self.add_candidate(candidate)
                    accepted += 1
            with self._ingest_lock:
                if not self._document_alive(document_id):
                    return accepted
                document.status = version.status = DocumentStatus.completed
                self.persist_document(document, version)
            return accepted
        except Exception:
            with self._ingest_lock:
                if self._document_alive(document_id):
                    document.status = version.status = DocumentStatus.failed
                    try:
                        self.persist_document(document, version)
                    except Exception:
                        log.exception("Не удалось сохранить статус failed документа %s", document_id)
            raise

    def _extract(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        """Извлечение кандидатов сервисом. Без адреса сервиса — явный отказ:
        документ получит статус failed с понятной причиной, а не пустой результат."""
        if self.llm is None:
            raise RuntimeError("Извлечение недоступно: не задан EXTRACTION_SERVICE_URL")
        return self.llm.extract_entities(fragments)

    def _document_alive(self, document_id: str) -> bool:
        """Документ существует и не находится в процессе удаления."""
        return document_id in self.documents and document_id not in self._deleting

    # --- Кандидаты и факты ---

    def _table_header(self, fragment: SourceFragment | None) -> str:
        """Первая строка той же таблицы — та, где заданы названия колонок и единицы.

        Кэш сбрасывается вместе с добавлением фрагментов: состав таблиц меняется
        только при разборе документа.
        """
        if fragment is None or fragment.element_type != "docx_table_row":
            return ""
        table = (fragment.metadata or {}).get("table")
        if table is None:
            return ""
        key = (fragment.document_id, str(table))
        if key not in self._table_headers:
            rows = [
                other for other in self.fragments.values()
                if other.document_id == fragment.document_id
                and other.element_type == "docx_table_row"
                and str((other.metadata or {}).get("table")) == str(table)
            ]
            header = min(rows, key=_row_ordinal, default=None)
            self._table_headers[key] = header.text if header else ""
        header_text = self._table_headers[key]
        # Для самой шапки контекст не нужен: она и есть фрагмент
        return "" if header_text == fragment.text else header_text

    def add_candidate(self, candidate: ExtractionCandidate) -> ExtractionCandidate:
        # Прежнее состояние нужно, чтобы поймать понижение: кандидат мог быть
        # утверждён раньше, и его факт обязан уйти вместе со статусом
        previous = self.candidates.get(candidate.id)
        if candidate.source:
            # Числа сверяются с полным текстом фрагмента: цитата обрезана
            # до 220 символов и заведомо не содержит всех значений
            fragment = self.fragments.get(candidate.source.fragment_id)
            fragment_text = fragment.text if fragment else ""
            # В таблице единица измерения стоит в шапке колонки, а не рядом со
            # значением, и попадает в отдельный фрагмент. Без шапки число из
            # ячейки нельзя опознать как температуру или расход — и проверка
            # чисел отвергала бы верные значения
            header_text = self._table_header(fragment)
            source_text = "\n".join(
                part for part in (
                    header_text,
                    fragment_text,
                    _row_values_with_units(header_text, fragment_text),
                ) if part
            ) or candidate.source.quote or ""
            candidate.payload["number_validation"] = validate_candidate_numbers(
                candidate.payload, source_text, self.validation_rules
            )
            # Цитата сверяется ТОЛЬКО с текстом фрагмента: сверять её с самой
            # собой бессмысленно, поэтому при отсутствующем фрагменте проверка
            # не выполняется. Неподтверждённая цитата не показывается читателю —
            # вместо неё в сноску идёт начало реального текста фрагмента
            if fragment_text:
                confirmed = quote_in_source(candidate.source.quote, fragment_text)
                candidate.payload["quote_validated"] = confirmed
                if not confirmed:
                    candidate.source.quote = fragment_text[:220]
        # Ворота в граф: все три механические проверки пройдены — кандидат
        # становится фактом сам; хоть одна не пройдена — идёт к эксперту с
        # названной причиной. Автоотклонения нет: выбрасывать утверждение,
        # которого человек не видел, нечем оправдать
        issues = self._review_issues(candidate)
        candidate.payload["review_issues"] = issues
        if issues:
            candidate.status = CandidateStatus.pending_review
            candidate.review_note = "Кандидат требует проверки: " + "; ".join(i["label"] for i in issues)
        else:
            candidate.status = CandidateStatus.approved
            candidate.review_note = None
        self.candidates[candidate.id] = candidate
        self._persist_candidate(candidate)
        if candidate.status == CandidateStatus.approved:
            self.approve_candidate(candidate.id)
        elif previous is not None and previous.status == CandidateStatus.approved:
            # Повторная обработка документа разобрала фрагмент иначе, и кандидат
            # больше не проходит ворота: утверждение снимается, иначе в графе
            # остаётся факт, который система сама считает непроверенным
            self.unapprove_candidate(candidate.id, candidate.review_note)
        return candidate

    def _review_issues(self, candidate: ExtractionCandidate) -> list[dict[str, str]]:
        """Все претензии к кандидату в разборном виде: код, поле, формулировка.

        По коду интерфейс фильтрует очередь, по полю подсвечивает строку формы.
        Пустой список означает, что кандидат проходит ворота и станет фактом.
        """
        issues = candidate_field_issues(candidate.payload)
        if candidate.source is None:
            # Инвариант системы: факт существует только со ссылкой на первоисточник
            issues.append({"code": "source_missing", "field": "source",
                           "label": "нет ссылки на фрагмент-источник"})
        elif candidate.payload.get("quote_validated") is not True:
            issues.append({"code": "quote_unconfirmed", "field": "quote",
                           "label": "цитата не подтверждена текстом источника"})
        issues.extend((candidate.payload.get("number_validation") or {}).get("issues_detail", []))
        return issues

    # Поля кандидата, которые эксперт правит в очереди. Цитата и источник в список
    # НЕ входят намеренно: правка цитаты обесценила бы проверку «цитата есть в
    # документе» — её можно было бы подогнать под факт
    EDITABLE_FIELDS = (
        "material", "property", "process", "sample", "lab", "team", "equipment",
        "temperature_c", "duration_h", "effect_direction", "effect_value", "effect_unit",
        "result_value", "result_unit",
    )

    def update_candidate_fields(self, candidate_id: str, fields: dict[str, Any]) -> ExtractionCandidate:
        """Правка полей кандидата экспертом с повторным прогоном проверок.

        Правка живёт в кандидате, а не в факте: иначе повторная обработка
        документа затёрла бы работу эксперта. Помеченный правкой кандидат
        пере-извлечением не перезаписывается (см. reprocess_document).
        """
        candidate = self.candidates[candidate_id]
        unknown = set(fields) - set(self.EDITABLE_FIELDS)
        if unknown:
            raise ValueError("Недопустимые поля: " + ", ".join(sorted(unknown)))
        for field, value in fields.items():
            candidate.payload[field] = value
        candidate.payload["edited_by_human"] = True
        # add_candidate перепроверит числа и поля, пересчитает ворота и,
        # если всё сошлось, сам создаст факт
        return self.add_candidate(candidate)

    def approve_candidate(self, candidate_id: str) -> Fact:
        candidate = self.candidates[candidate_id]
        if candidate.source is None:
            raise SourceRequiredError("Факт не может быть утвержден без ссылки на source fragment.")
        candidate.status = CandidateStatus.approved
        fact = fact_from_candidate(self.normalizer, candidate)
        self._mark_conflicts(fact)
        self.facts[fact.id] = fact
        self._persist_candidate(candidate)
        self.persist_fact(fact)
        self._project_semantics(fact, candidate)
        return fact

    def unapprove_candidate(self, candidate_id: str, note: str | None = None) -> ExtractionCandidate:
        """Снимает утверждение: факт уходит из графа, кандидат возвращается в очередь.

        Первоисточник не трогается — кандидат со всем разбором, фрагмент и документ
        остаются. Идентификатор факта выводится из идентификатора кандидата, поэтому
        повторное подтверждение вернёт то же утверждение с тем же id, и ссылки на
        него снова разрешатся.
        """
        candidate = self.candidates[candidate_id]
        fact_id = f"claim-{candidate_id.replace('candidate-', '')}"
        self.facts.pop(fact_id, None)
        if self.postgres_sink:
            self.postgres_sink.delete_fact(fact_id)
        if self.graph_sink:
            self.graph_sink.delete_fact(fact_id)
        # У соседей не должно остаться ссылки в пустоту: снимаем пометку
        # противоречия, а статус возвращаем, если противоречить стало нечему
        for other in list(self.facts.values()):
            if fact_id in other.conflicts_with:
                other.conflicts_with = [fid for fid in other.conflicts_with if fid != fact_id]
                if not other.conflicts_with and other.status == "conflicting":
                    other.status = "approved"
                self.persist_fact(other)
        candidate.status = CandidateStatus.pending_review
        candidate.review_note = note
        self._persist_candidate(candidate)
        return candidate

    def reject_candidate(self, candidate_id: str, note: str | None = None) -> ExtractionCandidate:
        candidate = self.candidates[candidate_id]
        candidate.status = CandidateStatus.rejected
        candidate.review_note = note
        self._persist_candidate(candidate)
        return candidate

    def _mark_conflicts(self, fact: Fact) -> None:
        """Фиксирует противоречие: тот же материал и свойство, противоположный эффект.

        Оба факта остаются в базе без изменений — статус conflicting лишь
        помечает противоречие и хранит ссылки на конфликтующие факты.
        """
        fact_direction = normalize_effect_direction(fact.effect_direction)
        opposite = {"increase": "decrease", "decrease": "increase"}.get(fact_direction)
        if opposite is None:
            return
        fact_key = self._fact_conflict_key(fact)
        # list(): факты добавляются из фоновых воркеров параллельно
        for other in list(self.facts.values()):
            other_direction = normalize_effect_direction(other.effect_direction)
            if (
                self._fact_conflict_key(other) == fact_key
                and other_direction == opposite
            ):
                fact.status = other.status = "conflicting"
                if other.id not in fact.conflicts_with:
                    fact.conflicts_with.append(other.id)
                if fact.id not in other.conflicts_with:
                    other.conflicts_with.append(fact.id)
                self.persist_fact(other)

    def _fact_conflict_key(self, fact: Fact) -> tuple[str, str]:
        material = self.normalizer.normalize_entity(fact.material) or fact.material
        property_name = self.normalizer.normalize_entity(fact.property) or fact.property
        return canonical_text(material), canonical_text(property_name)

    def _project_semantics(self, fact: Fact, candidate: ExtractionCandidate) -> None:
        """Переносит извлечённые сущности и связи онтологии из payload в Neo4j."""
        if not self.graph_sink:
            return
        # Сущности с именами-заглушками ('не указано', 'unknown'…) в граф не попадают
        entities = [
            {"type": item.get("type"), "name": clean_extracted(self.normalizer.normalize_entity(clean_extracted(item.get("name"))))}
            for item in candidate.payload.get("entities", [])
            if isinstance(item, dict)
        ]
        entities = [entity for entity in entities if entity["name"]]
        # Ребро, у которого имя одного из концов — заглушка, также не проецируется
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

    # --- Семантический индекс и поиск ---

    def _embed_chunk(self, chunk: list[SourceFragment], attempts: int = 3) -> list[list[float]]:
        """Векторы пачки фрагментов с повтором при обрыве.

        Сервис эмбеддингов может перезапуститься под нагрузкой (модель занимает
        около 2 ГБ), и одиночный обрыв не должен оставлять документ частично
        проиндексированным. Исчерпав попытки, ошибка выбрасывается наружу:
        вызывающий слой обязан пометить обработку failed, а не отчитаться об
        успехе с непроиндексированными фрагментами.
        """
        texts = [fragment.normalized_text for fragment in chunk]
        for attempt in range(1, attempts + 1):
            try:
                return self.embedder.embed(texts)
            except Exception as exc:  # noqa: BLE001 — важен любой обрыв, не только сетевой
                if attempt == attempts:
                    log.error("Эмбеддинги: пачка не посчитана за %d попыток: %s", attempts, exc)
                    raise
                pause = 2 ** attempt
                log.warning("Эмбеддинги: обрыв (%s), повтор через %d с", exc, pause)
                time.sleep(pause)
        return []  # недостижимо: цикл либо возвращает вектор, либо выбрасывает

    def index_fragments(self, fragments: list[SourceFragment]) -> None:
        # Пачками: большой документ не укладывается в таймаут одного запроса,
        # а результат фиксируется по мере готовности, а не в конце.
        # 16 длинных фрагментов на CPU укладываются в таймаут с запасом
        for start in range(0, len(fragments), 16):
            chunk = fragments[start : start + 16]
            vectors = self._embed_chunk(chunk)
            new_vectors: dict[str, list[float]] = {}
            for fragment, vector in zip(chunk, vectors):
                new_vectors[fragment.id] = vector
            if self.postgres_sink:
                self.postgres_sink.upsert_vectors(new_vectors, self.embedder.name)
            self.vectorized_fragment_ids.update(new_vectors)

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

    def get_graph(self, facts: list[Fact] | None = None) -> GraphPayload:
        return build_graph(self, facts)

    # --- Запись во внешние хранилища ---

    def persist_document(self, document: DocumentRecord, version: DocumentVersion) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_document(document, version)

    def persist_current(self, document: DocumentRecord) -> None:
        """Персист документа вместе с его текущей версией; версии нет в сторе —
        тихий пропуск (писать документ без версии некуда)."""
        version = self.versions.get(document.current_version_id)
        if version is not None:
            self.persist_document(document, version)

    def persist_document_quiet(self, document: DocumentRecord) -> None:
        """Персист документа, не прерывающий вызывающий код (бэкфилы, фоновые
        потоки): признаки справочные, сбой записи фиксируется в логе, попытка
        повторится при следующем старте."""
        try:
            self.persist_current(document)
        except Exception:
            log.exception("Не удалось сохранить документ %s", document.id)

    def persist_fact(self, fact: Fact) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_fact(fact)
        if self.graph_sink:
            self.graph_sink.upsert_fact(fact)

    def _persist_fragments(self, fragments: list[SourceFragment]) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_fragments(fragments)

    def _persist_candidate(self, candidate: ExtractionCandidate) -> None:
        if self.postgres_sink:
            self.postgres_sink.upsert_candidate(candidate)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
