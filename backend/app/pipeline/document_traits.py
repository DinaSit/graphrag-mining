"""Признаки документа: тип, научность (LLM), происхождение и год (эвристики).

Происхождение и год — простые эвристики без LLM. Тип документа и научность —
LLM-классификация по титульным страницам (classify_document_llm): модель
анализирует титульные страницы и обосновывает вердикт цитатой. Недоступная LLM
или невалидный ответ → None (в UI прочерк), признаки дооцениваются фоновым
дозаполнением при следующем старте backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from app.schemas import SourceFragment

log = logging.getLogger(__name__)

# Первых фрагментов достаточно для определения происхождения (язык титула)
_HEAD_FRAGMENTS = 40

# 4-значный год по маске (19|20)\d\d; границы диапазона проверяются отдельно
_YEAR_RE = re.compile(r"\b(19|20)\d\d\b")

# Короткая дата в имени файла DD.MM.YY / DD-MM-YY → 20YY (напр. «09.06.22» → 2022)
_FILENAME_SHORTDATE_RE = re.compile(r"\b\d{1,2}[.\-]\d{1,2}[.\-](\d{2})\b")

# Маркеры выходных данных рядом с годом: их наличие поднимает приоритет
# кандидата — «2016 г.», «© 2019», «УДК ... 2018», DOI/ISSN-строки
_YEAR_CONTEXT_MARKERS = ("г.", "год", "удк", "©", "doi", "issn", "(c)")

_MIN_YEAR = 1900

_LATIN_RE = re.compile(r"[a-z]")


def detect_origin(fragments: list[SourceFragment]) -> str:
    """Происхождение документа по доле латинских букв в начале
    (> 0.5 => "foreign", иначе "ru")."""
    head = " ".join(fragment.text for fragment in fragments[:_HEAD_FRAGMENTS])
    letters = [char for char in head.lower() if char.isalpha()]
    latin = sum(1 for char in letters if _LATIN_RE.match(char))
    return "foreign" if letters and latin / len(letters) > 0.5 else "ru"


# ==================================================================
# Тип документа и научность — LLM по титульным страницам
# ==================================================================

# Закрытый список типов: ответ LLM вне списка отбрасывается как невалидный.
# Сопоставление ё-толерантное (проектная конвенция — приведение ё→е с обеих
# сторон): «Отчет» и «отчёт» одинаково резолвятся в канон
DOC_TYPES = ("статья", "доклад", "отчёт", "обзор", "патент", "презентация")
_DOC_TYPE_BY_FOLDED = {t.replace("ё", "е"): t for t in DOC_TYPES}

_CLASSIFY_PROMPT = """Ты — библиограф горно-металлургического R&D. Ниже — титульные страницы документа (начало и самый конец) и имя файла. Определи два поля и докажи цитатой.

1. "type" — что перед тобой, строго одно слово из списка: статья, доклад, отчёт, обзор, патент, презентация. На титульнике это обычно написано прямо.
2. "scientific" — научный ли это источник. Правило: научный, если документ опубликован в рецензируемом месте (журнал с томом/номером, сборник конференции, патентное ведомство) ИЛИ выполнен научной организацией (авторы с аффилиацией: институт, университет, НИИ) и имеет научный аппарат (методика, список литературы, УДК, DOI). Иначе — ненаучный. Наличие экспериментов САМО ПО СЕБЕ научности не даёт.
3. "reason" — одно предложение-обоснование с опорой на текст: что именно ты увидел (название журнала, организацию, УДК, отсутствие всего этого).

Ответ строго одним JSON-объектом: {{"type": "...", "scientific": true|false, "reason": "..."}}

Имя файла: {filename}

Начало документа:
{head}

