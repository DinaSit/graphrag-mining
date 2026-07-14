"""Поиск ответа во внешних научных источниках (зона ML-A).

Используется как последняя ступень ответа: когда в базе знаний данных нет,
система сообщает об этом и ищет по списку источников, заданному
организаторами. Результат — только ответ и ссылка; в граф и базу фактов
внешние данные не записываются.

Источники: ddgs (по разрешённым доменам) + бесплатные научные API
(arXiv, Crossref, Semantic Scholar — см. scientific_sources). Опрашиваются
параллельно и независимо: сбой одной ветки не прерывает работу другой, выдача сливается
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

from app import embeddings, scientific_sources, translate, yandex_client

log = logging.getLogger(__name__)

# Выделенный пул под ddgs: зависший на капче поток не занимает общий пул
# asyncio.to_thread и не замедляет embeddings. wait_for
# отменяет ожидание, но не поток — поэтому пул изолирован и мал.
_DDGS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ddgs")

# Смена backend при повторе: ddgs>=9 — метапоиск по нескольким движкам,
# ratelimit/капча одного не означает недоступность других
_DDGS_BACKENDS = ["auto", "duckduckgo", "brave"]

# Паузы перед повторами ddgs (нарастающие): после исчерпания — пустой список
_RETRY_DELAYS = (0.5, 1.5)

# Общий лимит объединённой выдачи ddgs + научных API
MERGED_RESULTS_CAP = 12

# Домены ddgs-поиска и их отображаемые названия для GET /web_sources — один
# словарь: реестр отдаёт сервер, UI не дублирует список вручную, а домены
# фильтрации не расходятся с отображаемым перечнем (единственный источник правды)
_DDGS_SOURCES = {
    "researchgate.net": "ResearchGate",
    "elibrary.ru": "eLibrary.ru",
    "link.springer.com": "Springer Link",
    "patents.google.com": "Google Patents",
    "mdpi.com": "MDPI",
    "cyberleninka.ru": "КиберЛенинка",
    "onlinelibrary.wiley.com": "Wiley Online Library",
    "sciencedirect.com": "ScienceDirect",
}

ALLOWED_DOMAINS = list(_DDGS_SOURCES)

# Строки научных API в реестре: host попадает под маркер _API_HOSTS (включение
# ветки при поиске), поле api указывает UI, какую строку подсвечивать по метке
# source сниппетов (Crossref отдаёт ссылки doi.org — определить его по хосту нельзя)
_API_SOURCE_ROWS = (
    {"host": "arxiv.org", "url": "https://arxiv.org", "title": "arXiv", "api": "arxiv"},
    {"host": "crossref.org", "url": "https://search.crossref.org", "title": "Crossref", "api": "crossref"},
    {"host": "semanticscholar.org", "url": "https://semanticscholar.org",
     "title": "Semantic Scholar", "api": "semanticscholar"},
)

# Хосты научных API в реестре UI: строка с таким хостом включает опрос API,
# её отсутствие в переданном реестре — выключает. Выводится из строк реестра —
# добавление API правит одну структуру
_API_HOSTS = {row["host"]: row["api"] for row in _API_SOURCE_ROWS}


def default_sources() -> list[dict]:
    """Дефолтный реестр веб-источников для UI: ddgs-домены + научные API."""
    rows = [{"host": domain, "url": f"https://{domain}", "title": title}
            for domain, title in _DDGS_SOURCES.items()]
    return rows + [dict(row) for row in _API_SOURCE_ROWS]


def _split_sources(sources: list[dict] | None) -> tuple[list[str], set[str]] | None:
    """Реестр UI [{"host"}] → (домены ddgs, включённые API);
    None → None (поведение по умолчанию: ALLOWED_DOMAINS + все три API).

    Все домены равноправны — деления по языку нет: ddgs ищет по ним и русским
    вопросом, и английским переводом (см. search_results). Пустой реестр даёт
    пустые списки: пользователь отключил весь поиск.
    """
    if sources is None:
        return None
    domains: list[str] = []
    apis: set[str] = set()
    for s in sources:
        if not isinstance(s, dict):
            continue
        host = str(s.get("host") or "").strip().lower().removeprefix("www.")
        if not host:
            continue
        api = next((name for marker, name in _API_HOSTS.items() if marker in host), None)
        if api:
            apis.add(api)
            continue
        domains.append(host)
    return domains, apis

_ANSWER_PROMPT = """Ты — научный ассистент горно-металлургического R&D.
Ответь на вопрос, используя ТОЛЬКО выдержки из поисковой выдачи ниже.

