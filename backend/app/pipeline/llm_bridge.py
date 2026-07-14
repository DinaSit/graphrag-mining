"""HTTP-мост к LLM через сервис ml-extraction (POST /chat_json, /chat_stream).

Query-слой (планировщик вопросов, генерация ответов) использует каскад
моделей сервиса ml-extraction: основной — Яндекс, запасной — Ollama minimax.
Прямой импорт пакета ml_extraction в контейнере backend невозможен —
сервисы общаются только по HTTP.

При отказе обеих моделей поднимается LLMUnavailableError с типом причины:
ответ пользователю в этом случае собирается без генерации (см. query.py),
а причина показывается явно.
"""
from __future__ import annotations

import json
import os

import httpx

LLM_CHAT_URL = os.environ.get("LLM_CHAT_URL", "http://ml-extraction:8002/chat_json")

_KIND_HUMAN = {
    "auth": "нет доступа к LLM: API-ключ отклонён",
    "quota": "лимит токенов LLM исчерпан (квота возобновляется примерно каждые 20 минут)",
    "bad_response": "LLM вернула некорректный ответ",
    "unavailable": "LLM-сервис недоступен",
}


class LLMUnavailableError(Exception):
    """LLM не ответила. kind: auth / quota / bad_response / unavailable."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind if kind in _KIND_HUMAN else "unavailable"

    def human(self) -> str:
        """Короткая причина для показа пользователю."""
        return _KIND_HUMAN[self.kind]


def _error_from_body(status_code: int, body_text: str) -> LLMUnavailableError:
    """ml-extraction кладёт в detail {"kind", "message"} (см. /chat_json, /chat_stream)."""
    detail: object = None
    try:
        payload = json.loads(body_text)
        detail = payload.get("detail") if isinstance(payload, dict) else None
    except ValueError:
        pass
    if isinstance(detail, dict) and detail.get("kind"):
        return LLMUnavailableError(str(detail["kind"]), str(detail.get("message", "")))
    return LLMUnavailableError("unavailable", f"HTTP {status_code}: {body_text[:300]}")


async def chat_json(messages: list[dict], model: str | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(LLM_CHAT_URL, json={"messages": messages, "model": model})
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise LLMUnavailableError("unavailable", str(e)) from e
    if response.status_code >= 400:
        raise _error_from_body(response.status_code, response.text)
    return response.json()["result"]


async def chat_stream(messages: list[dict], model: str | None = None):
    """Стриминговый аналог chat_json (POST /chat_stream сервиса ml-extraction).

    Асинхронный генератор: выдаёт {"delta": "<порция текста>"} по мере
    генерации; последним ВСЕГДА идёт терминальное событие
    {"done": True, "text": "<весь накопленный текст>",
     "error": LLMUnavailableError | None} — исключения наружу не
    выбрасываются, чтобы SSE-обработчик /ask/stream гарантированно
    отправил финальное событие. Ошибки протокола (detail с kind)
    преобразуются в LLMUnavailableError так же, как в chat_json.
    """
    url = LLM_CHAT_URL.replace("/chat_json", "/chat_stream")
    accumulated: list[str] = []

    def _terminal(error: LLMUnavailableError | None) -> dict:
        return {"done": True, "text": "".join(accumulated), "error": error}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json={"messages": messages, "model": model}) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    yield _terminal(_error_from_body(response.status_code, body.decode("utf-8", "replace")))
                    return
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    # SSE-события разделяются пустой строкой; событие может
                    # быть разделено между порциями — данные накапливаются в буфере
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        for line in raw_event.splitlines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(line[len("data:"):].strip())
                            except ValueError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            if payload.get("done"):
                                error: LLMUnavailableError | None = None
                                raw_error = payload.get("error")
                                if isinstance(raw_error, dict):
                                    error = LLMUnavailableError(
                                        str(raw_error.get("kind") or "unavailable"),
                                        str(raw_error.get("message") or ""),
                                    )
                                yield {"done": True, "text": str(payload.get("text") or "".join(accumulated)), "error": error}
                                return
                            delta = payload.get("delta")
                            if delta:
                                accumulated.append(str(delta))
                                yield {"delta": str(delta)}
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        yield _terminal(LLMUnavailableError("unavailable", str(exc)))
        return
    # Поток закрылся без терминальной записи — контракт нарушен, сервис
    # считается недоступным (накопленный текст всё равно возвращаем)
    yield _terminal(LLMUnavailableError("unavailable", "стрим завершился без терминальной записи"))


# Экранированные последовательности JSON-строки (кроме \\uXXXX — см. автомат)
_STRING_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


class SummaryStreamExtractor:
    """Конечный автомат: извлекает значение ключа "summary" из ПОТОКА
    сырого JSON-текста модели, не дожидаясь конца генерации.

    Модель отвечает JSON, где "summary" — первый ключ (ANSWER_SYSTEM_PROMPT),
    поэтому его значение можно отдавать пользователю по мере прихода дельт.
    feed(chunk) возвращает очередную порцию ДЕКОДИРОВАННОГО текста summary
    (экранирование \\", \\n, \\uXXXX и суррогатные пары разворачиваются);
    после закрывающей кавычки остаток JSON игнорируется. Поток без
    ключа "summary" не порождает вывода.
    """

    _KEY = '"summary"'

    def __init__(self) -> None:
        self._state = "seek_key"  # seek_key -> seek_colon -> seek_quote -> in_string -> done
        self._tail = ""  # хвост потока: ключ может прийти разрезанным между дельтами
        self._escaped = False
        self._unicode: str | None = None  # накопитель hex-цифр \uXXXX
        self._high_surrogate: str | None = None  # первая половина суррогатной пары

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            self._step(ch, out)
        return "".join(out)

    def _step(self, ch: str, out: list[str]) -> None:
        if self._state == "seek_key":
            self._tail = (self._tail + ch)[-len(self._KEY):]
            if self._tail == self._KEY:
                self._state = "seek_colon"
        elif self._state == "seek_colon":
            if ch == ":":
                self._state = "seek_quote"
            elif not ch.isspace():
                # после "summary" не двоеточие — совпадение было внутри строки
                self._state = "seek_key"
                self._tail = ""
        elif self._state == "seek_quote":
            if ch == '"':
                self._state = "in_string"
            elif not ch.isspace():
                self._state = "seek_key"
                self._tail = ""
        elif self._state == "in_string":
            self._string_char(ch, out)
        # state == "done": остаток JSON накапливается у вызывающего, вывод не производится

    def _string_char(self, ch: str, out: list[str]) -> None:
        if self._unicode is not None:
            self._unicode += ch
            if len(self._unicode) == 4:
                self._emit_unicode(out)
            return
        if self._escaped:
            self._escaped = False
            if ch == "u":
                self._unicode = ""
                return
            self._flush_surrogate(out)
            out.append(_STRING_ESCAPES.get(ch, ch))
            return
        if ch == "\\":
            self._escaped = True
            return
        if ch == '"':
            self._flush_surrogate(out)
            self._state = "done"
            return
        self._flush_surrogate(out)
        out.append(ch)

    def _emit_unicode(self, out: list[str]) -> None:
        try:
            code = int(self._unicode or "", 16)
        except ValueError:
            self._unicode = None
            return
        self._unicode = None
        if self._high_surrogate is not None:
            high = self._high_surrogate
            self._high_surrogate = None
            if 0xDC00 <= code <= 0xDFFF:
                out.append(chr(0x10000 + ((ord(high) - 0xD800) << 10) + (code - 0xDC00)))
                return
            out.append(high)
        if 0xD800 <= code <= 0xDBFF:
            self._high_surrogate = chr(code)
            return
        out.append(chr(code))

    def _flush_surrogate(self, out: list[str]) -> None:
        # Незакрытая суррогатная пара: отдаём как есть, символы не теряем
        if self._high_surrogate is not None:
            out.append(self._high_surrogate)
            self._high_surrogate = None
