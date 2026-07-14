#!/usr/bin/env python3
"""Диагностика доступа к Yandex AI Studio и сервиса ml-extraction.

Проверяет: доступность чата и его латентность, соблюдение JSON-формата,
приём изображений (vision), эмбеддинги doc/query через POST /embed
работающего сервиса (локальная bge-m3) и их размерность,
извлечение на контрольном фрагменте. API-ключ не выводится.

Запуск: python scripts/probe_api.py (ключ — в .env корня репозитория;
для шага эмбеддингов должен быть запущен сервис ml-extraction,
адрес — env ML_SERVICE_URL, по умолчанию http://localhost:8002).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

# Корень ml_extraction в sys.path — импорт `app.*` работает при запуске из любой директории
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env корня репозитория (для локального запуска без docker)
_env_file = Path(__file__).resolve().parents[2] / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from app import config, yandex_client  # noqa: E402

# PNG 1x1 px для проверки приёма изображений
_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


async def main() -> int:
    print(f"Модель: {config.model_uri(config.YANDEX_MODEL)}")
    ok = True

    # --- 1. Чат и латентность ---
    try:
        t0 = time.monotonic()
        answer = await yandex_client.chat([{"role": "user", "content": "Ответь одним словом: работаю"}])
        print(f"[1] чат: OK за {time.monotonic() - t0:.1f}с — {answer.strip()[:60]!r}")
    except Exception as e:
        ok = False
        print(f"[1] чат: FAIL — {e}")

    # --- 2. JSON-дисциплина ---
    try:
        t0 = time.monotonic()
        data = await yandex_client.chat_json([{
            "role": "user",
            "content": 'Верни строго JSON: {"materials": ["никель", "медь"], "count": 2}',
        }])
        assert data.get("count") == 2, f"неожиданный ответ: {data}"
        print(f"[2] json: OK за {time.monotonic() - t0:.1f}с")
    except Exception as e:
        ok = False
        print(f"[2] json: FAIL — {e}")

    # --- 3. Vision (мультимодальный вход) ---
    try:
        t0 = time.monotonic()
        answer = await yandex_client.chat([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Опиши изображение одним предложением."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"}},
            ],
        }])
        print(f"[3] vision: OK за {time.monotonic() - t0:.1f}с — {answer.strip()[:60]!r}")
    except Exception as e:
        ok = False
        print(f"[3] vision: FAIL — {e} (vision недоступен: обработка сканов невозможна)")

    # --- 4. Эмбеддинги doc/query: POST /embed работающего сервиса (bge-m3) ---
    service_url = os.environ.get("ML_SERVICE_URL", "http://localhost:8002")
    for kind in ("doc", "query"):
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{service_url}/embed",
                    json={"texts": ["обессоливание шахтных вод", "electrowinning"], "kind": kind},
                )
            resp.raise_for_status()
            payload = resp.json()
            vecs = payload["embeddings"]
            print(f"[4] embeddings {kind}: OK за {time.monotonic() - t0:.1f}с, "
                  f"размерность {payload.get('dimensions') or len(vecs[0])}, модель {payload.get('model')}")
        except Exception as e:
            ok = False
            print(f"[4] embeddings {kind}: FAIL — {e} (сервис ml-extraction запущен на {service_url}?)")

    # --- 5. Мини-извлечение на реальном тексте ---
    try:
        from app.extractor import extract_fragments
        from app.schemas import SourceFragment

        t0 = time.monotonic()
        fragment = SourceFragment(
            id="probe-1", document_id="probe-doc", version_id="v1", page=1,
            text=("Скорость циркуляции католита при электроэкстракции никеля "
                  "поддерживалась в диапазоне 0.2–0.4 м/с при температуре 60 °C, "
                  "что повысило выход металла на 3,5 %."),
            normalized_text="",
        )
        candidates = await extract_fragments([fragment])
        print(f"[5] извлечение: OK за {time.monotonic() - t0:.1f}с, кандидатов: {len(candidates)}")
        for c in candidates:
            print(json.dumps(c.payload, ensure_ascii=False, indent=2)[:600])
    except Exception as e:
        ok = False
        print(f"[5] извлечение: FAIL — {e}")

    print("\nИтог:", "OK" if ok else "обнаружены ошибки (см. выше)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
