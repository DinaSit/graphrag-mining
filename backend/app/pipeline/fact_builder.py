"""Сборка факта из утверждённого кандидата и проверка качества кандидата.

Инвариант системы: факт существует только со ссылкой на первоисточник —
кандидат без source не превращается в факт ни при каком уровне уверенности.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.pipeline.normalization import (
    DomainNormalizer,
    clean_extracted,
    float_or_none,
    is_junk_label,
    normalize_effect_direction,
    slug,
)
from app.schemas import ExtractionCandidate, Fact


class SourceRequiredError(ValueError):
    pass


def fact_from_candidate(normalizer: DomainNormalizer, candidate: ExtractionCandidate) -> Fact:
    payload = candidate.payload
    # Гигиена у источника: значения-заглушки ('не указано', 'unknown'…)
    # приводятся к '' единым clean_extracted, а не заменяются значением
    # по умолчанию
    material = clean_extracted(normalizer.normalize_entity(clean_extracted(payload.get("material"))))
    property_name = clean_extracted(normalizer.normalize_entity(clean_extracted(payload.get("property"))))
    source = candidate.source
    if source is None:
        raise SourceRequiredError("Факт не может быть утвержден без ссылки на source fragment.")
    fact_id = f"claim-{candidate.id.replace('candidate-', '')}"
    effect_direction = normalize_effect_direction(payload.get("effect_direction"))
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
        confidence=extraction_confidence(payload),
        source=source,
    )


# Поля, без которых утверждение бессмысленно: о чём и что именно утверждается
_REQUIRED_FIELDS = (("material", "материал"), ("property", "свойство"))


def candidate_field_issues(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Незаполненные обязательные поля в разборном виде: код, поле, формулировка."""
    def missing(value: Any) -> bool:
        return value is None or is_junk_label(str(value))

    return [
        {"code": "field_missing", "field": field, "label": f"{name} не извлечён" if field == "material"
         else f"{name} не извлечено"}
        for field, name in _REQUIRED_FIELDS if missing(payload.get(field))
    ]


# Проверки извлечения, из которых складывается уверенность факта. Все три
# механические: их результат можно перепроверить по документу, не спрашивая
# модель. Самооценка модели (candidate.confidence) в уверенность факта не входит —
# она осталась порогом автоприёма кандидата
def extraction_confidence(payload: dict[str, Any]) -> float:
    """Доля пройденных проверок извлечения: 0, 0.333, 0.667 или 1.0.

    1. Цитата найдена в тексте фрагмента-источника.
    2. Числа факта подтверждены текстом источника (чисел нет — проверка пройдена).
    3. Извлечены ключевые поля: материал и свойство (candidate_field_issues).

    Проверка, которую не удалось выполнить (нет фрагмента в сторе), считается
    непройденной: «не проверено» — это не «проверено».
    """
    passed = 0
    if payload.get("quote_validated") is True:
        passed += 1
    if (payload.get("number_validation") or {}).get("validated", True):
        passed += 1
    if not candidate_field_issues(payload):
        passed += 1
    return round(passed / 3, 3)
