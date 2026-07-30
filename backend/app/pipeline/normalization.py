from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import yaml


def canonical_text(value: str) -> str:
    """Каноническая форма текста для сравнения сущностей и фактов: casefold +
    ё→е + замена последовательностей пробельных символов одним пробелом
    (общая для query- и storage-слоёв)."""
    return " ".join(value.strip().casefold().replace("ё", "е").split())


# Кириллические буквы, неотличимые от латинских по начертанию. В отраслевых
# документах химические формулы набирают вперемешку: «CaO» латиницей и «СаО»
# кириллицей — одно вещество, но разные строки, а значит и разные узлы графа
_HOMOGLYPHS = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
}


def fold_homoglyphs(value: str) -> str:
    """Приводит к латинице слова, набранные ТОЛЬКО буквами-двойниками.

    Слово, где есть хоть одна буква без латинского двойника («селен», «шлак»,
    «Лист1»), остаётся кириллическим: приводить его не к чему, а замена части
    букв породила бы третье написание вдобавок к двум имеющимся.
    """
    out: list[str] = []
    for word in re.split(r"([^\w]|_)", value):
        letters = [char for char in word if char.isalpha()]
        if letters and all(char in _HOMOGLYPHS or char.isascii() for char in letters):
            out.append("".join(_HOMOGLYPHS.get(char, char) for char in word))
        else:
            out.append(word)
    return "".join(out)


def slug(value: str) -> str:
    """Слаг для стабильных id узлов/сущностей: буквы-двойники приводятся к
    латинице, не-буквоцифры → дефисы.
    Единственная реализация — id в PG, Neo4j и парсерах обязаны совпадать."""
    cleaned = "".join(char.lower() if char.isalnum() else "-"
                      for char in fold_homoglyphs(value.strip()))
    return "-".join(part for part in cleaned.split("-") if part)


def index_token(token: str, exact: set[str], prefixes: set[str]) -> None:
    """Единое правило индексации термина: токен до 4 букв — в точные совпадения,
    длиннее — 5-буквенный префикс (покрывает морфологические формы). Общая точка
    для словаря доменной лексики (query_routing) и пословного матчинга фактов
    (fact_selection) — правила обязаны совпадать, иначе маршрутизация вопросов
    вне предметной области и матчинг перестанут быть согласованными."""
    if len(token) <= 4:
        exact.add(token)
    else:
        prefixes.add(token[:5])


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
# (clean_extracted), для подписей узлов графа (is_junk_label) и для удаления
# таких узлов из Neo4j (_cleanup_junk_nodes). trim/casefold/ё→е.
JUNK_VALUES = frozenset({
    "", "не указано", "unknown", "n/a", "-", "нет данных", "none", "null",
    # значения по умолчанию при извлечении (см. fact_from_candidate)
    "unknown material", "unknown property", "unknown process", "unknown lab",
})


def is_junk_label(value: str | None) -> bool:
    """Является ли значение заглушкой (JUNK_VALUES). Проверка подписи, в отличие
    от clean_extracted, ничего не преобразует — нужна там, где решается, создавать
    ли узел графа и считать ли поле кандидата извлечённым."""
    return str(value or "").strip().casefold().replace("ё", "е") in JUNK_VALUES


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


class RegionIndex:
    """Регионы утверждений по domain/default/regions.yaml.

    Держится отдельно от synonyms.csv намеренно: маркерами региона служат в том
    числе названия заводов и городов (Nikkelverk, Sudbury), а они уже описаны в
    словаре синонимов как Facility. Попади они туда же ещё и регионами,
    normalize_entity начал бы превращать название завода в название страны.
    """

    def __init__(self, domain_dir: Path):
        self.regions: list[dict[str, Any]] = self._load(domain_dir / "regions.yaml")
        self.domestic = [r["name"] for r in self.regions if r.get("domestic")]
        self.foreign = [r["name"] for r in self.regions if not r.get("domestic")]

    def regions_in(self, text: str) -> list[str]:
        """Регионы, упомянутые в тексте. Маркер сопоставляется как начало слова:
        морфологические формы («России», «российский») покрываются одним
        маркером, а совпадения внутри слова исключены."""
        folded = canonical_text(text)
        return [
            region["name"] for region in self.regions
            if any(re.search(rf"\b{re.escape(marker)}", folded) for marker in region["markers"])
        ]

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return [
            {"name": item["name"],
             "domestic": bool(item.get("domestic")),
             "markers": [canonical_text(m) for m in item.get("markers", [])]}
            for item in data if item.get("name")
        ]


class DomainNormalizer:
    """Канонизация имён сущностей по domain/default/synonyms.csv."""

    def __init__(self, domain_dir: Path):
        self.domain_dir = domain_dir
        self.aliases = self._load_aliases(domain_dir / "synonyms.csv")
        self.regions = RegionIndex(domain_dir)

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
