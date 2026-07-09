"""Поиск ответа во внешних научных источниках (зона ML-A).

Используется как последняя ступень ответа: когда в базе знаний данных нет,
система честно сообщает об этом и ищет по списку источников, заданному
организаторами. Результат — только ответ и ссылка; в граф и базу фактов
внешние данные не записываются.

Источники: ddgs (по разрешённым доменам) + бесплатные научные API
(arXiv, Crossref, Semantic Scholar — см. scientific_sources). Опрашиваются
параллельно и независимо: сбой одной ветки не роняет другую, выдача сливается
с дедупликацией по url/заголовку.
"""
import asyncio
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ddgs import DDGS

from app import scientific_sources, yandex_client

log = logging.getLogger(__name__)

# Выделенный пул под ddgs: зависший на капче поток не занимает общий пул
# asyncio.to_thread и не тормозит embeddings (находка аудита). wait_for
# отменяет ожидание, но не поток — потому пул изолирован и мал.
_DDGS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ddgs")

# Ротация реалистичных десктопных User-Agent по номеру попытки: однообразный
# заголовок — один из триггеров капчи. Это снижение частоты капчи, не обход.
# ddgs>=9 сам маскируется под случайный браузер (primp impersonate="random")
# и headers не принимает — тогда список не используется (см. _ddgs_text).
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Смена backend при ретрае: ddgs>=9 — метапоиск по нескольким движкам,
# ratelimit/капча одного не означает недоступность других
_DDGS_BACKENDS = ["auto", "duckduckgo", "brave"]

# Паузы перед ретраями ddgs (нарастающие): после исчерпания — пустой список
_RETRY_DELAYS = (0.5, 1.5)

# Общий кап слитой выдачи ddgs + научные API
MERGED_RESULTS_CAP = 12

ALLOWED_DOMAINS = [
    "researchgate.net",
    "elibrary.ru",
    "link.springer.com",
    "patents.google.com",
    "mdpi.com",
    "cyberleninka.ru",
    "onlinelibrary.wiley.com",
    "sciencedirect.com",
]

_ANSWER_PROMPT = """Ты — научный ассистент горно-металлургического R&D.
Ответь на вопрос, используя ТОЛЬКО выдержки из поисковой выдачи ниже.

Правила:
- если выдержки позволяют дать содержательный ответ — сформулируй его кратко (3–6 предложений) на русском;
- выбери ОДИН наиболее релевантный источник из списка и укажи его url;
- не выдумывай: если выдержек недостаточно для ответа — верни found=false;
- ответ строго одним JSON-объектом: {{"found": true|false, "answer": "...", "url": "..."}}

Вопрос: {question}

Выдержки:
{snippets}
"""


# Вопросительные и служебные слова выбрасываются из поискового запроса:
# полный текст вопроса слишком специфичен и часто даёт пустую выдачу
_STOPWORDS = {
    "какие", "какой", "какова", "каковы", "как", "что", "чем", "почему", "где",
    "при", "для", "или", "если", "того", "этом", "также", "более", "менее",
    "применяются", "используются", "существуют", "бывают", "можно", "нужно",
    "мировой", "практике", "сегодня", "сейчас",
}


def _simplify(question: str) -> str:
    words = re.findall(r"[\wА-Яа-яЁё-]+", question.lower().replace("ё", "е"))
    keep = [w for w in words if len(w) > 3 and w not in _STOPWORDS]
    return " ".join(keep[:8])


def _ddgs_text(query: str, max_results: int, attempt: int) -> list[dict]:
    """Один вызов ddgs с UA/backend по номеру попытки.

    headers поддерживают только старые версии (duckduckgo_search-стиль):
    ddgs>=9 их не принимает (TypeError) и сам подставляет случайный
    браузерный отпечаток — тогда работаем без своего User-Agent.
    """
    headers = {"User-Agent": _USER_AGENTS[attempt % len(_USER_AGENTS)]}
    backend = _DDGS_BACKENDS[attempt % len(_DDGS_BACKENDS)]
    try:
        ddgs = DDGS(headers=headers)
    except TypeError:
        ddgs = DDGS()
    with ddgs:
        try:
            return list(ddgs.text(query, max_results=max_results, backend=backend))
        except TypeError:  # версия без параметра backend
            return list(ddgs.text(query, max_results=max_results))


