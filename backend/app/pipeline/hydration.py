"""Восстановление состояния из PostgreSQL при старте backend и бэкфилы.

Всё, что выполняется один раз при подъёме процесса: загрузка стора из БД и
дозаполнение атрибутов, появившихся позже уже загруженных данных. Медленные
шаги (LLM-классификация, переиндексация, PDF-превью) уходят в фоновые потоки —
старт не блокируется. Каждый бэкфил идемпотентен: повторный старт до завершения
работы просто продолжает её.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from app.pipeline.document_traits import detect_origin, extract_publication_year
from app.pipeline.normalization import clean_extracted, normalize_effect_direction, slug
from app.pipeline.office_render import PREVIEW_SOURCE_EXTENSIONS
from app.pipeline.parsers import extension_of
from app.schemas import DocumentRecord, SourceFragment

if TYPE_CHECKING:
    from app.storage import ApplicationStore

log = logging.getLogger(__name__)


def hydrate(store: ApplicationStore) -> None:
    """Восстанавливает состояние из PostgreSQL после перезапуска backend-а."""
    if not store.postgres_sink or not store.postgres_sink.enabled:
        return
    state = store.postgres_sink.load_state()
    store.documents.update(state["documents"])
    store.versions.update(state["versions"])
    store.fragments.update(state["fragments"])
    store.candidates.update(state["candidates"])
    store.facts.update(state["facts"])
    store.fragment_vectors.update(state["vectors"])
    # Признаки документов, загруженных до появления эвристики, дополняются
    # синхронно: расчёт по фрагментам в памяти, объём — единицы документов.
    # Группировка фрагментов по документу делается один раз на оба бэкфила,
    # а не полным сканом стора на каждый документ
    fragments_by_document = store.fragments_by_document()
    backfill_document_traits(store, fragments_by_document)
    # Год издания — отдельным проходом: документ мог получить is_scientific
    # до появления поля year, поэтому бэкфил года не совмещён с traits
    backfill_document_years(store, fragments_by_document)
    # Значения-заглушки ('не указано', 'unknown'…) в ранее сохранённых фактах
    # очищаются в хранимых полях единожды при старте (идемпотентно)
    backfill_junk_facts(store)
    # Тип и научность (LLM по титульнику) — в фоне: вызовы медленные,
    # старт не блокируется; документы без вердикта дооцениваются здесь же
    # при каждом старте, пока LLM не ответит
    if any(document.doc_type is None for document in store.documents.values()):
        _in_background(classify_documents_llm, "classify-docs", store, fragments_by_document)
    # Фрагменты без векторов (например, после смены модели эмбеддингов)
    # индексируются заново в фоне: старт backend не блокируется
    missing = [fragment for fid, fragment in store.fragments.items() if fid not in store.fragment_vectors]
    if missing:
        _in_background(reindex_missing, "reindex-missing", store, missing)
    # DOCX/PPTX, загруженные до появления PDF-превью, конвертируются в фоне
    # (как reindex_missing): старт backend не блокируется, идемпотентно —
    # документ с уже проставленным preview_object пропускается
    if store.file_storage and store.file_storage.enabled:
        no_preview = [
            document for document in list(store.documents.values())
            if document.storage_object
            and document.preview_object is None
            and extension_of(document.filename) in PREVIEW_SOURCE_EXTENSIONS
        ]
        if no_preview:
            _in_background(backfill_previews, "preview-backfill", store, no_preview)


def _in_background(target, name: str, *args: Any) -> None:
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def backfill_document_traits(
    store: ApplicationStore, fragments_by_document: dict[str, list[SourceFragment]] | None = None
) -> None:
    """Проставляет origin (эвристика по языку) документам без признака."""
    if fragments_by_document is None:
        fragments_by_document = store.fragments_by_document()
    for document in list(store.documents.values()):
        if document.origin is not None:
            continue
        fragments = fragments_by_document.get(document.id)
        if not fragments:
            continue
        document.origin = detect_origin(fragments)
        store.persist_document_quiet(document)


def classify_documents_llm(
    store: ApplicationStore, fragments_by_document: dict[str, list[SourceFragment]]
) -> None:
    """Фоновая LLM-классификация (тип + научность) документов без вердикта.

    Работает в отдельном потоке: LLM-вызовы медленные, старт backend не
    блокируется. Идемпотентно — классифицированный документ пропускается;
    недоступная LLM оставляет None (прочерк в UI), попытка повторится
    при следующем старте. Два отказа LLM подряд останавливают обход:
    при недоступном каскаде повторные вызовы лишь расходуют таймауты
    на весь корпус.
    """
    pending = [d for d in list(store.documents.values()) if d.doc_type is None]
    done = 0
    misses = 0
    for document in pending:
        fragments = fragments_by_document.get(document.id)
        if not fragments:
            continue
        if store.apply_traits(document, fragments):
            store.persist_document_quiet(document)
            done += 1
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                log.warning("Классификация документов остановлена: LLM недоступна, "
                            "продолжим при следующем старте")
                break
    if pending:
        log.info("Классификация документов: %d/%d получили тип и научность", done, len(pending))


def backfill_document_years(
    store: ApplicationStore, fragments_by_document: dict[str, list[SourceFragment]] | None = None
) -> None:
    """Пересчитывает year ВСЕХ документов по актуальной эвристике
    (extract_publication_year: имя файла имеет приоритет над текстом,
    зона поиска в тексте — первые две страницы).

    Год записывается только этой эвристикой (ручного ввода нет), поэтому
    полный пересчёт на старте безопасен и поддерживает таблицу документов
    в соответствии с текущей версией правила. Операция незатратна: расчёт
    по фрагментам в памяти; запись в БД — только для документов, у которых
    значение изменилось.
    """
    if fragments_by_document is None:
        fragments_by_document = store.fragments_by_document()
    changed = 0
    for document in list(store.documents.values()):
        fragments = fragments_by_document.get(document.id) or []
        year = extract_publication_year(fragments, document.filename)
        if year == document.year:
            continue
        document.year = year
        store.persist_document_quiet(document)
        changed += 1
    if changed:
        log.info("backfill: год издания пересчитан у %d документов", changed)


def backfill_junk_facts(store: ApplicationStore) -> None:
    """Очищает значения-заглушки текстовых полей существующих фактов до ''
    (единый clean_extracted) и персистит. Идемпотентно: уже очищенное поле
    не меняется. NB: upsert_fact при конфликте не обновляет текстовые
    колонки, поэтому в PG значения-заглушки сохраняются и после рестарта
    очистка выполняется заново."""
    cleaned = 0
    for fact in list(store.facts.values()):
        updates: dict[str, Any] = {}
        for field in ("material", "process", "sample", "lab", "team", "property"):
            value = getattr(fact, field)
            new_value = clean_extracted(value)
            if new_value != value:
                updates[field] = new_value
        # effect_direction 'unknown'/значение-заглушка → '' (такое направление в подпись не выводится)
        if normalize_effect_direction(fact.effect_direction) == "unknown" and fact.effect_direction != "":
            updates["effect_direction"] = ""
        equipment_clean = clean_extracted(fact.equipment) or None
        if equipment_clean != fact.equipment:
            updates["equipment"] = equipment_clean
        if "material" in updates:
            updates["material_id"] = f"material-{slug(updates['material'])}"
        if not updates:
            continue
        updated = fact.model_copy(update=updates)
        store.facts[fact.id] = updated
        try:
            store.persist_fact(updated)
        except Exception:
            # Гигиена справочная: сбой записи не прерывает старт, попытка повторится
            log.exception("Не удалось сохранить очищенный факт %s", fact.id)
        cleaned += 1
    if cleaned:
        log.info("backfill: очищено мусорных значений в %d фактах", cleaned)


def reindex_missing(store: ApplicationStore, fragments: list[SourceFragment]) -> None:
    try:
        store.index_fragments(fragments)
        log.info("hydrate: переиндексировано %d фрагментов", len(fragments))
    except Exception:
        log.exception("hydrate: переиндексация %d фрагментов не удалась", len(fragments))


def backfill_previews(store: ApplicationStore, documents: list[DocumentRecord]) -> None:
    """Фоновый бэкфил PDF-превью для существующих DOCX/PPTX: скачать оригинал
    из MinIO, сконвертировать, сохранить превью, персистнуть preview_object.
    Ошибки по одному документу логируются и не прерывают остальные."""
    built = 0
    for document in documents:
        try:
            assert document.storage_object is not None  # отфильтровано вызывающим
            original = store.file_storage.get_object(document.storage_object)
            if original is None:
                continue
            store.make_preview(document, original)
            if document.preview_object is None:
                continue
            store.persist_current(document)
            built += 1
        except Exception:
            log.exception("backfill: PDF-превью документа %s не создано", document.id)
    if built:
        log.info("backfill: PDF-превью создано для %d документов", built)