Правила:
- составь по выдержкам краткую сводку (3–6 предложений) на русском; частичный ответ по тому, что есть, ЛУЧШЕ отказа;
- не выдумывай фактов сверх выдержек — но пересказывать и объединять их можно и нужно;
- выбери ОДИН наиболее релевантный источник из списка и укажи его url;
- found=false верни ТОЛЬКО если выдержки вообще не относятся к теме вопроса;
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


# Общенаучные слова: сами по себе релевантности не дают (иначе «технология»/
# «study» в вопросе давала бы совпадение с любой статьёй с этим словом).
# В фильтре не учитываются
_GENERIC_TERMS = {
    "технология", "технологии", "метод", "методы", "способ", "процесс", "система",
    "влияние", "применение", "получение", "исследование", "анализ", "устройство",
    "данные", "работа", "результат", "результаты", "изучение", "образование",
    "study", "method", "methods", "process", "system", "analysis", "research",
    "effect", "using", "based", "approach", "technology", "performance",
}


def _key_terms(text: str) -> set[str]:
    """Значимые токены текста: латиница/цифры целиком (бренды/аббревиатуры/
    англ-слова — THIOTEQ, copper, leaching), кириллица — 5-буквенным префиксом
    (снимает морфологию: «металлы»/«металлов» → «метал»). Стоп- и общенаучные
    слова выброшены."""
    terms: set[str] = set()
    for token in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е")):
        if token in _STOPWORDS or token in _GENERIC_TERMS:
            continue
        if re.fullmatch(r"[a-z0-9]+", token):
            if len(token) >= 3:
                terms.add(token)
        elif len(token) >= 4:
            terms.add(token[:5])
    return terms


def _filter_relevant(query_terms: set[str], results: list[dict]) -> list[dict]:
    """Оставляет результаты, чей заголовок/сниппет имеет хотя бы один общий
    значимый термин с вопросом (русским ИЛИ его переводом). Отсекает нерелевантную
    выдачу научных API (физика частиц из arXiv, гуманитарные статьи из Crossref).
    Пустой набор терминов — фильтр не применяется (критерий отбора отсутствует).

    Токенный фильтр — офлайн-резерв для _filter_relevant_semantic: слабый, по
    совпадению отдельных слов пропускает нерелевантные результаты (общее слово
    «распределение» даёт совпадение с гуманитарными статьями). Основной путь —
    семантический."""
    if not query_terms:
        return results
    return [
        result for result in results
        if _key_terms(f"{result.get('title', '')} "
                      f"{result.get('snippet') or result.get('body', '')}") & query_terms
    ]


# Порог косинусной близости bge-m3 (вопрос ↔ заголовок+сниппет): по измерениям
# на реальной выдаче релевантные металлургические результаты давали 0.56–0.70,
# нерелевантные (математические статьи из-за ошибки перевода, гуманитарные
# статьи, статистика ИМТ молодёжи) — 0.25–0.45. 0.45 разделяет кластеры
# с запасом. Настраивается через env, если потребуется калибровка.
_RELEVANCE_MIN = float(os.getenv("WEB_RELEVANCE_MIN", "0.45"))


def _cosine(a: list[float], b: list[float]) -> float:
    """Косинус нормированных векторов = скалярное произведение (bge-m3 отдаёт
    normalize_embeddings=True)."""
    return sum(x * y for x, y in zip(a, b))


async def _filter_relevant_semantic(
    question: str, results: list[dict], query_terms: set[str]
) -> list[dict]:
    """Семантический фильтр релевантности через bge-m3 (кросс-язычная модель,
    уже загружена в сервисе). Оставляет результаты, чей заголовок+сниппет
    ближе порога _RELEVANCE_MIN к вопросу по косинусу.

    Устойчив там, где токенный фильтр неэффективен: русский вопрос ↔ английские
    статьи сравниваются по смыслу, а общее слово («распределение» в вопросе и
    в «распределении молодёжи по ИМТ») не даёт ложного совпадения. Любой
    сбой эмбеддингов приводит к откату на токенный _filter_relevant — поиск не
    прерывается.
    """
    if not results:
        return results
    texts = [
        f"{r.get('title', '')} {r.get('snippet') or r.get('body', '')}".strip()
        for r in results
    ]
    try:
        # один батч: bge-m3 не различает запрос и документ (см. embeddings.embed),
        # а два раздельных вызова — это два прогона модели вместо одного
        vectors = await embeddings.embed([question] + texts)
        query_vec, doc_vecs = vectors[0], vectors[1:]
    except Exception as exc:
        log.warning("Семантический фильтр недоступен, откат на токенный: %s",
                    yandex_client.error_text(exc))
        return _filter_relevant(query_terms, results)
    kept = [
        result for result, doc_vec in zip(results, doc_vecs)
        if _cosine(query_vec, doc_vec) >= _RELEVANCE_MIN
    ]
    log.info("Семантический фильтр веб-выдачи: %d/%d прошли (порог %.2f)",
             len(kept), len(results), _RELEVANCE_MIN)
    return kept