def _query_hits(query: str, max_results: int) -> list[dict]:
    """ddgs с ретраями: до 2 повторов с паузой и сменой UA/backend.

    Капча/ratelimit/timeout не пробрасываются наружу — после исчерпания
    попыток возвращается пустой список, чтобы не ронять слитую выдачу.
    """
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            results = _ddgs_text(query, max_results, attempt)
            return [r for r in results if any(d in r.get("href", "") for d in ALLOWED_DOMAINS)]
        except Exception as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                log.warning("ddgs: попытка %d не удалась (%s), ретрай через %.1f с: %s",
                            attempt + 1, query[:120], _RETRY_DELAYS[attempt], exc)
                time.sleep(_RETRY_DELAYS[attempt])
    log.warning("ddgs: попытки исчерпаны (%s): %s", query[:120], last_exc)
    return []


def _search_with_query(question: str) -> tuple[list[dict], str]:
    """Возвращает (hits, фактически сработавший запрос)."""
    site_filter = " OR ".join(f"site:{domain}" for domain in ALLOWED_DOMAINS)
    simplified = _simplify(question)
    # Если упрощение съело все слова (короткий вопрос из стоп-слов), в запрос
    # с site-фильтром идёт исходный вопрос: чистый фильтр без ключевых слов
    # вернул бы случайные страницы разрешённых доменов
    keywords = simplified or question.strip()
    # Сначала короткий предметный запрос: длинный полный вопрос с OR-фильтром
    # часто раздувает URL и замедляет выдачу. Домен всё равно проверяется ниже.
    attempts = [
        simplified,
        f"{keywords} ({site_filter})",
        question,
    ]
    for query in attempts:
        if not query.strip():
            continue
        try:
            hits = _query_hits(query, max_results=10)
        except Exception as exc:
            log.warning("Веб-поиск: попытка не дала результата (%s): %s", query[:120], exc)
            continue
        if hits:
            return hits[:8], query
    return [], keywords


def _results_payload(hits: list[dict]) -> list[dict]:
    """Выдача ddgs (title/body/href) → формат контракта К3 (title/url/snippet[/year]).

    year (год публикации научного источника) переносится, только если задан:
    у чистой ddgs-выдачи его нет — поле опускается.
    """
    payload = []
    for h in hits:
        entry = {"title": h.get("title") or h.get("href"), "url": h.get("href"),
                 "snippet": (h.get("body") or "")[:300]}
        if h.get("year") is not None:
            entry["year"] = h["year"]
        payload.append(entry)
    return payload


def _hits_from_results(results: list[dict]) -> list[dict]:
    """Обратное преобразование К3 → внутренний формат ddgs для LLM-суммаризации.

    year проносится сквозь (для итоговых сниппетов), присутствует лишь у научных.
    """
    return [
        {"title": r.get("title") or r.get("url"), "body": r.get("snippet") or "",
         "href": r.get("url"), "year": r.get("year")}
        for r in results
    ]


def _normalize_url(url: str) -> str:
    """Ключ дедупликации: без схемы, www и хвостового слэша, в нижнем регистре."""
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def _merge_results(*groups: list[dict]) -> list[dict]:
    """Сливает группы выдачи в единый список К3 {title, url, snippet}.

    Дедуп по нормализованному url и по заголовку (одна статья часто доступна
    по разным url: DOI, arXiv, издатель). Порядок групп задаёт приоритет,
    общий кап — MERGED_RESULTS_CAP.
    """
    merged: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for group in groups:
        for r in group:
            url_key = _normalize_url(r.get("url") or "")
            title_key = re.sub(r"\s+", " ", (r.get("title") or "").strip().lower())
            if url_key and url_key in seen_urls:
                continue
            if title_key and title_key in seen_titles:
                continue
            if url_key:
                seen_urls.add(url_key)
            if title_key:
                seen_titles.add(title_key)
            entry = {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet") or ""}
            # year — только у научных источников; для ddgs (года нет) поле опускается,
            # чтобы не раздувать контракт К3 и не ломать сравнение выдачи
            if r.get("year") is not None:
                entry["year"] = r["year"]
            merged.append(entry)
            if len(merged) >= MERGED_RESULTS_CAP:
                return merged
    return merged


async def _ddgs_results(question: str) -> tuple[list[dict], str]:
    """Ветка ddgs в выделенном пуле с таймаутом; ошибки → пустая выдача."""
    try:
        timeout = float(os.getenv("WEB_SEARCH_TIMEOUT", "12"))
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_DDGS_POOL, _search_with_query, question), timeout=timeout,
        )
    except Exception as exc:
        log.error("Веб-поиск (ddgs) недоступен: %s", exc)
        return [], _simplify(question) or question.strip()


