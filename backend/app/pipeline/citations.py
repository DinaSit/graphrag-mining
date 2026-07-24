"""Цитаты в ответе модели: номера → канонические id → SourceRef.

Модели показывается пронумерованный evidence pack, и цитирует она номерами:
номера она воспроизводит надёжно, длинные id искажает. Здесь номер переводится
в канонический [fragment-…] и резолвится в источник — то же, что подсвечивает
интерфейс (CITE_RE в index.html).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.schemas import SourceRef

if TYPE_CHECKING:
    from app.storage import ApplicationStore

# Цитаты-иды в тексте LLM: [fragment-…] / [claim-…] / (fragment-…), в т.ч.
# списком через запятую — тот же формат, что подсвечивает UI (CITE_RE в index.html)
_CITATION_RE = re.compile(
    r"[\[(]\s*((?:fragment|claim)-[\w./-]+(?:\s*,\s*(?:fragment|claim)-[\w./-]+)*)\s*[\])]"
)

# Номерные цитаты модели: [3] / [3, 7] / диапазон [5–10] (тире/дефис — модель
# иногда сжимает подряд идущие номера вопреки промпту). Переводятся в
# канонические [fragment-…] по citation_index; скобки, где ни один номер
# не резолвится (напр. год [2023] или диапазон лет), не трогаются.
_MARKER_RE = re.compile(
    r"\[\s*(\d+(?:\s*[–—-]\s*\d+)?(?:\s*,\s*\d+(?:\s*[–—-]\s*\d+)?)*)\s*\]"
)
_MARKER_RANGE_RE = re.compile(r"^(\d+)\s*[–—-]\s*(\d+)$")

# Максимальная ширина разворачиваемого диапазона [a–b]: защита от некорректных
# значений — диапазона лет [1998–2005] или галлюцинации [1–999]
# (evidence pack ≤ 25 позиций)
_MARKER_RANGE_CAP = 25

# Служебные префиксы гипотез («Непрямая гипотеза:», «Гипотеза (косвенная):»,
# «Косвенно:», «Гипотеза:», …): пометку «Косвенно» ставит UI, текстовый дубль
# от модели срезается в finalize (вторая линия защиты после ANSWER_SYSTEM_PROMPT)
_HYPOTHESIS_PREFIX_RE = re.compile(
    r"^\s*(?:(?:(?:не)?прямая|косвенная)\s+гипотеза"
    r"|гипотеза(?:\s*\(\s*(?:(?:не)?прямая|косвенная)\s*\))?"
    r"|косвенно)\s*:\s*",
    re.IGNORECASE,
)


def strip_hypothesis_prefix(text: str) -> str:
    """Срезает у гипотезы ведущий служебный префикс и переводит первую букву в
    верхний регистр; текст без префикса (или состоящий из одного префикса)
    возвращается без изменений."""
    stripped, count = _HYPOTHESIS_PREFIX_RE.subn("", text or "", count=1)
    stripped = stripped.lstrip()
    if not count or not stripped:
        return text or ""
    return stripped[0].upper() + stripped[1:]


def translate_markers(text: str, index: dict[str, str]) -> str:
    """Переводит номерные цитаты модели ([3] / [3, 7] / [5–10]) в канонические
    id фрагментов ([fragment-…]) по citation_index. Диапазон разворачивается
    в номера (5, 6, …, 10) с капом ширины _MARKER_RANGE_CAP. Номера, которых
    нет в индексе, отбрасываются; если в скобках не резолвится ни один номер —
    скобка остаётся как есть (это может быть год [2023] или диапазон лет)."""
    if not text or not index:
        return text or ""

    def expand(token: str) -> list[str]:
        bounds = _MARKER_RANGE_RE.match(token)
        if bounds is None:
            return [token]
        start, end = int(bounds.group(1)), int(bounds.group(2))
        if end < start or end - start > _MARKER_RANGE_CAP:
            return []
        return [str(num) for num in range(start, end + 1)]

    def repl(match: re.Match) -> str:
        resolved: list[str] = []
        for token in re.split(r"\s*,\s*", match.group(1)):
            for num in expand(token.strip()):
                fragment_id = index.get(num)
                if fragment_id and fragment_id not in resolved:
                    resolved.append(fragment_id)
        if not resolved:
            return match.group(0)
        return "[" + ", ".join(resolved) + "]"

    return _MARKER_RE.sub(repl, text)


def source_for_citation(store: ApplicationStore, cited_id: str) -> SourceRef | None:
    """SourceRef по цитате из текста LLM: fragment-… ищется в store.fragments
    напрямую, claim-… — через факт (его source указывает на фрагмент)."""
    fragment_id = cited_id
    fallback: SourceRef | None = None
    if cited_id.startswith("claim-"):
        fact = store.facts.get(cited_id)
        if fact is None:
            return None
        fragment_id = fact.source.fragment_id
        fallback = fact.source
    fragment = store.fragments.get(fragment_id)
    if fragment is None:
        # Фрагмент не в сторе (например, цитата через claim из старых данных) —
        # источник факта уже содержит нужную ссылку
        return fallback
    return SourceRef(
        document_id=fragment.document_id,
        version_id=fragment.version_id,
        fragment_id=fragment.id,
        page=fragment.page,
        section=fragment.section,
        quote=fragment.text[:200],
    )


def cited_sources(
    store: ApplicationStore, texts: list[str], sources: list[SourceRef]
) -> list[SourceRef]:
    """Строго процитированные фрагменты в порядке первого появления: «источников
    не больше, чем сносок в тексте». Каждая цитата резолвится (fragment напрямую;
    claim-… через store.facts; отсутствующий в сторе фрагмент пропускается, а не
    выдумывается). Ничего сверх процитированного. Пустой список = цитат нет
    (поведение при их отсутствии определяет вызывающий finalize)."""
    by_fragment = {source.fragment_id: source for source in sources}
    cited: list[SourceRef] = []
    seen: set[str] = set()
    for text in texts:
        for match in _CITATION_RE.finditer(text or ""):
            for cited_id in re.split(r"\s*,\s*", match.group(1)):
                source = by_fragment.get(cited_id) or source_for_citation(store, cited_id)
                if source is None or source.fragment_id in seen:
                    continue
                seen.add(source.fragment_id)
                cited.append(source)
    return cited