def _ddgs_text(query: str, max_results: int, attempt: int) -> list[dict]:
    """Один вызов ddgs с backend по номеру попытки. Собственный User-Agent не требуется:
    ddgs>=9 (версия зафиксирована в requirements) маскируется под случайный браузер
    (primp impersonate="random")."""
    backend = _DDGS_BACKENDS[attempt % len(_DDGS_BACKENDS)]
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=backend))


def _query_hits(query: str, max_results: int, domains: list[str] | None = None) -> list[dict]:
    """ddgs с повторами: до 2 повторов с паузой и сменой backend
    (User-Agent ddgs рандомизирует самостоятельно — см. _ddgs_text).

    domains — список доменов-фильтров выдачи (None = ALLOWED_DOMAINS).
    Капча/ratelimit/timeout не пробрасываются наружу — после исчерпания
    попыток возвращается пустой список, чтобы не нарушать формирование
    объединённой выдачи.
    """
    allowed = ALLOWED_DOMAINS if domains is None else domains
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            results = _ddgs_text(query, max_results, attempt)
            return [r for r in results if any(d in r.get("href", "") for d in allowed)]
        except Exception as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                log.warning("ddgs: попытка %d не удалась (%s), ретрай через %.1f с: %s",
                            attempt + 1, query[:120], _RETRY_DELAYS[attempt], exc)
                time.sleep(_RETRY_DELAYS[attempt])
    log.warning("ddgs: попытки исчерпаны (%s): %s", query[:120], last_exc)
    return []


def _search_with_query(question: str, domains: list[str] | None = None) -> tuple[list[dict], str]:
    """Возвращает (hits, фактически сработавший запрос).

    domains — домены поиска/фильтрации (None = ALLOWED_DOMAINS)."""
    allowed = ALLOWED_DOMAINS if domains is None else domains
    site_filter = " OR ".join(f"site:{domain}" for domain in allowed)
    simplified = _simplify(question)
    # Если упрощение удалило все слова (короткий вопрос из стоп-слов), в запрос
    # с site-фильтром идёт исходный вопрос: чистый фильтр без ключевых слов
    # вернул бы случайные страницы разрешённых доменов. Та же логика, что у
    # запроса-заглушки query_used
    keywords = _fallback_query(question)
    # Сначала короткий предметный запрос: длинный полный вопрос с OR-фильтром
    # часто увеличивает длину URL и замедляет выдачу. Домен всё равно проверяется ниже.
    attempts = [
        simplified,
        f"{keywords} ({site_filter})",
        question,
    ]
    for query in attempts:
        if not query.strip():
            continue
        try:
            hits = _query_hits(query, max_results=10, domains=domains)
        except Exception as exc:
            log.warning("Веб-поиск: попытка не дала результата (%s): %s", query[:120], exc)
            continue
        if hits:
            return hits[:8], query
    return [], keywords


def _carry_optional(entry: dict, r: dict) -> dict:
    """Переносит в К3-запись опциональные поля, только если они заданы:
    year (год публикации научного источника — у ddgs его нет) и source
    (ветка поиска ddgs/arxiv/crossref/semanticscholar — по нему UI
    подсвечивает использованные площадки реестра)."""
    if r.get("year") is not None:
        entry["year"] = r["year"]
    if r.get("source"):
        entry["source"] = r["source"]
    return entry


def _results_payload(hits: list[dict]) -> list[dict]:
    """Выдача ddgs (title/body/href) → формат контракта К3
    (title/url/snippet[/year][/source])."""
    return [
        _carry_optional({"title": h.get("title") or h.get("href"), "url": h.get("href"),
                         "snippet": (h.get("body") or "")[:300]}, h)
        for h in hits
    ]


