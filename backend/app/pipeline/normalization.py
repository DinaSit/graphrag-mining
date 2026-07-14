from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def canonical_text(value: str) -> str:
    """Каноническая форма текста для сравнения сущностей и фактов: casefold +
    ё→е + замена последовательностей пробельных символов одним пробелом
    (общая для query- и storage-слоёв)."""
    return " ".join(value.strip().casefold().replace("ё", "е").split())


def slug(value: str) -> str:
    """Слаг для стабильных id узлов/сущностей: не-буквоцифры → дефисы.
    Единственная реализация — id в PG, Neo4j и парсерах обязаны совпадать."""
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part)


def float_or_none(value: Any) -> float | None:
    """Число из сырого значения LLM/таблицы: None/'' → None, запятая → точка,
    неразборчивое → None. Общая для storage-, providers- и validation-слоёв."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def direction_label(direction: str) -> str:
    """Русская подпись направления эффекта (узлы графа, таблица экспериментов);
    неизвестное значение отдаётся как есть."""
    return {"increase": "рост", "decrease": "снижение", "neutral": "без изменений"}.get(direction, direction)


def normalize_effect_direction(value: Any) -> str:
    """Канон направления эффекта (increase/decrease/neutral) из русских и
    английских вариантов, ё-толерантно. Единственная копия: используется
    нормализацией фактов (storage) и поиском противоречий (query) — единая
    реализация исключает расхождение наборов алиасов между слоями
    (например, потерю no_change/increased)."""
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


# Значения-заглушки в подписях КГ: единый список для гигиены данных у источника
# (clean_extracted), для подписей узлов графа (_is_junk_label в storage.py)
# и для удаления таких узлов из Neo4j (_cleanup_junk_nodes). trim/casefold/ё→е.
JUNK_VALUES = frozenset({
    "", "не указано", "unknown", "n/a", "-", "нет данных", "none", "null",
    # значения по умолчанию при извлечении (см. _fact_from_candidate / _is_missing_value)
    "unknown material", "unknown property", "unknown process", "unknown lab",
})


def clean_extracted(value: str | None) -> str:
    """Гигиена извлечённого значения у источника: строка-заглушка из КГ-списка
    ('не указано', 'unknown', 'n/a', '-', 'нет данных', 'none', 'null' и
    производные) преобразуется в ''. Остальные значения возвращаются с
    обрезанными пробелами.
    Единственная точка маппинга — используется при создании факта, в проекции
    семантики и в бэкфиле существующих данных."""
    text = str(value or "").strip()
    if text.casefold().replace("ё", "е") in JUNK_VALUES:
        return ""
    return text


class DomainNormalizer:
    """Канонизация имён сущностей по domain/default/synonyms.csv
    (владелец словаря — инженер знаний)."""

    def __init__(self, domain_dir: Path):
        self.domain_dir = domain_dir
        self.aliases = self._load_aliases(domain_dir / "synonyms.csv")

    def normalize_entity(self, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.strip().lower().replace("ё", "е").split())
        return self.aliases.get(compact, value.strip())

    def _load_aliases(self, path: Path) -> dict[str, str]:
        aliases: dict[str, str] = {}
        if not path.exists():
            return aliases
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                canonical = row.get("canonical", "").strip()
                alias = row.get("alias", "").strip()
                if canonical and alias:
                    aliases[alias.lower().replace("ё", "е")] = canonical
                    aliases[canonical.lower().replace("ё", "е")] = canonical
        return aliases
