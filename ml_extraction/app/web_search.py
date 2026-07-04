"""Поиск ответа во внешних научных источниках (зона ML-A).

Используется как последняя ступень ответа: когда в базе знаний данных нет,
система честно сообщает об этом и ищет по списку источников, заданному
организаторами. Результат — только ответ и ссылка; в граф и базу фактов
внешние данные не записываются.
"""
import asyncio
import json
import logging

from duckduckgo_search import DDGS

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


def _search(question: str) -> list[dict]:
    site_filter = " OR ".join(f"site:{domain}" for domain in ALLOWED_DOMAINS)
    with DDGS() as ddgs:
        results = list(ddgs.text(f"{question} ({site_filter})", max_results=10))
    hits = [r for r in results if any(d in r.get("href", "") for d in ALLOWED_DOMAINS)]
    if not hits:
        # Поисковик мог проигнорировать site-фильтр — ищем шире и фильтруем сами
        with DDGS() as ddgs:
            results = list(ddgs.text(question, max_results=20))
        hits = [r for r in results if any(d in r.get("href", "") for d in ALLOWED_DOMAINS)]
    return hits[:8]


async def answer_from_web(question: str) -> dict:
    """Возвращает {"found": bool, "answer": str|None, "url": str|None}."""
    try:
        hits = await asyncio.to_thread(_search, question)
    except Exception as exc:
        log.error("Веб-поиск недоступен: %s", exc)
        return {"found": False, "answer": None, "url": None}
    if not hits:
        return {"found": False, "answer": None, "url": None}

    snippets = "\n".join(f"- {h.get('title', '')}: {h.get('body', '')} ({h.get('href', '')})" for h in hits)
    try:
        data = await yandex_client.chat_json([{
            "role": "user",
            "content": _ANSWER_PROMPT.format(question=question, snippets=snippets),
        }])
    except (yandex_client.YandexClientError, json.JSONDecodeError, ValueError) as exc:
        log.error("Генерация веб-ответа не удалась: %s", exc)
        return {"found": False, "answer": None, "url": None}

    if not data.get("found") or not data.get("answer"):
        return {"found": False, "answer": None, "url": None}
    # Ссылка должна существовать в выдаче, а не быть сочинённой моделью
    urls = {h.get("href") for h in hits}
    url = data.get("url") if data.get("url") in urls else hits[0].get("href")
    return {"found": True, "answer": data["answer"], "url": url}
