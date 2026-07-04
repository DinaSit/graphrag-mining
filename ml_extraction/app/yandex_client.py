"""Клиент Yandex AI Studio (OpenAI-совместимый API).

Единая точка доступа к LLM и эмбеддингам. Модуль переиспользуется
для разбора вопросов и генерации ответов (зона ML-Б); соответствующие
промпты находятся вне этого модуля.

Каскад: основной провайдер — Яндекс (YANDEX_BASE_URL); для query-вызовов
(allow_fallback=True) при его отказе используется запасной OpenAI-совместимый
сервер (FALLBACK_BASE_URL, Ollama minimax). Извлечение фолбеком не пользуется.
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

# Лимит параллельности глобальный на процесс: сколько бы клиентов ни звало
# сервис одновременно, суммарная нагрузка на квоту провайдера не растёт.
_CHAT_SEMAPHORE = asyncio.Semaphore(int(config.LLM_CONCURRENCY))

# Запасной сервер отвечает медленнее и без квоты на ретраи: две попытки достаточно
_FALLBACK_RETRIES = 2


class YandexClientError(RuntimeError):
    """kind: auth — ключ отклонён; quota — лимит запросов/токенов;
    bad_response — модель вернула мусор; unavailable — сервис недоступен."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


def _headers() -> dict:
    if not config.YANDEX_API_KEY:
        raise YandexClientError("YANDEX_API_KEY не задан (создай .env, см. ml_extraction/README.md)", kind="auth")
    return {"Authorization": f"Bearer {config.YANDEX_API_KEY}"}


async def _post(path: str, body: dict, timeout: float, base_url: str | None = None,
                retries: int | None = None) -> dict:
    base = base_url or config.YANDEX_BASE_URL
    attempts = retries or config.LLM_RETRIES
    last_error: YandexClientError | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base}{path}", headers=_headers(), json=body)
            if resp.status_code in _RETRIABLE:
                kind = "quota" if resp.status_code == 429 else "unavailable"
                last_error = YandexClientError(f"HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code >= 400:
                # 4xx не ретраится: ключ отозван (401/403), модель не найдена (404), кривой запрос (400)
                kind = "auth" if resp.status_code in (401, 403) else "unavailable"
                raise YandexClientError(f"LLM отказал: HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = YandexClientError(str(e), kind="unavailable")
            await asyncio.sleep(2 ** attempt)
    kind = last_error.kind if last_error else "unavailable"
    raise YandexClientError(f"LLM недоступен после {attempts} попыток: {last_error}", kind=kind)


def _chat_body(messages: list[dict], temperature: float, model: str) -> dict:
    return {"model": model, "messages": messages, "temperature": temperature}


async def chat(messages: list[dict], temperature: float = 0.0, model: str | None = None,
               allow_fallback: bool = False) -> str:
    """messages — формат OpenAI; content может быть списком блоков (text + image_url).

    allow_fallback=True разрешает уход на запасной сервер при отказе основного
    (только query-вызовы; извлечение зовёт chat без фолбека).
    """
    async with _CHAT_SEMAPHORE:
        try:
            data = await _post(
                "/chat/completions",
                _chat_body(messages, temperature, config.model_uri(model or config.YANDEX_MODEL)),
                timeout=config.LLM_TIMEOUT,
            )
        except YandexClientError as primary_error:
            if not (allow_fallback and config.FALLBACK_BASE_URL):
                raise
            log.warning("Основной LLM отказал (%s): %s — пробуем запасной %s",
                        primary_error.kind, primary_error, config.FALLBACK_MODEL)
            try:
                data = await _post(
                    "/chat/completions",
                    _chat_body(messages, temperature, config.FALLBACK_MODEL),
                    timeout=config.LLM_TIMEOUT,
                    base_url=config.FALLBACK_BASE_URL,
                    retries=_FALLBACK_RETRIES,
                )
            except YandexClientError as fallback_error:
                # quota/auth информативнее обезличенного unavailable
                kind = fallback_error.kind if fallback_error.kind != "unavailable" else primary_error.kind
                raise YandexClientError(
                    f"основной LLM: {primary_error}; запасной LLM: {fallback_error}", kind=kind
                ) from fallback_error
    content = data["choices"][0]["message"]["content"]
    if not content:  # модель может вернуть пустой ответ (обрезка, фильтр)
        raise YandexClientError("Модель вернула пустой ответ", kind="bad_response")
    return content


def parse_json_answer(raw: str) -> dict:
    """Достаёт JSON из ответа модели: срезает ```-заборы и мусор вокруг фигурных скобок."""
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"В ответе модели нет JSON-объекта: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


async def chat_json(messages: list[dict], model: str | None = None, allow_fallback: bool = False) -> dict:
    raw = await chat(messages, temperature=0.0, model=model, allow_fallback=allow_fallback)
    try:
        return parse_json_answer(raw)
    except (ValueError, json.JSONDecodeError):
        # одна повторная попытка с указанием на ошибку формата
        log.warning("Невалидный JSON от модели, повторный запрос")
        retry = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": "Ответ не является валидным JSON. Повтори ответ строго одним JSON-объектом без пояснений и без markdown."},
        ]
        return parse_json_answer(await chat(retry, temperature=0.0, model=model, allow_fallback=allow_fallback))
