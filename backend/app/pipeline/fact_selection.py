"""Отбор, фильтрация и ранжирование фактов под вопрос + представления фактов.

Работает поверх видимых фактов стора: скрытые документы не участвуют ни в
пословном матчинге, ни в косвенном поиске.
"""
from __future__ import annotations

import re
from statistics import mean
from typing import TYPE_CHECKING

from app.pipeline.normalization import (
    DomainNormalizer,
    canonical_text,
    direction_label,
    index_token,
)
from app.schemas import ExperimentRow, Fact, ParsedQuestion, QueryRequest, SourceRef

if TYPE_CHECKING:
    from app.storage import ApplicationStore


def match_facts_by_words(store: ApplicationStore, question: str) -> list[Fact]:
    """Пословный матчинг фактов без LLM-планировщика (быстрая ветка):
    токены вопроса ищутся напрямую по вершинам фактов
    (material/process/property). Короткие токены (<=4) — точное совпадение,
    длинные — по 5-буквенному префиксу (покрывает морфологию); токены из
    словаря синонимов раскрываются каноном, чтобы «electrowinning» находил
    факты про электроэкстракцию."""
    aliases = {
        canonical_text(alias): canonical
        for alias, canonical in getattr(store.normalizer, "aliases", {}).items()
    }
    exact: set[str] = set()
    prefixes: set[str] = set()
    text = question.lower().replace("ё", "е")
    for token in re.findall(r"[a-zа-я0-9]+", text):
        canonical = aliases.get(token)
        # Одно-двухбуквенные токены — шум («в», «на»), кроме аббревиатур
        # из словаря синонимов (например, «ВП»)
        if len(token) <= 2 and canonical is None:
            continue
        index_token(token, exact, prefixes)
        if canonical is not None:
            for word in re.findall(r"[a-zа-я0-9]+", canonical.lower().replace("ё", "е")):
                index_token(word, exact, prefixes)

    if not exact and not prefixes:
        return []
    matched: list[Fact] = []
    # visible_facts: скрытые документы не участвуют в матчинге
    for fact in store.visible_facts():
        haystack = f"{fact.material} {fact.process} {fact.property}".lower().replace("ё", "е")
        for token in re.findall(r"[a-zа-я0-9]+", haystack):
            if token in exact or token[:5] in prefixes:
                matched.append(fact)
                break
    matched.sort(key=lambda fact: fact.confidence, reverse=True)
    return matched[:30]


def entity_key(normalizer: DomainNormalizer, value: str | None) -> str | None:
    """Ключ сравнения сущностей: канонизация нормалайзером + casefold + ё→е.

    Применяется к ОБЕИМ сторонам сравнения: «медный концентрат» из вопроса
    должен совпасть с «Медный концентрат» из документа и без synonyms.csv.
    """
    if not value:
        return None
    return canonical_text(normalizer.normalize_entity(value) or value)


def filter_facts_legacy(
    normalizer: DomainNormalizer, facts: list[Fact], request: QueryRequest, parsed: ParsedQuestion
) -> list[Fact]:
    confidence_min = max(request.confidence_min, request.filters.confidence_min)
    result: list[Fact] = []
    material_key = entity_key(normalizer, parsed.material)
    property_key = entity_key(normalizer, parsed.property)
    filter_materials = {entity_key(normalizer, item) for item in request.filters.materials if item}
    filter_properties = {entity_key(normalizer, item) for item in request.filters.properties if item}
    filter_labs = set(request.filters.laboratories)
    for fact in facts:
        if fact.confidence < confidence_min:
            continue
        if fact.is_hypothesis and not request.include_hypotheses:
            continue
        if material_key and entity_key(normalizer, fact.material) != material_key:
            continue
        if property_key and entity_key(normalizer, fact.property) != property_key:
            continue
        if filter_materials and entity_key(normalizer, fact.material) not in filter_materials:
            continue
        if filter_properties and entity_key(normalizer, fact.property) not in filter_properties:
            continue
        if filter_labs and fact.lab not in filter_labs:
            continue
        result.append(fact)
    return result


def merge_unique(*fact_lists: list[Fact]) -> list[Fact]:
    seen: set[str] = set()
    merged: list[Fact] = []
    for facts in fact_lists:
        for fact in facts:
            if fact.id not in seen:
                merged.append(fact)
                seen.add(fact.id)
    return merged


