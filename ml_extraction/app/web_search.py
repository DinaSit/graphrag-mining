"""Поиск ответа во внешних научных источниках (зона ML-A).

Используется как последняя ступень ответа: когда в базе знаний данных нет,
система честно сообщает об этом и ищет по списку источников, заданному
организаторами. Результат — только ответ и ссылка; в граф и базу фактов
внешние данные не записываются.
"""
import asyncio
import json
import logging
import os
import re

from ddgs import DDGS

from app import yandex_client

log = logging.getLogger(__name__)

ALLOWED_DOMAINS = [
    "researchgate.net",
    "elibrary.ru",
    "link.springer.com",
    "patents.google.com",
    "mdpi.com",
    "cyberleninka.ru",
    "onlinelibrary.wiley.com",
    "sciencedirect.com",
    "sci-hub.ru",
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


def _query_hits(query: str, max_results: int) -> list[dict]:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    return [r for r in results if any(d in r.get("href", "") for d in ALLOWED_DOMAINS)]


def _search(question: str) -> list[dict]:
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
            return hits[:8]
    return []


def _snippet_payload(hits: list[dict]) -> list[dict]:
    return [
        {"title": h.get("title") or h.get("href"), "url": h.get("href"), "snippet": (h.get("body") or "")[:300]}
        for h in hits[:5]
    ]


async def answer_from_web(question: str) -> dict:
    """Возвращает {"found", "answer", "url", "snippets", "llm_error"}.

    Поиск работает без LLM; связную формулировку даёт каскад LLM
    (Яндекс → запасной). Если обе модели недоступны — found=True с сырыми
    выдержками и llm_error вместо связного ответа: пользователь всё равно
    получает найденное.
    """
    try:
        timeout = float(os.getenv("WEB_SEARCH_TIMEOUT", "12"))
        hits = await asyncio.wait_for(asyncio.to_thread(_search, question), timeout=timeout)
    except Exception as exc:
        log.error("Веб-поиск недоступен: %s", exc)
        return {"found": False, "answer": None, "url": None, "snippets": []}
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
                "snippets": _snippet_payload(hits), "llm_error": message[:300]}

    if not data.get("found") or not data.get("answer"):
        return {"found": False, "answer": None, "url": None, "snippets": []}
    # Ссылка должна существовать в выдаче, а не быть сочинённой моделью
    urls = {h.get("href") for h in hits}
    url = data.get("url") if data.get("url") in urls else hits[0].get("href")
    return {"found": True, "answer": data["answer"], "url": url, "snippets": _snippet_payload(hits)}
