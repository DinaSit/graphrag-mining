"""Конфигурация сервиса извлечения (зона ML-A). Всё — из переменных окружения."""
import os
from pathlib import Path

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
# Folder id задаётся ТОЛЬКО окружением (.env): дефолт в коде публиковал бы
# реальный идентификатор каталога в репозитории
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
YANDEX_BASE_URL = os.environ.get("YANDEX_BASE_URL", "https://llm.api.cloud.yandex.net/v1")

# Короткое имя достраивается до gpt://<folder>/<name>; полный URI можно задать напрямую
YANDEX_MODEL = os.environ.get("YANDEX_MODEL", "qwen3.6-35b-a3b/latest")

# Запасной OpenAI-совместимый сервер (Ollama) для query-вызовов; пустая строка — фолбек выключен.
# Извлечение (/extract) фолбеком не пользуется: инжест работает только через основной LLM.
FALLBACK_BASE_URL = os.environ.get("FALLBACK_BASE_URL", "")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "minimax-m3:cloud")
# Ключ запасного сервера. Пустой — запрос без Authorization (локальному Ollama ключ не нужен).
# Ключ Яндекса на сторонний FALLBACK_BASE_URL не отправляется никогда.
FALLBACK_API_KEY = os.environ.get("FALLBACK_API_KEY", "")

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "2"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "6"))

# Суммарный бюджет одного query-вызова (/chat_json): ретраи основного провайдера
# плюс фолбэк должны уложиться в него. Меньше 120 с — таймаута httpx на стороне
# backend (llm_bridge), иначе фолбэк не успевает ответить до обрыва соединения.
CHAT_DEADLINE = float(os.environ.get("CHAT_DEADLINE", "100"))

# domain/default монтируется в контейнер (см. docker-compose.override.yml)
DOMAIN_DIR = Path(os.environ.get("DOMAIN_DIR", str(Path(__file__).resolve().parents[2] / "domain" / "default")))

MIN_FRAGMENT_CHARS = int(os.environ.get("MIN_FRAGMENT_CHARS", "40"))


def model_uri(name: str, scheme: str = "gpt") -> str:
    if name.startswith(("gpt://", "emb://")):
        return name
    if "yandex" not in YANDEX_BASE_URL:
        # Сторонний OpenAI-совместимый сервер (например, Ollama): имя модели как есть
        return name
    return f"{scheme}://{YANDEX_FOLDER_ID}/{name}"