def _hits_from_results(results: list[dict]) -> list[dict]:
    """Обратное преобразование К3 → внутренний формат ddgs для LLM-суммаризации.

    year и source проносятся сквозь (для итоговых сниппетов и подсветки
    реестра в UI); присутствуют не у всех веток.
    """
    return [
        {"title": r.get("title") or r.get("url"), "body": r.get("snippet") or "",
         "href": r.get("url"), "year": r.get("year"), "source": r.get("source")}
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
            merged.append(_carry_optional(
                {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet") or ""}, r))
            if len(merged) >= MERGED_RESULTS_CAP:
                return merged
    return merged


def _fallback_query(question: str) -> str:
    """Запрос-заглушка для query_used, когда ddgs не отработал."""
    return _simplify(question) or question.strip()


def _sources_only(hits: list[dict], message: str) -> dict:
    """Ответ «источники без сводки»: LLM не дала связного текста (ошибка или
    отказ), но найденное ВСЕГДА доходит до пользователя."""
    return {"found": True, "answer": None, "url": hits[0].get("href"),
            "snippets": _results_payload(hits[:5]), "llm_error": message}


async def _ddgs_results(question: str, domains: list[str] | None = None) -> tuple[list[dict], str]:
    """Ветка ddgs в выделенном пуле с таймаутом; ошибки → пустая выдача."""
    try:
        timeout = float(os.getenv("WEB_SEARCH_TIMEOUT", "12"))
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_DDGS_POOL, _search_with_query, question, domains),
            timeout=timeout,
        )
    except Exception as exc:
        log.error("Веб-поиск (ddgs) недоступен: %s", yandex_client.error_text(exc))
        return [], _fallback_query(question)


async def _skip():
    """Заглушка выключенной ветки поиска: место в gather сохраняется."""
    return None


async def search_results(question: str, sources: list[dict] | None = None,
                         query_en: str | None = None) -> dict:
    """Чистый поиск без LLM (контракт К3): {"results": [...], "query_used": str}.

    sources — реестр источников из UI (см. _split_sources); None — значения по
    умолчанию: один ddgs-проход русским вопросом по ALLOWED_DOMAINS + все научные API.
    С реестром: два равноправных ddgs-прохода по всем включённым доменам —
    русским вопросом и английским переводом (площадки универсальные, язык
    контента не привязан к домену), научные API — по включённым.
    query_en — готовый перевод вопроса, если вызывающий уже перевёл (перевод
    argos дорогой, повторять его нельзя); None — переводим здесь.
    Ветки опрашиваются параллельно и независимо: ветка с ошибкой даёт пустой
    список, результаты работоспособных веток всё равно попадают в выдачу.
    query_used — фактический запрос русской ddgs-ветки.
    """
    # Научные API англоязычны — им передаётся переведённый запрос (офлайн-перевод,
    # сбой деградирует к оригиналу). Перевод выполняется в отдельном потоке:
    # argos синхронный, на первом вызове загружает модель
    if query_en is None:
        query_en = await asyncio.to_thread(translate.to_english, question)
    split = _split_sources(sources)
    if split is None:
        ddgs_domains, apis = None, None  # поведение по умолчанию: один ru-проход
        run_en = False
    else:
        ddgs_domains, apis = split
        # en-проход нужен, только если есть домены и перевод дал другой текст
        run_en = bool(ddgs_domains) and bool(query_en.strip()) and query_en != question
    run_ru = ddgs_domains is None or bool(ddgs_domains)
    ru_part, en_part, sci_part = await asyncio.gather(
        _ddgs_results(question, ddgs_domains) if run_ru else _skip(),
        _ddgs_results(query_en, ddgs_domains) if run_en else _skip(),
        scientific_sources.search_scientific(query_en, enabled=apis),
        return_exceptions=True,
    )
    hits, query_used = [], _fallback_query(question)
    # _ddgs_results перехватывает Exception внутри себя; isinstance-проверки — вторая
    # линия защиты от BaseException (например, CancelledError), который он не перехватывает
    if isinstance(ru_part, BaseException):
        log.error("Веб-поиск (ddgs) недоступен: %s", yandex_client.error_text(ru_part))
    elif ru_part is not None:
        hits, query_used = ru_part
    hits_en: list[dict] = []
    if isinstance(en_part, BaseException):
        log.error("Веб-поиск (ddgs, en) недоступен: %s", yandex_client.error_text(en_part))
    elif en_part is not None:
        hits_en = en_part[0]
    if isinstance(sci_part, BaseException):
        log.warning("Научные источники недоступны: %s", sci_part)
        sci_part = []
    # ddgs-выдача уже релевантна (поисковик искал по запросу+доменам) и в
    # фильтре не участвует; нерелевантная выдача научных API отсекается в answer_from_web.
    # Метка source="ddgs" — диагностика: по /web_search видно вклад каждой ветки
    ddgs_payload = [{**entry, "source": "ddgs"}
                    for entry in _results_payload(hits) + _results_payload(hits_en)]
    return {"results": _merge_results(ddgs_payload, sci_part), "query_used": query_used}


async def answer_from_web(question: str, results: list[dict] | None = None,
                          sources: list[dict] | None = None) -> dict:
    """Возвращает {"found", "answer", "url", "snippets", "llm_error"}.

    results — необязательная готовая выдача формата К3 (например, от /web_search):
    если передана и непуста, собственный поиск пропускается и сразу идёт
    LLM-суммаризация по ней; пустая/отсутствует — прежнее поведение.
    sources — реестр источников из UI, уходит в собственный поиск как есть.
    Поиск работает без LLM; связную формулировку даёт каскад LLM
    (Яндекс → запасной). Если обе модели недоступны — found=True с сырыми
    выдержками и llm_error вместо связного ответа: пользователь всё равно
    получает найденное.
    """
    if results:
        # Готовая выдача (например, от backend) уже курирована — доверяем как есть
        hits = _hits_from_results(results)
    else:
        # Собственный поиск: ddgs + научные API. Нерелевантная выдача научных API
        # (физика частиц из arXiv, гуманитарные статьи из Crossref) отсекается
        # семантически по bge-m3: результат, далёкий по смыслу от вопроса, до
        # пользователя и до LLM-сводки не доходит. Токенный набор терминов — на
        # случай отката к офлайн-резерву, если эмбеддинги недоступны.
        # Перевод — ОДИН раз на запрос (инференс argos дорогой), результат
        # используется и в поиске, и в терминах резервного токенного фильтра
        query_en = await asyncio.to_thread(translate.to_english, question)
        payload = await search_results(question, sources=sources, query_en=query_en)
        hits = _hits_from_results(payload["results"])
        query_terms = _key_terms(question) | _key_terms(query_en)
        hits = await _filter_relevant_semantic(question, hits, query_terms)
    if not hits:
        return {"found": False, "answer": None, "url": None, "snippets": []}

    snippets = "\n".join(f"- {h.get('title', '')}: {h.get('body', '')} ({h.get('href', '')})" for h in hits)
    # Веб-контур асинхронный (UI обращается к нему параллельно и показывает
    # заглушку загрузки), поэтому бюджет увеличен: каскад Яндекс→запасной
    # должен уложиться в него целиком.
    # Правило цепочки: внешний бюджет (WEB_ANSWER_TIMEOUT у прокси backend)
    # = поиск (WEB_SEARCH_TIMEOUT) + LLM (WEB_LLM_TIMEOUT) + запас.
    llm_timeout = float(os.getenv("WEB_LLM_TIMEOUT", "45"))
    try:
        # deadline ограничивает повторы и делит бюджет между каскадом Яндекс→запасной;
        # wait_for — внешняя страховка с запасом, чтобы не отменить ответ
        # резервного провайдера, пришедший на границе бюджета
        data = await asyncio.wait_for(
            yandex_client.chat_json([{
                "role": "user",
                "content": _ANSWER_PROMPT.format(question=question, snippets=snippets),
            }], allow_fallback=True, deadline=llm_timeout),
            timeout=llm_timeout + 2,
        )
    except (asyncio.TimeoutError, yandex_client.YandexClientError, json.JSONDecodeError, ValueError) as exc:
        # error_text: пустой str(exc) заменяется именем типа — причина не теряется
        message = (f"LLM-сводка веб-результатов не уложилась в бюджет {llm_timeout + 2:.0f} с"
                   if isinstance(exc, asyncio.TimeoutError) else yandex_client.error_text(exc))
        log.error("Генерация веб-ответа не удалась: %s", message)
        return _sources_only(hits, message[:300])

    if not data.get("found") or not data.get("answer"):
        # Модель отказалась от сводки (осторожный false у reasoning-моделей —
        # частый случай), однако выдача найдена: источники не отбрасываются,
        # пользователь получает их списком без связного текста — как при
        # недоступной LLM, только без ошибки
        return _sources_only(hits, "модель не составила связную сводку — ниже найденные источники")
    # Ссылка должна существовать в выдаче, а не быть сочинённой моделью
    urls = {h.get("href") for h in hits}
    url = data.get("url") if data.get("url") in urls else hits[0].get("href")
    return {"found": True, "answer": data["answer"], "url": url, "snippets": _results_payload(hits[:5])}
