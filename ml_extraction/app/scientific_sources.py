"""Бесплатные научные API без ключей: arXiv, Crossref, Semantic Scholar (зона ML-A).

Дополняют ddgs-поиск в web_search: каждый источник опрашивается асинхронно
и изолированно — сбой или таймаут одного не прерывает работу остальных
(пустой список и warning в лог). Результаты приводятся к формату К3 {title, url, snippet}
с полем source для отладки.

Язык запросов: API англоязычны — web_search передаёт сюда английский перевод
вопроса (офлайн-argos, см. translate.py). Нерелевантную часть выдачи отсекает
семантический фильтр bge-m3 в answer_from_web.
"""
import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

# Единый бюджет на источник: медленный API исключается по таймауту, не задерживая ответ
SOURCE_TIMEOUT = 6.0
RESULTS_PER_SOURCE = 5

# Вежливый пул Crossref: с mailto в User-Agent запросы попадают в polite pool
# с более стабильными лимитами (https://api.crossref.org/swagger-ui/index.html)
_CROSSREF_UA = "graphrag-rnd/1.0 (mailto:research@example.org)"

_ARXIV_URL = "http://export.arxiv.org/api/query"
_CROSSREF_URL = "https://api.crossref.org/works"
_S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _make_client() -> httpx.AsyncClient:
    """Фабрика клиента: в тестах подменяется на клиент с httpx.MockTransport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(SOURCE_TIMEOUT), follow_redirects=True)


def _clean(text: str | None) -> str:
    """Снимает HTML/JATS-теги (abstract Crossref) и схлопывает пробелы."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _year(value) -> int | None:
    """Год издания из разнородных значений API → int | None.

    Принимает int, ISO-дату/строку с годом ("2024-01-01T00:00:00Z", "2024") или
    None. Допустимый диапазон (1500..2100) отсекает некорректные значения; всё
    прочее — None, отсутствие года не должно прерывать парсинг источника.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool — подкласс int, но годом быть не может
        return None
    if isinstance(value, int):
        return value if 1500 <= value <= 2100 else None
    match = re.search(r"\d{4}", str(value))
    if not match:
        return None
    year = int(match.group())
    return year if 1500 <= year <= 2100 else None


async def _search_arxiv(client: httpx.AsyncClient, query: str) -> list[dict]:
    """arXiv Atom API: entry → {title, url (ссылка id), snippet (summary)}."""
    try:
        resp = await client.get(_ARXIV_URL, params={
            "search_query": f"all:{query}", "start": 0, "max_results": RESULTS_PER_SOURCE,
        })
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        out = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            url = _clean(entry.findtext("atom:id", "", _ATOM_NS))
            if not url:
                continue
            title = _clean(entry.findtext("atom:title", "", _ATOM_NS))
            summary = _clean(entry.findtext("atom:summary", "", _ATOM_NS))
            year = _year(entry.findtext("atom:published", "", _ATOM_NS))
            out.append({"title": title or url, "url": url, "snippet": summary[:300],
                        "year": year, "source": "arxiv"})
        return out
    except Exception as exc:
        log.warning("arXiv недоступен: %s", exc)
        return []


def _crossref_year(item: dict) -> int | None:
    """Год из Crossref-структуры date-parts: published → issued (первый год)."""
    for key in ("published", "issued", "published-print", "published-online"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = _year(parts[0][0])
            if year is not None:
                return year
    return None


async def _search_crossref(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Crossref REST API: items → {title[0], URL, abstract/container-title}."""
    try:
        resp = await client.get(
            _CROSSREF_URL,
            params={
                "query": query, "rows": RESULTS_PER_SOURCE,
                "select": "title,URL,abstract,container-title,published,issued",
            },
            headers={"User-Agent": _CROSSREF_UA},
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        out = []
        for item in items:
            url = item.get("URL")
            if not url:
                continue
            titles = item.get("title") or []
            container = item.get("container-title") or []
            snippet = _clean(item.get("abstract")) or _clean(container[0] if container else "")
            out.append({
                "title": _clean(titles[0] if titles else "") or url,
                "url": url,
                "snippet": snippet[:300],
                "year": _crossref_year(item),
                "source": "crossref",
            })
        return out
    except Exception as exc:
        log.warning("Crossref недоступен: %s", exc)
        return []


async def _search_semanticscholar(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Semantic Scholar Graph API без ключа (низкий лимит): 429 → [] без повтора."""
    try:
        resp = await client.get(_S2_URL, params={
            "query": query, "limit": RESULTS_PER_SOURCE, "fields": "title,abstract,url,year",
        })
        if resp.status_code == 429:
            log.warning("Semantic Scholar: лимит запросов (429), источник пропущен")
            return []
        resp.raise_for_status()
        out = []
        for item in resp.json().get("data", []) or []:
            url = item.get("url")
            if not url:
                continue
            out.append({
                "title": _clean(item.get("title")) or url,
                "url": url,
                "snippet": _clean(item.get("abstract"))[:300],
                "year": _year(item.get("year")),
                "source": "semanticscholar",
            })
        return out
    except Exception as exc:
        log.warning("Semantic Scholar недоступен: %s", exc)
        return []


async def search_scientific(query: str, enabled: set[str] | None = None) -> list[dict]:
    """Параллельный опрос научных API → [{title, url, snippet, source}].

    enabled — имена включённых API ({"arxiv", "crossref", "semanticscholar"}),
    None = все три (по умолчанию); пустой набор — опрос пропускается целиком.
    Каждый источник перехватывает собственные ошибки (лог warning, []);
    return_exceptions=True — вторая линия защиты от неожиданных сбоев.
    """
    query = (query or "").strip()
    if not query:
        return []
    searchers = {
        "arxiv": _search_arxiv,
        "crossref": _search_crossref,
        "semanticscholar": _search_semanticscholar,
    }
    active = [fn for name, fn in searchers.items() if enabled is None or name in enabled]
    if not active:
        return []
    async with _make_client() as client:
        parts = await asyncio.gather(
            *(fn(client, query) for fn in active),
            return_exceptions=True,
        )
    results: list[dict] = []
    for part in parts:
        if isinstance(part, BaseException):
            log.warning("Научный источник упал вне собственного обработчика: %s", part)
            continue
        results.extend(part)
    return results