Последняя страница:
{tail}
"""


def _title_pages_text(fragments: list[SourceFragment]) -> tuple[str, str]:
    """(начало, конец) для промпта: первые две страницы и самая последняя,
    с ограничением длины — модели передаются титульные страницы, а не весь
    документ."""
    if not fragments:
        return "", ""
    pages = sorted({fragment.page for fragment in fragments})
    head_pages = set(pages[:2])
    tail_page = pages[-1]
    head = " ".join(f.text for f in fragments if f.page in head_pages)
    tail = " ".join(f.text for f in fragments if f.page == tail_page)
    return head[:4000], tail[:1200]


# --- Авторы -----------------------------------------------------------------
# Записи ФИО, встречающиеся на титульных листах корпуса: «Косов Я.И.»,
# «Л.Б. Цымбулов», «Затицкий Борис Эдуардович», «D.M. Bogatyrev». Инициалы или
# полное отчество обязательны: без них фамилией стало бы любое слово с заглавной
# буквы, а титульный лист состоит из названий, грифов и должностей.
_SURNAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_INITIALS = r"[А-ЯЁ]\.\s?[А-ЯЁ]\."
# Порядок групп в каждом шаблоне задаётся явно, а не угадывается по точкам
_AUTHOR_PATTERNS = (
    (re.compile(rf"\b({_SURNAME})\s+({_INITIALS})"), "surname_initials"),
    (re.compile(rf"\b({_INITIALS})\s*({_SURNAME})\b"), "initials_surname"),
    (re.compile(r"\b([A-Z]\.\s?[A-Z]\.)\s*([A-Z][a-z]+)"), "initials_surname"),
    (re.compile(rf"\b({_SURNAME})\s+([А-ЯЁ])[а-яё]+\s+([А-ЯЁ])[а-яё]*"
                r"(?:ович|евич|овна|евна|ична|инична)\b"), "full_name"),
)
# Слова, формально проходящие как фамилия: должности и грифы стоят на тех же
# строках, что и инициалы («УТВЕРЖДАЮ Директор ... ___ С.М.»)
_NOT_SURNAME = frozenset({
    "утверждаю", "согласовано", "директор", "руководитель", "исполнитель", "начальник",
    "заместитель", "главный", "ведущий", "старший", "младший", "специалист", "инженер",
    "научный", "департамент", "департамента", "институт", "лаборатория", "отдел", "цех",
    "работ", "автор", "авторы", "реферат", "обзор", "отчет", "введение", "содержание",
    "оглавление", "заключение", "аннотация", "приложение", "приложения", "литература",
    "список", "таблица", "рисунок", "раздел", "глава",
})
_AUTHORS_LIMIT = 12
# Титульный лист: у DOCX «страница» — счётчик блоков, поэтому берутся первые
# фрагменты, а не первые страницы (иначе титул сводится к паре строк)
_AUTHOR_HEAD_FRAGMENTS = 25
_AUTHOR_HEAD_CHARS = 6000


def extract_authors(fragments: list[SourceFragment]) -> list[str]:
    """Авторы документа по титульному листу. Эвристика без LLM; вид записи
    приводится к единому «Фамилия И.О.», повторы одной фамилии схлопываются.

    Признак справочный, как и год: ошибка не критична, зато носитель
    компетенции перестаёт быть невидимым. Полнота ограничена титульным листом —
    авторов, названных только в списке литературы, здесь нет.
    """
    head = " ".join(f.text for f in fragments[:_AUTHOR_HEAD_FRAGMENTS])[:_AUTHOR_HEAD_CHARS]
    found: dict[str, str] = {}   # фамилия в нижнем регистре → показываемое имя
    for pattern, order in _AUTHOR_PATTERNS:
        for match in pattern.finditer(head):
            if order == "surname_initials":
                surname, initials = match.group(1), match.group(2)
            elif order == "initials_surname":
                surname, initials = match.group(2), match.group(1)
            else:  # full_name: фамилия + имя + отчество → инициалы из первых букв
                surname, initials = match.group(1), f"{match.group(2)}.{match.group(3)}."
            if surname.lower() in _NOT_SURNAME:
                continue
            found.setdefault(surname.lower(), f"{surname} {' '.join(initials.split())}")
            if len(found) >= _AUTHORS_LIMIT:
                return list(found.values())
    return list(found.values())


def classify_document_llm(fragments: list[SourceFragment], filename: str | None = None) -> dict | None:
    """Тип документа и научность через LLM (каскад ml-extraction /chat_json).

    Возвращает {"doc_type": str, "is_scientific": bool, "trait_reason": str}
    либо None при недоступной LLM или невалидном ответе — вызов никогда не
    бросает исключений, чтобы не прерывать инжест; признаки дооцениваются
    фоновым дозаполнением при следующем старте.
    """
    head, tail = _title_pages_text(fragments)
    if not head:
        return None
    # Импорт здесь, а не в начале модуля: llm_bridge подключает зависимости main в тестах.
    # chat_json — единственная точка контракта /chat_json (каскад, маппинг
    # ошибок kind); вызовы classify идут из потоков без event loop —
    # asyncio.run безопасен
    from app.pipeline.llm_bridge import chat_json

    prompt = _CLASSIFY_PROMPT.format(filename=filename or "неизвестно", head=head, tail=tail)
    try:
        data = asyncio.run(chat_json([{"role": "user", "content": prompt}]))
        if isinstance(data, str):  # модель могла вернуть JSON строкой
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("ответ LLM не JSON-объект")
        raw_type = str(data.get("type") or "").strip().lower().replace("ё", "е")
        doc_type = _DOC_TYPE_BY_FOLDED.get(raw_type)
        scientific = data.get("scientific")
        reason = str(data.get("reason") or "").strip()
        if doc_type is None or not isinstance(scientific, bool) or not reason:
            raise ValueError(f"невалидная классификация: type={raw_type!r}, scientific={scientific!r}")
        return {"doc_type": doc_type, "is_scientific": scientific, "trait_reason": reason[:400]}
    except Exception as exc:
        log.warning("Классификация документа недоступна (%s): %s", filename, exc)
        return None


def _year_from_filename(filename: str | None, current_year: int) -> int | None:
    """Год из имени файла. Сначала полный 4-значный год ((19|20)\\d\\d,
    напр. «ОИП-07-2022», «…-16-06-2024»), затем короткая дата
    DD.MM.YY / DD-MM-YY (напр. «09.06.22» → 2022)."""
    if not filename:
        return None
    years = [int(match.group()) for match in _YEAR_RE.finditer(filename)]
    years = [year for year in years if _MIN_YEAR <= year <= current_year]
    if years:
        return max(years)
    for match in _FILENAME_SHORTDATE_RE.finditer(filename):
        year = 2000 + int(match.group(1))
        if _MIN_YEAR <= year <= current_year:
            return year
    return None


def _year_from_document(fragments: list[SourceFragment], current_year: int) -> int | None:
    """Год из текста документа. Зона поиска: первые ДВЕ страницы (титул,
    выходные данные); последняя страница НЕ учитывается: как правило, там
    расположен список литературы, и его годы не соответствуют году издания
    документа. Кандидаты — 4-значные года
    (19|20)\\d\\d в [1900, текущий год]; приоритет годам в контексте выходных
    данных («г.», «год», «УДК»/«©»/«DOI»/«ISSN»); внутри группы самый частый,
    при равенстве — самый поздний."""
    if not fragments:
        return None
    pages = sorted({fragment.page for fragment in fragments})
    zone_pages = set(pages[:2])
    all_years: Counter[int] = Counter()
    context_years: Counter[int] = Counter()
    for fragment in fragments:
        if fragment.page not in zone_pages:
            continue
        lowered = fragment.text.lower()
        has_marker = any(marker in lowered for marker in _YEAR_CONTEXT_MARKERS)
        for match in _YEAR_RE.finditer(fragment.text):
            year = int(match.group())
            if year < _MIN_YEAR or year > current_year:
                continue
            all_years[year] += 1
            if has_marker:
                context_years[year] += 1
    pool = context_years or all_years
    if not pool:
        return None
    # Самый частый; при равенстве — самый поздний год
    return max(pool, key=lambda year: (pool[year], year))


def extract_publication_year(
    fragments: list[SourceFragment], filename: str | None = None
) -> int | None:
    """Год издания документа; год из имени файла имеет приоритет.

    Год ищется в ДВУХ местах: имя файла и текст документа (первые две
    страницы). Сверка:
    - нашёлся только в названии → из названия;
    - нашёлся только в документе → из документа;
    - нашёлся в обоих и значения различаются → берётся год из названия;
    - не нашёлся нигде → None (в UI — прочерк).
    Эвристика без LLM; ошибка не критична, признак справочный.
    """
    current_year = datetime.now(timezone.utc).year
    name_year = _year_from_filename(filename, current_year)
    if name_year is not None:
        return name_year
    return _year_from_document(fragments, current_year)
