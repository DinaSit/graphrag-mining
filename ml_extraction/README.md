# ML Extraction Service

Сервис извлечения фактов из документов. Заменяет `ml-mock`:
тот же контракт `POST /extract {fragments} → {candidates}`, внутри — модель
Qwen3.6-35B через Yandex AI Studio (OpenAI-совместимый API).

Промпт собирается при старте из доменной конфигурации `domain/default/`
(онтология, словарь терминов); ответы модели валидируются и нормализуются
под контракт `extraction-schema.json`. Код backend не изменяется: подключение —
через переменную `EXTRACTION_SERVICE_URL` (см. `docker-compose.override.yml`).

## Настройка

В корне репозитория создать `.env` (файл в `.gitignore`):

```
YANDEX_API_KEY=<ключ Yandex AI Studio>
YANDEX_FOLDER_ID=<folder id>
```

## Запуск

В составе стека (backend переключается на сервис автоматически через override):

```bash
docker compose up --build ml-extraction backend
```

Локально:

```bash
cd ml_extraction
pip install -r requirements.txt
set -a; source ../.env; set +a
uvicorn app.main:app --port 8002
```

## Скрипты

```bash
# диагностика API: чат, JSON-формат, vision, эмбеддинги, контрольное извлечение
python scripts/probe_api.py

# прогон извлечения по папке документов (сервис должен быть запущен)
python scripts/run_corpus.py --src <папка> --out results/ --limit 3
```

## Структура

```
app/
  main.py                 FastAPI: /extract, /chat_json, /chat_stream, /web_search, /web_answer, /embed, /health
  extractor.py            оркестрация: фрагменты → LLM → кандидаты
  prompt.py               сборка промпта из domain/default
  prompts/extraction.md   шаблон промпта извлечения
  yandex_client.py        клиент Yandex AI Studio: chat, chat_json, chat_stream (каскад Яндекс → фолбэк)
  web_search.py           поиск по разрешённым доменам (ddgs) и LLM-сводка веб-ответа
  embeddings.py           локальные эмбеддинги bge-m3 (1024)
  schemas.py              копии контрактных моделей backend
  config.py               конфигурация из переменных окружения
scripts/
  probe_api.py            диагностика доступа к API
  run_corpus.py           прогон по корпусу → JSON-результаты
```

## Формат кандидата

`payload` содержит плоские поля по `domain/default/extraction-schema.json`
(совместимость с текущим backend) и дополнительные поля `entities`, `relations`,
`numeric_parameters` — задел под расширение схемы графа до полной онтологии.
Данные сохраняются в PostgreSQL (JSONB) без изменений схемы.

## Интеграция

`app/yandex_client.py` — общая точка доступа к LLM: `chat()`, `chat_json()`,
`embed(kind="doc"|"query")`. Для документов и поисковых запросов у Яндекса
раздельные модели эмбеддингов: при индексации использовать `kind="doc"`,
при поиске — `kind="query"`. Размерность векторов — 256.