async def search_results(question: str) -> dict:
    """Чистый поиск без LLM (контракт К3): {"results": [...], "query_used": str}.

    ddgs и научные API опрашиваются параллельно и независимо: упавшая ветка
    даёт пустой список, живая всё равно попадает в выдачу. query_used —
    прежний смысл, фактический запрос ddgs-ветки.
    """
    ddgs_part, sci_part = await asyncio.gather(
        _ddgs_results(question),
        scientific_sources.search_scientific(question),
        return_exceptions=True,
    )
    if isinstance(ddgs_part, BaseException):
        log.error("Веб-поиск (ddgs) недоступен: %s", ddgs_part)
        hits, query_used = [], _simplify(question) or question.strip()
    else:
        hits, query_used = ddgs_part
    if isinstance(sci_part, BaseException):
        log.warning("Научные источники недоступны: %s", sci_part)
        sci_part = []
    return {"results": _merge_results(_results_payload(hits), sci_part), "query_used": query_used}


async def answer_from_web(question: str, results: list[dict] | None = None) -> dict:
    """Возвращает {"found", "answer", "url", "snippets", "llm_error"}.

    results — необязательная готовая выдача формата К3 (например, от /web_search):
    если передана и непуста, собственный поиск пропускается и сразу идёт
    LLM-суммаризация по ней; пустая/отсутствует — прежнее поведение.
    Поиск работает без LLM; связную формулировку даёт каскад LLM
    (Яндекс → запасной). Если обе модели недоступны — found=True с сырыми
    выдержками и llm_error вместо связного ответа: пользователь всё равно
    получает найденное.
    """
    if results:
        hits = _hits_from_results(results)
    else:
        # Тот же слитый поиск, что у /web_search: ddgs + научные API,
        # ошибки веток гасятся внутри search_results
        payload = await search_results(question)
        hits = _hits_from_results(payload["results"])
    if not hits:
        return {"found": False, "answer": None, "url": None, "snippets": []}

    snippets = "\n".join(f"- {h.get('title', '')}: {h.get('body', '')} ({h.get('href', '')})" for h in hits)
    try:
        llm_timeout = float(os.getenv("WEB_LLM_TIMEOUT", "8"))
        # deadline режет ретраи и делит бюджет между каскадом Яндекс→запасной;
        # wait_for — внешняя страховка с запасом, чтобы не отменить ответ
        # фолбэка, пришедший на границе бюджета
        data = await asyncio.wait_for(
            yandex_client.chat_json([{
                "role": "user",
                "content": _ANSWER_PROMPT.format(question=question, snippets=snippets),
            }], allow_fallback=True, deadline=llm_timeout),
            timeout=llm_timeout + 2,
        )
    except (asyncio.TimeoutError, yandex_client.YandexClientError, json.JSONDecodeError, ValueError) as exc:
        message = "LLM-сводка веб-результатов не уложилась в таймаут" if isinstance(exc, asyncio.TimeoutError) else str(exc)
        log.error("Генерация веб-ответа не удалась: %s", message)
        return {"found": True, "answer": None, "url": hits[0].get("href"),
                "snippets": _results_payload(hits[:5]), "llm_error": message[:300]}

    if not data.get("found") or not data.get("answer"):
        return {"found": False, "answer": None, "url": None, "snippets": []}
    # Ссылка должна существовать в выдаче, а не быть сочинённой моделью
    urls = {h.get("href") for h in hits}
    url = data.get("url") if data.get("url") in urls else hits[0].get("href")
    return {"found": True, "answer": data["answer"], "url": url, "snippets": _results_payload(hits[:5])}
