"""Аналитика доказательной базы: противоречия, пробелы, статус и ответ без LLM.

Всё, что конвейер выводит из найденных фактов сам, без языковой модели.
Списки модели дописываются к этим результатам (merge_texts), а не заменяют их.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from app.pipeline.normalization import DomainNormalizer, canonical_text, normalize_effect_direction
from app.schemas import Fact


def find_contradictions(normalizer: DomainNormalizer, facts: list[Fact]) -> list[str]:
    groups: dict[tuple[str, str], list[Fact]] = {}
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    for fact in facts:
        material = normalizer.normalize_entity(fact.material) or fact.material
        property_name = normalizer.normalize_entity(fact.property) or fact.property
        key = (canonical_text(material), canonical_text(property_name))
        labels.setdefault(key, (material, property_name))
        groups.setdefault(key, []).append(fact)
    contradictions: list[str] = []
    seen_messages: set[str] = set()
    for key, group in groups.items():
        for first, second in combinations(group, 2):
            directions = {
                normalize_effect_direction(first.effect_direction),
                normalize_effect_direction(second.effect_direction),
            }
            if directions != {"increase", "decrease"}:
                continue
            if not comparable_conditions(first, second):
                continue
            material, property_name = labels[key]
            message = (
                f"{material}, {property_name}: разные источники показывают противоположный эффект "
                f"при сопоставимых условиях; лаборатории: {', '.join(sorted({first.lab, second.lab}))}."
            )
            if message not in seen_messages:
                seen_messages.add(message)
                contradictions.append(message)
    return contradictions


def comparable_conditions(first: Fact, second: Fact, temperature_tolerance_c: float = 5.0) -> bool:
    """Противоположные эффекты при разных условиях — не противоречие:
    рост твёрдости при 705 °C и падение при 790 °C физически согласованы
    (пик старения). Неуказанная температура считается «любой» и пересекается
    со всем; разные процессы делают пару несопоставимой.
    """
    if first.process and second.process and canonical_text(first.process) != canonical_text(second.process):
        return False
    if (
        first.temperature_c is not None
        and second.temperature_c is not None
        and abs(first.temperature_c - second.temperature_c) > temperature_tolerance_c
    ):
        return False
    return True


def find_gaps(parsed, facts: list[Fact], numeric_evidence: list[dict[str, Any]]) -> list[str]:
    gaps: list[str] = []
    if not facts and not numeric_evidence:
        return ["Нет подтверждённых фактов для заданной комбинации условий."]
    if facts:
        labs = {fact.lab for fact in facts}
        if len(labs) < 2:
            gaps.append("Результаты подтверждены менее чем двумя независимыми источниками.")
    # Факты по теме могут найтись, а численное подтверждение целевого
    # показателя — нет: это и есть пробел
    if parsed is not None and parsed.target and not numeric_evidence:
        gaps.append(f"Нет данных, напрямую подтверждающих целевой показатель «{parsed.target.parameter}».")
    return gaps


def degraded_summary(facts: list[Fact], search_hits, has_direct_facts: bool, llm_failed: bool) -> str:
    """Человекочитаемое summary без LLM: что реально нашлось в базе.

    Используется и когда LLM недоступна (llm_failed), и когда модель
    не вернула summary.
    """
    if has_direct_facts:
        body = f"В базе знаний найдено {len(facts)} факт(ов) по запросу — см. таблицу фактов и источники."
    elif facts:
        body = (f"Прямых фактов не найдено; есть {len(facts)} косвенных кейс(ов) по смежным понятиям — "
                "см. гипотезы.")
    elif search_hits:
        top = [
            f"«{hit.metadata.get('filename', hit.source.document_id)}» — {hit.text[:160].strip()}…"
            for hit in search_hits[:3]
        ]
        body = (f"Фактов в графе не найдено, но семантический поиск дал {len(search_hits)} "
                "релевантных фрагментов:\n- " + "\n- ".join(top))
    else:
        body = "В базе знаний ничего не найдено по этому запросу."
    if llm_failed:
        return "Ответ собран без языковой модели (см. причину выше). " + body
    return body


def evidence_status(has_direct_facts: bool, related_facts, search_hits) -> str:
    """Статус доказательной базы: прямые факты / смежные-поиск / пусто.
    Единая точка для финального ответа и SSE-предпросмотра:
    единственная реализация исключает расхождение встроенных копий условия."""
    if has_direct_facts:
        return "direct"
    return "partial" if related_facts or search_hits else "none"


def merge_texts(base: list[str], extra: list[str] | None) -> list[str]:
    """Дописывает списки модели к собранным конвейером: дубли убираются,
    порядок первого появления сохраняется."""
    return list(dict.fromkeys(base + extra)) if extra else base
