"""Клиент Yandex AI Studio (OpenAI-совместимый API).

Единая точка доступа к LLM (эмбеддинги — локальные, см. app/embeddings.py).
Модуль переиспользуется для разбора вопросов и генерации ответов (зона ML-Б);
соответствующие промпты находятся вне этого модуля.

Каскад: основной провайдер — Яндекс (YANDEX_BASE_URL); для query-вызовов
(allow_fallback=True) при его отказе используется запасной OpenAI-совместимый
сервер (FALLBACK_BASE_URL, Ollama minimax). Извлечение фолбеком не пользуется.
"""
import asyncio
import json
import logging
import re
import time

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

# Доля дедлайна, которую разрешено съесть основному провайдеру, когда настроен
# фолбэк: иначе одна зависшая попытка Яндекса выбирает весь бюджет и запасной
# сервер не получает шанса ответить.
_PRIMARY_SHARE = 0.6

# Минимальный остаток бюджета, при котором ещё имеет смысл начинать попытку
_MIN_ATTEMPT_BUDGET = 1.0

# Последняя фактически использованная query-модель. Health не делает
# генеративный пробный запрос, чтобы не расходовать квоту при каждом рендере UI.
_LAST_QUERY_MODEL = {
    "status": "configured" if config.YANDEX_API_KEY or config.FALLBACK_BASE_URL else "unavailable",
    "provider": "yandex" if config.YANDEX_API_KEY else ("fallback" if config.FALLBACK_BASE_URL else "none"),
    "model": config.model_uri(config.YANDEX_MODEL) if config.YANDEX_API_KEY else (config.FALLBACK_MODEL if config.FALLBACK_BASE_URL else None),
    "error_kind": None,
    "error": None,
    "updated_at": None,
}


