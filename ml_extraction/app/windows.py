"""Группировка фрагментов в окна: один вызов модели — один связный кусок текста.

Модель, получившая один абзац, не видит ни заголовка раздела, ни соседнего
предложения, поэтому материал и условия опыта, названные выше по тексту, ей
взять неоткуда — и она заполняет поля заглушкой «не указано». Окно возвращает
контекст: абзацы идут подряд до границы раздела или до предела WINDOW_CHARS,
строки таблицы собираются обратно в таблицу целиком вместе с шапкой.

Провенанс при этом не размывается: каждое утверждение модели привязывается
обратно к конкретному фрагменту окна по его цитате — см. attribute_quote.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import config
from app.schemas import SourceFragment

# Типы фрагментов, у которых page — настоящий номер страницы (слайда): окно
# не склеивает соседние страницы, иначе цитата потеряет адрес в просмотрщике
_PAGE_ADDRESSED = ("pdf_page_text", "pptx_slide_text")

_DIGIT_RE = re.compile(r"\d")

# Раздел, записанный парсером по умолчанию («DOCX evidence unit», «DOCX table 3»,
# «PPTX slide 4»): адрес фрагмента, а не заголовок из документа — контекста
# модели не добавляет, а место в промпте занимает
_PLACEHOLDER_SECTION_RE = re.compile(r"^(DOCX|PDF|PPTX)\s+(evidence unit|table|slide)\b", re.I)

# Таблица без единого числа и короче этого порога — блок подписей или шапка
# документа, свёрстанные таблицей («Руководитель работ | И.о. начальника ОИП»)
_LAYOUT_TABLE_CHARS = 200


@dataclass
class Window:
    """Фрагменты, уходящие в один промпт. kind: text | table | vision."""

    fragments: list[SourceFragment]
    kind: str

    @property
    def lead(self) -> SourceFragment:
        return self.fragments[0]

    @property
    def text(self) -> str:
        return "\n".join(f.text.strip() for f in self.fragments if f.text.strip())

    @property
    def section(self) -> str | None:
        section = (self.lead.section or "").strip()
        if not section or _PLACEHOLDER_SECTION_RE.match(section):
            return None
        return section

    @property
    def element_type(self) -> str:
        # Модели сообщается вид материала, а не тип отдельного фрагмента:
        # «docx_table_row» для собранной таблицы вводил бы её в заблуждение
        return "docx_table" if self.kind == "table" else self.lead.element_type


def build_windows(fragments: list[SourceFragment]) -> list[Window]:
    """Разбивает фрагменты на окна, не теряя и не дублируя ни одного.

    Окна возвращаются в порядке исходных фрагментов, чтобы очередь ревью шла
    по документу сверху вниз.
    """
    vision: list[Window] = []
    tables: dict[tuple[str, str], list[SourceFragment]] = {}
    stream: list[SourceFragment] = []

    for fragment in fragments:
        if fragment.metadata.get("image_b64"):
            # Скан: текста нет, окно бессмысленно — разбор идёт по изображению
            vision.append(Window([fragment], "vision"))
        elif fragment.element_type == "docx_table_row":
            key = (fragment.document_id, str(fragment.metadata.get("table") or ""))
            tables.setdefault(key, []).append(fragment)
        else:
            stream.append(fragment)

    windows = list(vision)
    for rows in tables.values():
        rows.sort(key=_row_ordinal)
        if _is_layout_table(rows):
            continue
        windows.append(Window(rows, "table"))
    windows.extend(_text_windows(stream))

    order = {fragment.id: i for i, fragment in enumerate(fragments)}
    windows.sort(key=lambda w: order.get(w.lead.id, 0))
    return windows


def _text_windows(fragments: list[SourceFragment]) -> list[Window]:
    windows: list[Window] = []
    current: list[SourceFragment] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            windows.append(Window(current, "text"))
        current, size = [], 0

    for fragment in fragments:
        text = (fragment.text or "").strip()
        if not text:
            continue
        if current and (_breaks(current[-1], fragment) or size + len(text) > config.WINDOW_CHARS):
            flush()
        current.append(fragment)
        size += len(text)
    flush()
    return windows


def _breaks(previous: SourceFragment, fragment: SourceFragment) -> bool:
    """Граница окна: смена документа, вида фрагмента, раздела или страницы."""
    if previous.document_id != fragment.document_id:
        return True
    if previous.element_type != fragment.element_type:
        return True
    if (previous.section or "") != (fragment.section or ""):
        return True
    return fragment.element_type in _PAGE_ADDRESSED and previous.page != fragment.page


def _row_ordinal(fragment: SourceFragment) -> int:
    try:
        return int(fragment.metadata.get("row"))
    except (TypeError, ValueError):
        return 0


def _is_layout_table(rows: list[SourceFragment]) -> bool:
    text = " ".join(fragment.text for fragment in rows)
    return not _DIGIT_RE.search(text) and len(text) < _LAYOUT_TABLE_CHARS


# Нормализация цитаты повторяет quote_in_source из backend (validation.py):
# привязка цитаты к фрагменту и последующая проверка этой цитаты обязаны
# считать одинаково, иначе кандидат уходил бы в ревью из-за расхождения правил
_QUOTE_NOISE = str.maketrans({char: " " for char in "«»\"'“”„‘’—–-­‑"})


def _normalize(text: str | None) -> str:
    lowered = str(text or "").lower().replace("ё", "е").translate(_QUOTE_NOISE)
    return " ".join(lowered.split())


def attribute_quote(window: Window, quote: str | None) -> SourceFragment:
    """Фрагмент окна, из которого взята цитата.

    Сначала ищется дословное вхождение. Если его нет — модель сшила цитату из
    двух абзацев или пересказала её — берётся фрагмент с наибольшим пересечением
    по словам. Промах привязки не проходит молча: цитата не подтвердится текстом
    выбранного фрагмента, и backend отправит кандидата в ревью с пометкой
    «цитата не подтверждена».
    """
    normalized = _normalize(quote)
    if not normalized or len(window.fragments) == 1:
        return window.lead
    for fragment in window.fragments:
        if normalized in _normalize(fragment.text):
            return fragment

    words = set(normalized.split())
    best, best_score = window.lead, 0
    for fragment in window.fragments:
        score = len(words & set(_normalize(fragment.text).split()))
        if score > best_score:
            best, best_score = fragment, score
    return best
