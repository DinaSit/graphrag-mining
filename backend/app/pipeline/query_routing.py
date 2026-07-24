"""Маршрутизация вопроса до запуска конвейера.

Два решения принимаются здесь: относится ли вопрос к предметной области
(иначе полный поиск не запускается вовсе) и достаточно ли быстрой ветки
классического RAG вместо графа с планировщиком.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.pipeline.normalization import DomainNormalizer, index_token
from app.schemas import QueryRequest, QueryResponse

if TYPE_CHECKING:
    from app.storage import ApplicationStore

# Маркеры сравнения в вопросе — единственный текстовый признак полного
# пайплайна помимо цифр (см. needs_full_pipeline; дополнительные rule-шаблоны
# намеренно не вводятся)
_COMPARE_MARKERS_RE = re.compile(r"сравн|отлич| vs |против")

# Стемы базовой R&D-лексики: словарь синонимов покрывает термины домена,
# стемы добавляют морфологию и общеисследовательские слова, которых в нём нет
_DOMAIN_STEMS = frozenset({
    "метод", "экспер", "исслед", "публика", "отчет", "лаборат", "температур",
    "концентрац", "скорост", "давлен", "расход", "материал", "процесс",
    "оборудован", "установк", "технолог", "параметр", "режим", "источник",
    "статья", "патент", "вывод", "эффект", "практик", "раствор", "очистк",
    "вода", "воды", "руда", "руды", "металл", "сплав", "шлак", "штейн",
})


def needs_full_pipeline(request: QueryRequest) -> bool:
    """Единственный признак маршрутизации fast/full (без rule-шаблонов):
    цифры, маркеры сравнения или явные фильтры => полный пайплайн."""
    text = request.question.lower().replace("ё", "е")
    if any(ch.isdigit() for ch in text):
        return True
    if _COMPARE_MARKERS_RE.search(text):
        return True
    filters = request.filters
    return bool(filters.materials or filters.properties or filters.laboratories or filters.confidence_min > 0)


class OfftopicRouter:
    """Отсев вопросов вне предметной области. Словарь домена строится из
    synonyms.csv один раз и кэшируется на время жизни объекта."""

    def __init__(self, normalizer: DomainNormalizer):
        self.normalizer = normalizer
        self._domain_terms: tuple[set[str], set[str]] | None = None

    def _domain_vocabulary(self) -> tuple[set[str], set[str]]:
        """Термины домена из словаря синонимов: (короткие — точное совпадение,
        5-буквенные префиксы длинных — матчат морфологические формы)."""
        if self._domain_terms is None:
            exact: set[str] = set()
            prefixes: set[str] = set()
            for alias, canonical in getattr(self.normalizer, "aliases", {}).items():
                for term in (alias, canonical):
                    for word in re.findall(r"[a-zа-я0-9]+", term.lower().replace("ё", "е")):
                        index_token(word, exact, prefixes)
            self._domain_terms = (exact, prefixes)
        return self._domain_terms

    def is_offtopic(self, question: str) -> bool:
        """Разговорные вопросы и вопросы вне предметной области («как дела?»)
        не обрабатываются полным пайплайном.

        Проверяется на границе API (/ask), а не внутри answer(): пайплайн
        остаётся полным для прямых вызовов. Эвристика ошибается только в
        безопасную сторону: цифры, единицы, длинный текст или любой доменный
        термин отправляют вопрос в пайплайн.
        """
        text = question.strip().lower().replace("ё", "е")
        if not text:
            return True
        if any(ch.isdigit() for ch in text):
            return False
        if len(text) > 80:
            return False
        exact, prefixes = self._domain_vocabulary()
        for token in re.findall(r"[a-zа-я0-9]+", text):
            if len(token) <= 4:
                if token in exact:
                    return False
            elif token[:5] in prefixes or any(token.startswith(stem) for stem in _DOMAIN_STEMS):
                return False
        return True


def offtopic_response(store: ApplicationStore) -> QueryResponse:
    return QueryResponse(
        summary=(
            "Вопрос не похож на запрос к базе знаний, поэтому полный поиск не запускался. "
            "Я отвечаю на вопросы по горно-металлургическим R&D-материалам: методы, материалы, "
            "процессы, параметры, эксперименты, источники. Например: «Какие методы обессоливания "
            "воды подходят, если сульфаты 200–300 мг/л, а требуемый сухой остаток ≤1000 мг/дм³?»"
        ),
        experiments=[],
        sources=[],
        graph=store.get_graph(facts=[]),
        contradictions=[],
        gaps=[],
        confidence=0.0,
        evidence_status="none",
        offtopic=True,
    )
