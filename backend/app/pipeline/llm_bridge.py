"""HTTP-мост к LLM через сервис ml-extraction (POST /chat_json).

Query-слой (планировщик вопросов, генерация ответов) использует каскад
моделей сервиса ml-extraction: основной — Яндекс, запасной — Ollama minimax.
Прямой импорт пакета ml_extraction в контейнере backend невозможен —
сервисы общаются только по HTTP.

При отказе обеих моделей поднимается LLMUnavailableError с типом причины:
ответ пользователю в этом случае собирается без генерации (см. query.py),
а причина показывается явно.
"""
from __future__ import annotations

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


async def chat_json(messages: list[dict], model: str | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(LLM_CHAT_URL, json={"messages": messages, "model": model})
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise LLMUnavailableError("unavailable", str(e)) from e
    if response.status_code >= 400:
        # ml-extraction кладёт в detail {"kind", "message"} (см. /chat_json)
        detail: object = None
        try:
            detail = response.json().get("detail")
        except ValueError:
            pass
        if isinstance(detail, dict) and detail.get("kind"):
            raise LLMUnavailableError(str(detail["kind"]), str(detail.get("message", "")))
        raise LLMUnavailableError("unavailable", f"HTTP {response.status_code}: {response.text[:300]}")
    return response.json()["result"]
