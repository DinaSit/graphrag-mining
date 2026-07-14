# ML Extraction Service

Сервис извлечения фактов, эмбеддингов и веб-поиска (порт 8002, зона ML-A).
Заменяет `ml-mock`: контракт `POST /extract {fragments} → {candidates}` сохранён,
извлечение выполняет LLM через Yandex AI Studio (OpenAI-совместимый API).
Код backend не меняется: подключение — через `EXTRACTION_SERVICE_URL`
(см. `docker-compose.override.yml`).

## Эндпоинты

| Метод | Путь | Назначение |
| --- | --- | --- |
| POST | `/extract` | Извлечение фактов из фрагментов; только основной провайдер, без резервного; 503 без `YANDEX_API_KEY`, 502 с `{kind, message}` при отказе провайдера |
| POST | `/chat_json` | LLM для query-слоя backend: `{messages} → {result: <JSON модели>}`; каскад «основной → резервный», бюджет `CHAT_DEADLINE` |
| POST | `/chat_stream` | Потоковый LLM (SSE): записи `{delta}` и одна терминальная `{done, text, provider, error}` — она отправляется всегда, в том числе при отказе обоих провайдеров |
| POST | `/embed` | Эмбеддинги локальной bge-m3: `{texts, kind} → {embeddings, dimensions: 1024, model}`; `kind` сохранён для совместимости контракта |
| GET | `/web_sources` | Реестр веб-площадок по умолчанию; единственный источник этого списка для UI |
| POST | `/web_search` | Поиск без LLM: ddgs по разрешённым доменам и научные API; метки `source` в выдаче показывают вклад каждой ветки |
| POST | `/web_answer` | Ответ из внешних источников: поиск и LLM-сводка; при отказе обоих провайдеров возвращаются выдержки со ссылками без сводки; в граф не записывает |
| GET | `/health` | Статус и модель основного провайдера, резервного и последнего query-вызова |

## Веб-поиск

Две ветки опрашиваются параллельно и независимо, сбой одной не прерывает другую:
ddgs по восьми разрешённым доменам (русским вопросом и его английским переводом)
и научные API без ключей — arXiv, Crossref, Semantic Scholar. Перевод RU→EN
выполняется локально (argos-translate, текст за пределы сервиса не передаётся)
с доменным глоссарием терминов. Объединённая выдача дедуплицируется по URL
и заголовку и фильтруется по семантической близости к вопросу (bge-m3, порог
`WEB_RELEVANCE_MIN`). UI может передать собственный реестр площадок: строки
реестра включают и отключают домены и научные API.

## Настройка

В корне репозитория создать `.env` (файл в `.gitignore`):

```
YANDEX_API_KEY=<ключ Yandex AI Studio>
YANDEX_FOLDER_ID=<folder id>
```

Остальные параметры имеют значения по умолчанию (`app/config.py`,
`docker-compose.override.yml`):

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `YANDEX_BASE_URL` / `YANDEX_MODEL` | `llm.api.cloud.yandex.net/v1` / `qwen3.6-35b-a3b/latest` | Основной провайдер; короткое имя модели достраивается до `gpt://<folder>/<name>` |
| `FALLBACK_BASE_URL` / `FALLBACK_MODEL` / `FALLBACK_API_KEY` | пусто / `minimax-m3:cloud` / пусто | Резервный OpenAI-совместимый сервер — только для query-вызовов; ключ Яндекса на него не отправляется |
| `LLM_TIMEOUT` / `LLM_RETRIES` / `LLM_CONCURRENCY` | 120 / 6 / 2 | Таймаут попытки, число повторов, ограничение параллельных вызовов |
| `CHAT_DEADLINE` | 100 | Суммарный бюджет query-вызова (повторы и резервный провайдер); должен быть меньше HTTP-таймаута backend |
| `WEB_SEARCH_TIMEOUT` / `WEB_LLM_TIMEOUT` / `WEB_RELEVANCE_MIN` | 12 / 45 / 0.45 | Бюджеты веб-контура и порог семантического фильтра |
| `DOMAIN_DIR` / `MIN_FRAGMENT_CHARS` | `domain/default` / 40 | Доменная конфигурация и минимальная длина фрагмента |

## Структура

```
app/
  main.py                 FastAPI: эндпоинты из таблицы выше
  extractor.py            извлечение: фрагменты → LLM → кандидаты
  prompt.py               сборка промпта из domain/default; режимы full и light
  prompts/                шаблоны промптов извлечения (полный и облегчённый)
  yandex_client.py        клиент Yandex AI Studio: chat/chat_json/chat_stream,
                          каскад провайдеров, повторы, ограничение параллелизма
  embeddings.py           локальная bge-m3 (1024 измерения, кросс-язычная RU/EN);
                          модель фиксирована: замена требует пересчёта всех векторов
  web_search.py           ddgs и научные API: реестр площадок, дедупликация, LLM-сводка
  scientific_sources.py   arXiv, Crossref, Semantic Scholar — без ключей, изолированно,
                          бюджет 6 с на источник
  translate.py            локальный перевод RU→EN для научных API, доменный глоссарий
  schemas.py              копии контрактных моделей backend; при изменении схем
                          синхронизируются вручную
  config.py               конфигурация из переменных окружения
scripts/
  probe_api.py            диагностика API: чат, JSON-формат, vision, эмбеддинги
  run_corpus.py           извлечение по папке документов → JSON в results/
  load_corpus.py          загрузка корпуса в базу знаний через /ingest backend
tests/                    5 файлов, 50 тестов; сеть замещена httpx.MockTransport
```

## Запуск

В составе стека (backend подключается к сервису автоматически через override):

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

Тесты — в отдельном контейнере (pytest в образ не входит):

```bash
docker compose run --rm --no-deps -T \
  -v "$PWD/ml_extraction/tests:/srv/tests" \
  ml-extraction sh -c "pip install -q pytest pytest-asyncio; python -m pytest tests -q"
```

Веса bge-m3 (~2,3 ГБ) скачиваются при первом обращении и сохраняются в томе
`hf-cache` независимо от пересборки образа. Модель перевода устанавливается
при сборке образа (требуется сеть); во время работы перевод выполняется локально.

## Формат кандидата

`payload` содержит плоские поля по `domain/default/extraction-schema.json`
(совместимость с backend) и дополнительные `entities`, `relations`,
`numeric_parameters` — задел под расширение схемы графа до полной онтологии.
Сохраняются в PostgreSQL (JSONB) без изменения схемы.
