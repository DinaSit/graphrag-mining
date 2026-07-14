"""Клиент Yandex AI Studio (OpenAI-совместимый API).

Единая точка доступа к LLM (эмбеддинги — локальные, см. app/embeddings.py).
Модуль переиспользуется для разбора вопросов и генерации ответов (зона ML-Б);
соответствующие промпты находятся вне этого модуля.

Каскад: основной провайдер — Яндекс (YANDEX_BASE_URL); для query-вызовов
(allow_fallback=True) при его отказе используется запасной OpenAI-совместимый
сервер (FALLBACK_BASE_URL, Ollama minimax). Извлечение запасной сервер не использует.
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

# Лимит параллельности глобальный на процесс: сколько бы клиентов ни обращалось
# к сервису одновременно, суммарная нагрузка на квоту провайдера не растёт.
_CHAT_SEMAPHORE = asyncio.Semaphore(int(config.LLM_CONCURRENCY))

# Запасной сервер отвечает медленнее и без квоты на повторы: двух попыток достаточно
_FALLBACK_RETRIES = 2

# Доля дедлайна, которую разрешено израсходовать основному провайдеру, когда
# настроен резервный провайдер: иначе одна зависшая попытка Яндекса выбирает
# весь бюджет и запасной сервер не получает возможности ответить.
_PRIMARY_SHARE = 0.6

# Минимальный остаток бюджета, при котором ещё имеет смысл начинать попытку
_MIN_ATTEMPT_BUDGET = 1.0

def _initial_query_model_status() -> dict:
    """Статус до первого query-вызова: что из каскада настроено конфигурацией.

    Приоритет — как в самом каскаде: Яндекс, затем запасной сервер, иначе
    ничего не настроено.
    """
    if config.YANDEX_API_KEY:
        provider, model = "yandex", config.model_uri(config.YANDEX_MODEL)
    elif config.FALLBACK_BASE_URL:
        provider, model = "fallback", config.FALLBACK_MODEL
    else:
        provider, model = "none", None
    return {
        "status": "configured" if provider != "none" else "unavailable",
        "provider": provider,
        "model": model,
        "error_kind": None,
        "error": None,
        "updated_at": None,
    }


# Последняя фактически использованная query-модель. Health не делает
# генеративный пробный запрос, чтобы не расходовать квоту при каждом рендере UI.
_LAST_QUERY_MODEL = _initial_query_model_status()


class YandexClientError(RuntimeError):
    """kind: auth — ключ отклонён; quota — лимит запросов/токенов;
    bad_response — модель вернула некорректный ответ; unavailable — сервис недоступен."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


def error_text(exc: BaseException) -> str:
    """Причина для сообщений об ошибке: str(exc) бывает пуст (httpx-таймауты,
    TimeoutError/CancelledError) — тогда причиной становится имя типа
    исключения. Пустая причина после двоеточия недопустима нигде."""
    return str(exc).strip() or type(exc).__name__


def _transport_error(exc: Exception, request_timeout: float) -> YandexClientError:
    """httpx-сбой попытки → YandexClientError с непустой причиной; у таймаута
    в текст добавляется фактический бюджет попытки."""
    reason = error_text(exc)
    if isinstance(exc, httpx.TimeoutException):
        reason = f"не уложился в бюджет попытки {request_timeout:.1f} с ({reason})"
    return YandexClientError(reason, kind="unavailable")


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


# Тайминги повторов общие для _post и _post_stream: единые функции исключают
# расхождение копий. Цикл попыток во вспомогательную функцию не выносится —
# у стрима свои правила после первого токена, а break/continue должны
# оставаться непосредственно в вызывающем коде.

def _attempt_timeout(timeout: float, deadline: float | None, started: float) -> float | None:
    """Таймаут очередной попытки в рамках общего бюджета deadline;
    None — остатка не хватает даже на минимальную попытку."""
    if deadline is None:
        return timeout
    remaining = deadline - (time.monotonic() - started)
    if remaining < _MIN_ATTEMPT_BUDGET:
        return None
    return min(timeout, remaining)


def _backoff_delay(attempt: int, attempts: int, deadline: float | None, started: float) -> float | None:
    """Пауза перед следующей попыткой; None — повторы следует прекратить:
    попытки исчерпаны, либо пауза израсходует бюджет — пауза, после которой
    не останется времени на попытку, не имеет смысла."""
    if attempt + 1 >= attempts:
        return None
    delay = min(2 ** attempt, 30)
    if deadline is not None:
        remaining = deadline - (time.monotonic() - started)
        if remaining - delay < _MIN_ATTEMPT_BUDGET:
            return None
    return delay


