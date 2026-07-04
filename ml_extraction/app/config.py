"""Конфигурация сервиса извлечения (зона ML-A). Всё — из переменных окружения."""
import os
from pathlib import Path

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "b1ggusvist6c2sia1dno")
YANDEX_BASE_URL = os.environ.get("YANDEX_BASE_URL", "https://llm.api.cloud.yandex.net/v1")

# Короткое имя достраивается до gpt://<folder>/<name>; полный URI можно задать напрямую
YANDEX_MODEL = os.environ.get("YANDEX_MODEL", "qwen3.6-35b-a3b/latest")
YANDEX_EMB_DOC_MODEL = os.environ.get("YANDEX_EMB_DOC_MODEL", "text-embeddings-v2-doc/latest")
YANDEX_EMB_QUERY_MODEL = os.environ.get("YANDEX_EMB_QUERY_MODEL", "text-embeddings-v2-query/latest")

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "2"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "6"))

# domain/default монтируется в контейнер (см. docker-compose.override.yml)
DOMAIN_DIR = Path(os.environ.get("DOMAIN_DIR", str(Path(__file__).resolve().parents[2] / "domain" / "default")))

MIN_FRAGMENT_CHARS = int(os.environ.get("MIN_FRAGMENT_CHARS", "40"))


def model_uri(name: str, scheme: str = "gpt") -> str:
    if name.startswith(("gpt://", "emb://")):
        return name
    return f"{scheme}://{YANDEX_FOLDER_ID}/{name}"
