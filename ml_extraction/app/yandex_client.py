"""Клиент Yandex AI Studio (OpenAI-совместимый API).

Единая точка доступа к LLM и эмбеддингам. Модуль переиспользуется
для разбора вопросов и генерации ответов (зона ML-Б); соответствующие
промпты находятся вне этого модуля.
"""
import asyncio
import json
import logging
import re

import httpx

from app import config

log = logging.getLogger(__name__)

_RETRIABLE = {429, 500, 502, 503, 504}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class YandexClientError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.YANDEX_API_KEY:
        raise YandexClientError("YANDEX_API_KEY не задан (создай .env, см. ml_extraction/README.md)")
    return {"Authorization": f"Bearer {config.YANDEX_API_KEY}"}


async def _post(path: str, body: dict, timeout: float) -> dict:
    last_error: Exception | None = None
    for attempt in range(config.LLM_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{config.YANDEX_BASE_URL}{path}", headers=_headers(), json=body)
            if resp.status_code in _RETRIABLE:
                last_error = YandexClientError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            await asyncio.sleep(2 ** attempt)
    raise YandexClientError(f"Yandex API недоступен после {config.LLM_RETRIES} попыток: {last_error}")


async def chat(messages: list[dict], temperature: float = 0.0, model: str | None = None) -> str:
    """messages — формат OpenAI; content может быть списком блоков (text + image_url)."""
    data = await _post(
        "/chat/completions",
        {
            "model": config.model_uri(model or config.YANDEX_MODEL),
            "messages": messages,
            "temperature": temperature,
        },
        timeout=config.LLM_TIMEOUT,
    )
    return data["choices"][0]["message"]["content"]


def parse_json_answer(raw: str) -> dict:
    """Достаёт JSON из ответа модели: срезает ```-заборы и мусор вокруг фигурных скобок."""
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"В ответе модели нет JSON-объекта: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


async def chat_json(messages: list[dict], model: str | None = None) -> dict:
    raw = await chat(messages, temperature=0.0, model=model)
    try:
        return parse_json_answer(raw)
    except (ValueError, json.JSONDecodeError):
        # одна повторная попытка с указанием на ошибку формата
        log.warning("Невалидный JSON от модели, повторный запрос")
        retry = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": "Ответ не является валидным JSON. Повтори ответ строго одним JSON-объектом без пояснений и без markdown."},
        ]
        return parse_json_answer(await chat(retry, temperature=0.0, model=model))


async def embed(texts: list[str], kind: str = "doc") -> list[list[float]]:
    """Эмбеддинги текстов. kind='doc' — индексация фрагментов, kind='query' — поисковые запросы.

    Яндекс использует раздельные модели для документов и запросов; размерность — 256.
    """
    model = config.YANDEX_EMB_DOC_MODEL if kind == "doc" else config.YANDEX_EMB_QUERY_MODEL
    uri = config.model_uri(model, scheme="emb")
    sem = asyncio.Semaphore(4)

    # API принимает одну строку за запрос; тексты отправляются параллельно
    async def one(text: str) -> list[float]:
        async with sem:
            data = await _post("/embeddings", {"model": uri, "input": text}, timeout=60)
            return data["data"][0]["embedding"]

    return list(await asyncio.gather(*(one(t) for t in texts)))