def in_year_range(store: ApplicationStore, document_id: str, parsed: ParsedQuestion) -> bool:
    """Попадает ли документ-источник в заданный вопросом диапазон лет.

    Документ без года не проходит: подтвердить попадание нечем, а показать
    его как ответ «за последние 5 лет» значило бы выдать непроверенное за
    проверенное. Сколько материала отсеяно, сообщает find_gaps.
    """
    if parsed.year_min is None and parsed.year_max is None:
        return True
    document = store.documents.get(document_id)
    year = document.year if document else None
    if year is None:
        return False
    return ((parsed.year_min is None or year >= parsed.year_min)
            and (parsed.year_max is None or year <= parsed.year_max))


def filter_by_year(store: ApplicationStore, facts: list[Fact], parsed: ParsedQuestion) -> list[Fact]:
    return [f for f in facts if in_year_range(store, f.source.document_id, parsed)]


def rank_fact(normalizer: DomainNormalizer, parsed: ParsedQuestion):
    material_key = entity_key(normalizer, parsed.material)
    property_key = entity_key(normalizer, parsed.property)

    def rank(fact: Fact) -> float:
        score = fact.confidence
        if material_key and entity_key(normalizer, fact.material) == material_key:
            score += 0.3
        if property_key and entity_key(normalizer, fact.property) == property_key:
            score += 0.25
        if parsed.process and parsed.process.lower() in fact.process.lower():
            score += 0.2
        return score
    return rank


def indirect_search(store: ApplicationStore, parsed: ParsedQuestion) -> tuple[list[Fact], list[str]]:
    """Прямых данных нет: ищем по одному ослабленному признаку за раз (материал ИЛИ процесс)."""
    hypotheses: list[str] = []
    loose_terms = [parsed.material, parsed.process, parsed.equipment] + [e.name for e in parsed.entities]
    loose_terms = [t for t in loose_terms if t]
    found: list[Fact] = []
    # visible_facts: скрытые документы не дают и косвенных кейсов;
    # выборка одна на все ослабленные признаки, а не в каждой итерации
    visible = store.visible_facts()
    for term in loose_terms:
        normalized = store.normalizer.normalize_entity(term) or term
        partial = [f for f in visible if normalized.lower() in f.material.lower() or normalized.lower() in f.process.lower()]
        if partial:
            found.extend(partial)
            hypotheses.append(
                f"Прямых данных по полной комбинации не найдено. Найдены косвенные кейсы по «{term}» "
                f"({len(partial)} факт(ов)) — не подтверждённый вывод, гипотеза для проверки."
            )
    unique = merge_unique(found)
    # Косвенные находки помечаются гипотезами (копии — базу не трогаем)
    unique = [f.model_copy(update={"is_hypothesis": True}) for f in unique]
    return unique, hypotheses


def row_from_fact(fact: Fact) -> ExperimentRow:
    value = direction_label(fact.effect_direction)
    if fact.effect_value is not None:
        value += f" на {fact.effect_value:g}{fact.effect_unit or ''}"
    return ExperimentRow(
        experiment_id=fact.experiment_id, material=fact.material, sample=fact.sample,
        process=fact.process, temperature_c=fact.temperature_c, duration_h=fact.duration_h,
        property=fact.property, result_value=fact.result_value, result_unit=fact.result_unit,
        effect=value, lab=fact.lab, confidence=fact.confidence, source=fact.source,
    )


def collect_sources(facts: list[Fact]) -> list[SourceRef]:
    """Источники фактов без дублей (по паре документ+фрагмент), в порядке фактов."""
    sources: list[SourceRef] = []
    seen: set[tuple[str, str]] = set()
    for fact in facts:
        key = (fact.source.document_id, fact.source.fragment_id)
        if key not in seen:
            sources.append(fact.source)
            seen.add(key)
    return sources


def mean_confidence(facts: list[Fact]) -> float:
    """Уверенность ответа: средняя уверенность фактов (3 знака), пустой список — 0.0.
    Единственная копия формулы для быстрой ветки, полного пайплайна и демоута."""
    return round(mean([fact.confidence for fact in facts]), 3) if facts else 0.0
