"""Эвристические признаки документа: научность и происхождение.

Признаки вычисляются один раз по фрагментам документа (в конце инжеста
и бэкфилом при старте) и хранятся в documents.is_scientific / documents.origin.
Это дешёвая эвристика без LLM: ошибка не критична, признаки носят
справочный характер (доля научных источников в ответе).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from app.schemas import SourceFragment

# Первых фрагментов достаточно: маркеры научности (УДК, DOI, аннотация,
# список авторов) стоят в начале статьи — полный текст сканировать не нужно
_HEAD_FRAGMENTS = 40

# Год издания ищем в начале и в конце документа: выходные данные и год стоят
# либо на титуле/шапке (первые фрагменты), либо в колофоне/списке литературы (хвост)
_YEAR_EDGE_FRAGMENTS = 20
_YEAR_TAIL_FRAGMENTS = 10

# 4-значный год по маске (19|20)\d\d; границы диапазона проверяются отдельно
_YEAR_RE = re.compile(r"\b(19|20)\d\d\b")

# Короткая дата в имени файла DD.MM.YY / DD-MM-YY → 20YY (напр. «09.06.22» → 2022)
_FILENAME_SHORTDATE_RE = re.compile(r"\b\d{1,2}[.\-]\d{1,2}[.\-](\d{2})\b")

# Маркеры выходных данных рядом с годом: их наличие поднимает приоритет
# кандидата — «2016 г.», «© 2019», «УДК ... 2018», DOI/ISSN-строки
_YEAR_CONTEXT_MARKERS = ("г.", "год", "удк", "©", "doi", "issn", "(c)")

_MIN_YEAR = 1900

# Маркеры научной публикации; ищутся регистронезависимо как подстроки
_SCIENTIFIC_MARKERS = (
    "удк",
    "doi",
    "issn",
    "список литературы",
    "библиограф",
    "аннотация",
    "journal",
    "vol.",
    "№ журнала",
)

# Паттерн списка авторов русскоязычной статьи: «Иванов И. И.»
_AUTHORS_RE = re.compile(r"\b[А-ЯЁ][а-яё]+\s[А-ЯЁ]\.\s?[А-ЯЁ]\.")

_LATIN_RE = re.compile(r"[a-z]")


def classify_document(fragments: list[SourceFragment]) -> tuple[bool, str]:
    """(is_scientific, origin) по началу документа.

    Научность — любой маркер публикации или паттерн авторов в первых
    ~40 фрагментах; происхождение — по доле латинских букв среди букв
    (> 0.5 => "foreign", иначе "ru").
    """
    head = " ".join(fragment.text for fragment in fragments[:_HEAD_FRAGMENTS])
    lowered = head.lower()
    is_scientific = any(marker in lowered for marker in _SCIENTIFIC_MARKERS) or bool(_AUTHORS_RE.search(head))

    letters = [char for char in lowered if char.isalpha()]
    latin = sum(1 for char in letters if _LATIN_RE.match(char))
    origin = "foreign" if letters and latin / len(letters) > 0.5 else "ru"
    return is_scientific, origin


def _fragment_order_key(fragment: SourceFragment) -> tuple[int, int]:
    """Числовой порядок фрагмента: (страница, номер блока). Номер блока берётся
    из метаданных, иначе из хвоста id (…-p42 → 42) — иначе лексическая сортировка
    по id ставит p10 раньше p2, и титул с годом не попадает в начало документа."""
    meta = fragment.metadata or {}
    for key in ("ordinal", "paragraph", "block", "row", "slide"):
        value = meta.get(key)
        if isinstance(value, int):
            return (fragment.page, value)
    match = re.search(r"(\d+)\D*$", fragment.id)
    return (fragment.page, int(match.group(1)) if match else 0)


def _year_from_filename(filename: str | None, current_year: int) -> int | None:
    """Год из имени файла — фолбэк, когда в тексте год не найден. Сначала полный
    4-значный год ((19|20)\\d\\d, напр. «ОИП-07-2022», «…-16-06-2024»), затем
    короткая дата DD.MM.YY / DD-MM-YY (напр. «09.06.22» → 2022)."""
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


def extract_publication_year(
    fragments: list[SourceFragment], filename: str | None = None
) -> int | None:
    """Год издания документа (best-effort эвристика, без LLM).

    Сначала — из ТЕКСТА: фрагменты упорядочиваются численно (титул/колофон/список
    литературы реально в начале и конце), сканируются первые ~20 и последние ~10.
    Кандидаты — 4-значные года (19|20)\\d\\d в [1900, текущий год]; приоритет годам
    в контексте выходных данных («г.», «год», «УДК»/«©»/«DOI»/«ISSN»); внутри группы
    самый частый, при равенстве — самый поздний.

    Если в тексте года нет — ФОЛБЭК на имя файла (год из названия). None — если и
    там не найдено. Эвристика справочная; точное определение — задача LLM/метаданных.
    """
    current_year = datetime.now(timezone.utc).year
    if fragments:
        ordered = sorted(fragments, key=_fragment_order_key)
        edge = ordered[:_YEAR_EDGE_FRAGMENTS] + ordered[-_YEAR_TAIL_FRAGMENTS:]
        all_years: Counter[int] = Counter()
        context_years: Counter[int] = Counter()
        for fragment in edge:
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
        if pool:
            # Самый частый; при равенстве — самый поздний год
            return max(pool, key=lambda year: (pool[year], year))
    # Год в тексте не найден — пробуем имя файла
    return _year_from_filename(filename, current_year)