def _retries_exhausted(last_error: YandexClientError | None) -> YandexClientError:
    """Итоговая ошибка после исчерпания попыток или бюджета."""
    kind = last_error.kind if last_error else "unavailable"
    return YandexClientError(
        f"LLM недоступен: {last_error or 'бюджет времени исчерпан до первой попытки'}", kind=kind
    )


async def _post(path: str, body: dict, timeout: float, base_url: str | None = None,
                retries: int | None = None, headers: dict | None = None,
                deadline: float | None = None) -> dict:
    """deadline — суммарный бюджет вызова в секундах (попытки + нарастающие паузы).

    Повторы прекращаются, когда остатка бюджета не хватает на следующую попытку;
    после последней попытки паузы нет — ошибка отдаётся сразу.
    """
    base = base_url or config.YANDEX_BASE_URL
    attempts = retries or config.LLM_RETRIES
    request_headers = _primary_headers() if headers is None else headers
    started = time.monotonic()
    last_error: YandexClientError | None = None
    for attempt in range(attempts):
        request_timeout = _attempt_timeout(timeout, deadline, started)
        if request_timeout is None:
            break
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                resp = await client.post(f"{base}{path}", headers=request_headers, json=body)
            if resp.status_code in _RETRIABLE:
                kind = "quota" if resp.status_code == 429 else "unavailable"
                last_error = YandexClientError(f"HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
            elif resp.status_code >= 400:
                # 4xx не повторяется: ключ отозван (401/403), модель не найдена (404), некорректный запрос (400)
                kind = "auth" if resp.status_code in (401, 403) else "unavailable"
                raise YandexClientError(f"LLM отказал: HTTP {resp.status_code}: {resp.text[:300]}", kind=kind)
            else:
                return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = _transport_error(e, request_timeout)
        delay = _backoff_delay(attempt, attempts, deadline, started)
        if delay is None:
            break
        await asyncio.sleep(delay)
    raise _retries_exhausted(last_error)


async def _post_stream(path: str, body: dict, timeout: float, base_url: str | None = None,
                       retries: int | None = None, headers: dict | None = None,
                       deadline: float | None = None):
    """Стриминговый аналог _post: асинхронный генератор кусков delta.content.

    Чанки читаются лениво (httpx stream), весь ответ в память не загружается.
    Повторы и бюджет — те же, что в _post, но действуют только ДО первого
    токена: повторная попытка после начала генерации продублировала бы текст,
    поэтому сбой в середине стрима сразу отдаётся исключением наружу.
    """
    base = base_url or config.YANDEX_BASE_URL
    attempts = retries or config.LLM_RETRIES
    request_headers = _primary_headers() if headers is None else headers
    started = time.monotonic()
    last_error: YandexClientError | None = None
    for attempt in range(attempts):
        request_timeout = _attempt_timeout(timeout, deadline, started)
        if request_timeout is None:
            break
        emitted = False
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                async with client.stream("POST", f"{base}{path}", headers=request_headers, json=body) as resp:
                    if resp.status_code in _RETRIABLE:
                        text = (await resp.aread()).decode("utf-8", errors="replace")
                        kind = "quota" if resp.status_code == 429 else "unavailable"
                        last_error = YandexClientError(f"HTTP {resp.status_code}: {text[:300]}", kind=kind)
                    elif resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", errors="replace")
                        kind = "auth" if resp.status_code in (401, 403) else "unavailable"
                        raise YandexClientError(f"LLM отказал: HTTP {resp.status_code}: {text[:300]}", kind=kind)
                    else:
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[len("data:"):].strip()
                            if payload == "[DONE]":
                                return
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                # keep-alive или нераспознанная строка не должна прерывать стрим
                                continue
                            choices = chunk.get("choices") or []
                            delta = ((choices[0].get("delta") or {}).get("content")) if choices else None
                            if delta:
                                emitted = True
                                yield delta
                        return
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if emitted:
                raise YandexClientError(
                    f"обрыв стрима после первого токена: {error_text(e)}", kind="unavailable"
                ) from e
            last_error = _transport_error(e, request_timeout)
        delay = _backoff_delay(attempt, attempts, deadline, started)
        if delay is None:
            break
        await asyncio.sleep(delay)
    raise _retries_exhausted(last_error)


def _extract_content(data: dict) -> str:
    """Разбор и валидация нестримового ответа: choices[0].message.content.

    HTTP 200 с пустым/битым телом — такой же отказ провайдера, как 5xx:
    поднимается YandexClientError(kind='bad_response'), чтобы каскад в chat()
    успел попробовать запасной сервер, а /health не показал «available».
    """
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
    except (IndexError, KeyError, TypeError) as e:
        raise YandexClientError(
            f"Модель вернула неожиданную структуру ответа (нет choices/message/content): {str(data)[:300]}",
            kind="bad_response",
        ) from e
    if not content or not str(content).strip():
        # Пустой content — отказ, даже если reasoning_content непустой:
        # модель завершила рассуждения, но итоговый ответ не выдала
        reason = "Модель вернула пустой ответ"
        if choice.get("finish_reason") == "length":
            reason += " (finish_reason=length: лимит токенов исчерпан"
            if (message.get("reasoning_content") or "").strip():
                reason += ", всё ушло на размышления (reasoning_content)"
            reason += ")"
        raise YandexClientError(reason, kind="bad_response")
    return content


def _chat_body(messages: list[dict], temperature: float, model: str) -> dict:
    return {"model": model, "messages": messages, "temperature": temperature}


def _stream_body(messages: list[dict], temperature: float, model: str) -> dict:
    body = _chat_body(messages, temperature, model)
    body["stream"] = True
    return body


def _remaining_budget(deadline: float | None, started: float) -> float | None:
    """Остаток общего бюджета deadline к текущему моменту; None — бюджет не задан."""
    if deadline is None:
        return None
    return deadline - (time.monotonic() - started)


def _primary_deadline(deadline: float | None, started: float, cascade: bool) -> float | None:
    """Бюджет первой ступени каскада. Считается от фактического остатка:
    ожидание семафора тоже тратит бюджет вызывающей стороны. При настроенном
    резервном провайдере основному достаётся лишь доля _PRIMARY_SHARE, чтобы
    запасной сервер гарантированно получил своё окно времени."""
    remaining = _remaining_budget(deadline, started)
    if remaining is None:
        return None
    return remaining * _PRIMARY_SHARE if cascade else remaining


async def chat(messages: list[dict], temperature: float = 0.0, model: str | None = None,
               allow_fallback: bool = False, deadline: float | None = None) -> str:
    """messages — формат OpenAI; content может быть списком блоков (text + image_url).

    allow_fallback=True разрешает переход на запасной сервер при отказе основного
    (только query-вызовы; извлечение вызывает chat без резервного провайдера).
    deadline — суммарный бюджет вызова в секундах на обе ступени каскада;
    None — без ограничения (инжест: там ретраи оправданы).
    """
    started = time.monotonic()
    cascade = allow_fallback and bool(config.FALLBACK_BASE_URL)
    async with _CHAT_SEMAPHORE:
        primary_deadline = _primary_deadline(deadline, started, cascade)
        try:
            primary_model = config.model_uri(model or config.YANDEX_MODEL)
            data = await _post(
                "/chat/completions",
                _chat_body(messages, temperature, primary_model),
                timeout=config.LLM_TIMEOUT,
                deadline=primary_deadline,
            )
            # Пустой/битый 200 — тоже отказ: статус ставится только после валидации
            content = _extract_content(data)
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
                    # запасному серверу передаётся весь остаток общего бюджета
                    deadline=_remaining_budget(deadline, started),
                )
                content = _extract_content(data)
                _set_query_model_status("available", "fallback", config.FALLBACK_MODEL)
            except YandexClientError as fallback_error:
                # quota/auth информативнее обезличенного unavailable
                kind = fallback_error.kind if fallback_error.kind != "unavailable" else primary_error.kind
                combined_error = YandexClientError(
                    f"основной LLM: {primary_error}; запасной LLM: {fallback_error}", kind=kind
                )
                _set_query_model_status("unavailable", "none", None, combined_error)
                raise combined_error from fallback_error
    return content