class YandexClientError(RuntimeError):
    """kind: auth — ключ отклонён; quota — лимит запросов/токенов;
    bad_response — модель вернула мусор; unavailable — сервис недоступен."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


def _set_query_model_status(
    status: str,
    provider: str,
    model: str | None,
    error: YandexClientError | None = None,
) -> None:
    _LAST_QUERY_MODEL.update(
        {
            "status": status,
            "provider": provider,
            "model": model,
            "error_kind": error.kind if error else None,
            "error": str(error)[:300] if error else None,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


def query_model_status() -> dict:
    return dict(_LAST_QUERY_MODEL)


def _primary_headers() -> dict:
    if not config.YANDEX_API_KEY:
        raise YandexClientError("YANDEX_API_KEY не задан (создай .env, см. ml_extraction/README.md)", kind="auth")
    return {"Authorization": f"Bearer {config.YANDEX_API_KEY}"}


def _fallback_headers() -> dict:
    # Ключ Яндекса на чужой сервер не отправляется; пустой FALLBACK_API_KEY —
    # запрос без Authorization (локальный Ollama авторизации не требует)
    if config.FALLBACK_API_KEY:
        return {"Authorization": f"Bearer {config.FALLBACK_API_KEY}"}
    return {}


async def _post(path: str, body: dict, timeout: float, base_url: str | None = None,
                retries: int | None = None, headers: dict | None = None,
                deadline: float | None = None) -> dict:
    """deadline — суммарный бюджет вызова в секундах (попытки + бэкофф-сны).

    Ретраи прекращаются, когда остатка бюджета не хватает на следующую попытку;
    после последней попытки бэкофф-сна нет — ошибка отдаётся сразу.
    """
    base = base_url or config.YANDEX_BASE_URL
    attempts = retries or config.LLM_RETRIES
    request_headers = _primary_headers() if headers is None else headers
    started = time.monotonic()
    last_error: YandexClientError | None = None
    for attempt in range(attempts):
        remaining = None if deadline is None else deadline - (time.monotonic() - started)
        if remaining is not None and remaining < _MIN_ATTEMPT_BUDGET:
            break
        request_timeout = timeout if remaining is None else min(timeout, remaining)
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                resp = await client.post(f"{base}{path}", headers=request_headers, json=body)
            if resp.status_code in _RETRIABLE:
                kind = "quota" if resp.status_code == 429 else "unavailable"
                last_error = YandexClientError(f"HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
            elif resp.status_code >= 400:
                # 4xx не ретраится: ключ отозван (401/403), модель не найдена (404), кривой запрос (400)
                kind = "auth" if resp.status_code in (401, 403) else "unavailable"
                raise YandexClientError(f"LLM отказал: HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
            else:
                return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = YandexClientError(str(e), kind="unavailable")
        if attempt + 1 >= attempts:
            break
        delay = min(2 ** attempt, 30)
        if deadline is not None:
            remaining = deadline - (time.monotonic() - started)
            # Сон, после которого не остаётся времени на попытку, бессмыслен
            if remaining - delay < _MIN_ATTEMPT_BUDGET:
                break
        await asyncio.sleep(delay)
    kind = last_error.kind if last_error else "unavailable"
    raise YandexClientError(f"LLM недоступен: {last_error or 'бюджет времени исчерпан до первой попытки'}", kind=kind)


def _chat_body(messages: list[dict], temperature: float, model: str) -> dict:
    return {"model": model, "messages": messages, "temperature": temperature}


async def chat(messages: list[dict], temperature: float = 0.0, model: str | None = None,
               allow_fallback: bool = False, deadline: float | None = None) -> str:
    """messages — формат OpenAI; content может быть списком блоков (text + image_url).

    allow_fallback=True разрешает уход на запасной сервер при отказе основного
    (только query-вызовы; извлечение зовёт chat без фолбека).
    deadline — суммарный бюджет вызова в секундах на обе ступени каскада;
    None — без ограничения (инжест: там ретраи оправданы).
    """
    started = time.monotonic()
    cascade = allow_fallback and bool(config.FALLBACK_BASE_URL)
    async with _CHAT_SEMAPHORE:
        # Ожидание семафора тоже тратит бюджет вызывающей стороны, поэтому доля
        # считается от фактического остатка. При настроенном фолбэке основному
        # провайдеру достаётся лишь часть, чтобы запасной сервер гарантированно
        # получил своё окно
        if deadline is None:
            primary_deadline = None
        else:
            remaining_budget = deadline - (time.monotonic() - started)
            primary_deadline = remaining_budget * _PRIMARY_SHARE if cascade else remaining_budget
        try:
            primary_model = config.model_uri(model or config.YANDEX_MODEL)
            data = await _post(
                "/chat/completions",
                _chat_body(messages, temperature, primary_model),
                timeout=config.LLM_TIMEOUT,
                deadline=primary_deadline,
            )
            _set_query_model_status("available", "yandex", primary_model)
        except YandexClientError as primary_error:
            if not cascade:
                _set_query_model_status("unavailable", "none", None, primary_error)
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
                    headers=_fallback_headers(),
                    # фолбэку передаётся весь остаток общего бюджета
                    deadline=None if deadline is None else deadline - (time.monotonic() - started),
                )
                _set_query_model_status("available", "fallback", config.FALLBACK_MODEL)
            except YandexClientError as fallback_error:
                # quota/auth информативнее обезличенного unavailable
                kind = fallback_error.kind if fallback_error.kind != "unavailable" else primary_error.kind
                combined_error = YandexClientError(
                    f"основной LLM: {primary_error}; запасной LLM: {fallback_error}", kind=kind
                )
                _set_query_model_status("unavailable", "none", None, combined_error)
                raise combined_error from fallback_error
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


async def chat_json(messages: list[dict], model: str | None = None, allow_fallback: bool = False,
                    deadline: float | None = None) -> dict:
    started = time.monotonic()
    raw = await chat(messages, temperature=0.0, model=model, allow_fallback=allow_fallback, deadline=deadline)
    try:
        return parse_json_answer(raw)
    except (ValueError, json.JSONDecodeError):
        # одна повторная попытка с указанием на ошибку формата
        log.warning("Невалидный JSON от модели, повторный запрос")
        retry = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": "Ответ не является валидным JSON. Повтори ответ строго одним JSON-объектом без пояснений и без markdown."},
        ]
        remaining = None if deadline is None else deadline - (time.monotonic() - started)
        return parse_json_answer(await chat(retry, temperature=0.0, model=model,
                                            allow_fallback=allow_fallback, deadline=remaining))