async def chat_stream(messages: list[dict], temperature: float = 0.0, model: str | None = None,
                      deadline: float | None = None):
    """Стриминговый аналог chat() для эндпоинта /chat_stream (контракт К2).

    Асинхронный генератор: события {"delta": <кусок текста>}, затем РОВНО ОДНА
    терминальная {"done": True, "text", "provider", "error"} — всегда, даже при
    отказе обоих провайдеров. Каскад на запасной сервер возможен только пока
    основной не отдал ни одного токена (иначе текст продублировался бы); сбой после
    первого токена завершает стрим терминальной записью с error и частичным text.
    Семафор LLM_CONCURRENCY удерживается на всё время стрима; деление бюджета
    deadline между ступенями — как в chat(). Ключ Яндекса на FALLBACK_BASE_URL
    не отправляется (см. _fallback_headers).
    """
    started = time.monotonic()
    cascade = bool(config.FALLBACK_BASE_URL)
    parts: list[str] = []
    provider = "none"
    error: YandexClientError | None = None
    try:
        async with _CHAT_SEMAPHORE:
            primary_deadline = _primary_deadline(deadline, started, cascade)
            primary_model = config.model_uri(model or config.YANDEX_MODEL)
            try:
                async for delta in _post_stream(
                    "/chat/completions",
                    _stream_body(messages, temperature, primary_model),
                    timeout=config.LLM_TIMEOUT,
                    deadline=primary_deadline,
                ):
                    provider = "yandex"
                    parts.append(delta)
                    yield {"delta": delta}
                if not parts:  # HTTP 200, но ни одного токена — как пустой ответ в chat()
                    raise YandexClientError("Модель вернула пустой ответ", kind="bad_response")
                _set_query_model_status("available", "yandex", primary_model)
            except YandexClientError as primary_error:
                if parts or not cascade:
                    error = primary_error
                    _set_query_model_status("unavailable", "none", None, primary_error)
                else:
                    log.warning("Основной LLM отказал до первого токена (%s): %s — стрим через запасной %s",
                                primary_error.kind, primary_error, config.FALLBACK_MODEL)
                    try:
                        async for delta in _post_stream(
                            "/chat/completions",
                            _stream_body(messages, temperature, config.FALLBACK_MODEL),
                            timeout=config.LLM_TIMEOUT,
                            base_url=config.FALLBACK_BASE_URL,
                            retries=_FALLBACK_RETRIES,
                            headers=_fallback_headers(),
                            # запасному серверу передаётся весь остаток общего бюджета
                            deadline=_remaining_budget(deadline, started),
                        ):
                            provider = "fallback"
                            parts.append(delta)
                            yield {"delta": delta}
                        if not parts:
                            raise YandexClientError("Модель вернула пустой ответ", kind="bad_response")
                        _set_query_model_status("available", "fallback", config.FALLBACK_MODEL)
                    except YandexClientError as fallback_error:
                        if parts:
                            # запасной сервер начал отвечать и оборвался: частичный текст уже отправлен
                            error = fallback_error
                        else:
                            # quota/auth информативнее обезличенного unavailable
                            kind = fallback_error.kind if fallback_error.kind != "unavailable" else primary_error.kind
                            error = YandexClientError(
                                f"основной LLM: {primary_error}; запасной LLM: {fallback_error}", kind=kind
                            )
                        _set_query_model_status("unavailable", "none", None, error)
    except Exception as unexpected:  # страховка контракта: терминальная запись обязана уйти
        log.exception("Непредвиденный сбой стрима LLM")
        error = YandexClientError(error_text(unexpected)[:300], kind="unavailable")
    yield {
        "done": True,
        "text": "".join(parts),
        "provider": provider,
        "error": None if error is None else {"kind": error.kind, "message": str(error)[:500]},
    }


def parse_json_answer(raw: str) -> dict:
    """Извлекает JSON из ответа модели: удаляет обрамление ``` и посторонний текст вокруг фигурных скобок."""
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"В ответе модели нет JSON-объекта: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


async def chat_json(messages: list[dict], model: str | None = None, allow_fallback: bool = False,
                    deadline: float | None = None) -> dict:
    """Как chat(), но ответ разбирается в JSON (parse_json_answer).

    Невалидный JSON не фатален: один повторный запрос с указанием модели на
    ошибку формата, в остатке того же бюджета deadline. Второй некорректный ответ
    пробрасывается наружу (ValueError/JSONDecodeError).
    """
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
        remaining = _remaining_budget(deadline, started)
        return parse_json_answer(await chat(retry, temperature=0.0, model=model,
                                            allow_fallback=allow_fallback, deadline=remaining))
