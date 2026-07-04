# Scientific Multimodal GraphRAG

Scientific Multimodal GraphRAG - локальный стек для построения карты знаний R&D в горно-металлургической предметной области. Система загружает документы, хранит исходники, разбивает содержимое на evidence units, извлекает кандидаты фактов, сохраняет источники и статусы в PostgreSQL, проецирует подтвержденные факты в Neo4j и показывает результат в Streamlit.

Проект сделан как Python/FastAPI backend с заменяемыми портами для парсеров, LLM, embeddings, vector store и graph store. Извлечение знаний вынесено в отдельный HTTP-сервис: базовый compose поднимает `ml-mock`, а `docker-compose.override.yml` подключает реальный `ml-extraction` на Yandex AI Studio без переписывания API и UI.

Основной сценарий:

1. Пользователь загружает документ через Streamlit UI или REST API.
2. Оригинальный файл сохраняется в S3-совместимое хранилище MinIO.
3. Метаданные документа, версия, статус и `storage_uri` пишутся в PostgreSQL.
4. Parser adapter извлекает текст и табличные строки в canonical source fragments / evidence units.
5. Backend вызывает сервис извлечения знаний по HTTP-контракту `/extract`.
6. Сервис извлечения возвращает кандидаты фактов с обязательной ссылкой на source fragment.
7. Backend валидирует числовые значения через regex + Pint-compatible нормализацию.
8. Кандидаты с достаточной уверенностью утверждаются автоматически, остальные остаются в review.
9. Утвержденные факты пишутся в PostgreSQL и проецируются в Neo4j.
10. Source fragments индексируются embedding provider-ом в pgvector.
11. `/ask` собирает evidence pack, graph payload, источники, противоречия, пробелы и краткий ответ.
12. Streamlit показывает загрузку, вопрос, ответ, источники, блоки "противоречия / пробелы / гипотезы" и простую визуализацию графа.

## Технологический стек

- Python 3.12.
- FastAPI для REST API.
- Pydantic v2 для схем запросов и ответов.
- PostgreSQL 16 + pgvector для документов, source fragments, candidates, facts и vector rows.
- Neo4j 5 Community для графа знаний.
- MinIO как S3-compatible file storage для оригиналов документов.
- Streamlit для основного UI.
- Graphviz через `st.graphviz_chart` для простой визуализации цепочки графа.
- SQLAlchemy-модели как декларативное описание таблиц.
- SQL migrations через `backend/migrations/001_init.sql`.
- `psycopg` для прямой записи в PostgreSQL.
- `neo4j` Python driver для записи графа.
- `pdfplumber`, `python-docx`, `python-pptx`, `openpyxl` для парсинга документов.
- Pint-compatible validation для нормализации чисел и единиц.
- Docker Compose для локального запуска всего стека.
- Pytest/unittest для smoke и core pipeline тестов.

## Как запустить

Полный локальный стенд описан в `docker-compose.yml`:

```powershell
docker compose up --build
```

Если рядом лежит `docker-compose.override.yml`, Docker Compose применит его автоматически: backend будет использовать `ml-extraction`, bge-m3 embeddings и веб-поиск через ML-сервис. Для этого в `.env` должны быть заданы `YANDEX_API_KEY` и `YANDEX_FOLDER_ID`.

Сервисы:

- Streamlit UI: `http://localhost:8501`;
- FastAPI backend: `http://localhost:8000`;
- OpenAPI: `http://localhost:8000/docs`;
- ML mock extraction service: `http://localhost:8001`;
- ML extraction service при активном override: `http://localhost:8002`;
- PostgreSQL: `localhost:5432`, база `graphrag`;
- Neo4j Browser: `http://localhost:7474`;
- MinIO API: `http://localhost:9000`;
- MinIO Console: `http://localhost:9001`.

Локальные доступы:

- PostgreSQL: `graphrag / graphrag`;
- Neo4j: `neo4j / graphrag-demo`;
- MinIO: `graphrag / graphrag-demo`, bucket `graphrag-documents`.

Для проверки контейнеров:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```

Для запуска тестов внутри backend-контейнера:

```powershell
docker compose exec backend python -m pytest tests
```

## Основные REST API

### Health

`GET /health`

Возвращает состояние backend и подключенных storage-сервисов:

- `postgres`: `enabled` или `memory`;
- `neo4j`: `enabled` или `memory`;
- `minio`: `enabled` или `disabled`;
- `extraction`: `remote` или `local`.

### Ingest

`POST /ingest`

Загружает multipart-файл и запускает полный ingest pipeline. Параметры:

- `file` - обязательная часть multipart-запроса;
- `document_type` - необязательный тип документа, например `pdf`, `txt`, `xlsx`;
- `source_label` - необязательная пользовательская метка источника;
- `access_level` - область доступа, по умолчанию `uploaded`.

При успехе возвращает документ, статус и количество evidence units:

```json
{
  "document": {
    "id": "doc-...",
    "filename": "sample_alloy_x.txt",
    "document_type": "txt",
    "status": "completed",
    "element_count": 1,
    "storage_uri": "s3://graphrag-documents/documents/doc-.../doc-...-v1/sample_alloy_x.txt"
  },
  "status": "completed",
  "evidence_units": 1
}
```

Повторная загрузка того же файла не создает дубль: backend проверяет SHA-256 сначала в памяти, затем в PostgreSQL.

Пример:

```powershell
curl.exe -X POST "http://localhost:8000/ingest" `
  -F "file=@.\sample_corpus\sample_alloy_x.txt" `
  -F "document_type=txt" `
  -F "source_label=sample_alloy_x.txt" `
  -F "access_level=uploaded"
```

### Ask

`POST /ask`

Основной endpoint вопрос-ответ. Принимает естественный вопрос, фильтры и флаг показа гипотез.

```json
{
  "question": "Что делали со сплавом X при температуре 700-750 °C и как изменялась твёрдость?",
  "include_hypotheses": false,
  "confidence_min": 0
}
```

Ответ содержит:

- `summary` - краткую сводку;
- `experiments` - таблицу найденных фактов/экспериментов;
- `sources` - source refs с `document_id`, `version_id`, `fragment_id`, page и quote;
- `graph` - nodes/edges для визуализации;
- `contradictions` - противоречия между источниками;
- `gaps` - пробелы в покрытии данных;
- `confidence` - среднюю уверенность по выбранным фактам.

Пример:

```powershell
$body = @{
  question = "Что делали со сплавом X при температуре 700-750 °C и как изменялась твёрдость?"
  include_hypotheses = $false
  confidence_min = 0
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8000/ask -ContentType "application/json; charset=utf-8" -Body $body
```

### Graph

`GET /graph`

Возвращает graph payload из Neo4j. Если Neo4j недоступен или граф пустой, используется in-memory fallback из текущего `ApplicationStore`.

Упрощенный формат:

```json
{
  "nodes": [
    {"id": "material-...", "label": "Сплав X", "type": "Material", "data": {}}
  ],
  "edges": [
    {"id": "...", "source": "claim-...", "target": "material-...", "label": "ABOUT"}
  ]
}
```

### Facts

`GET /facts`

Возвращает утвержденные факты из PostgreSQL или in-memory fallback. Используется для проверки, что extraction и approve pipeline записали результат в storage layer.

### Documents

`GET /documents`

Возвращает загруженные документы, статусы, количество evidence units и `storage_uri`.

`GET /documents/{document_id}`

Возвращает документ и его source fragments.

`GET /documents/{document_id}/status`

Возвращает текущий статус документа.

`POST /documents/{document_id}/reprocess`

Повторно запускает extraction по уже созданным fragments. Сейчас используется как lightweight reprocess endpoint без фоновой очереди.

### Search

`POST /search`

Запускает hybrid-like поиск по source fragments. Текущая реализация объединяет deterministic vector similarity и простой lexical score.

Параметры:

- `query` - поисковая строка;
- `top_k` - лимит результатов;
- `filters` - базовые фильтры.

### Review

`GET /review/facts`

Возвращает extraction candidates. Можно фильтровать по статусу.

`POST /review/facts/{candidate_id}/approve`

Утверждает кандидата и создает `Fact`. Факт не может быть утвержден без `SourceRef`.

`POST /review/facts/{candidate_id}/reject`

Отклоняет кандидата.

### Ontology

`GET /ontology`

Возвращает текущий YAML онтологии из `domain/default/ontology.yaml`.

`GET /ontology/versions`

Возвращает список версий онтологии.

`GET /ontology/candidates`

Возвращает кандидаты расширения онтологии.

`POST /ontology/candidates/{candidate_id}/approve`

Утверждает ontology candidate.

`POST /ontology/candidates/{candidate_id}/reject`

Отклоняет ontology candidate.

`POST /ontology/candidates/{candidate_id}/merge`

Помечает candidate как merged с существующим типом.

## Модульная структура

Проект не разбит на отдельные Python packages, но логически организован как модульный backend с явными портами и адаптерами.

```text
backend/
  app/
    main.py                 FastAPI composition root и REST endpoints
    storage.py              ApplicationStore и основной ingest pipeline
    persistence.py          PostgreSQL и Neo4j adapters
    file_storage.py         MinIO/S3 adapter
    schemas.py              Pydantic модели API и домена
    db_models.py            SQLAlchemy описание таблиц
    ml_mock.py              mock extraction service
    pipeline/
      interfaces.py         ParserAdapter, LLMProvider, EmbeddingProvider, VectorStore, GraphStore
      parsers.py            PDF/DOCX/PPTX/XLSX/CSV/plain text parsers
      providers.py          MockLLM, RemoteExtractionProvider, embedding providers
      normalization.py      ontology/synonyms/unit normalization
      validation.py         regex + unit-compatible numeric validation
      query.py              QueryOrchestrator для /ask и /search
      sample_data.py        synthetic sample corpus seeding
  migrations/
    001_init.sql            PostgreSQL + pgvector schema
  tests/
    test_quality_gates.py   quality gates и numeric validation
    test_query_contract.py  contract tests для /ask
    test_web_answer.py      web answer contract

domain/default/
  ontology.yaml             онтология R&D металлургии
  extraction-schema.json    JSON schema извлечения
  synonyms.csv              aliases RU/EN
  units.yaml                единицы измерения
  ranking.yaml              baseline ranking config
  validation-rules.yaml     правила числовой валидации
  query-templates.yaml      шаблоны вопрос-ответ
  prompts/extraction.ru.md  prompt contract для extraction

streamlit_app/
  app.py                    основной UI
  Dockerfile
  requirements.txt

infra/
  neo4j_constraints.cypher  constraints для Neo4j
```

## Модуль `backend.app.main`

`main.py` является composition root backend-а:

- создает `ApplicationStore`;
- подключает `PostgresSink`, `Neo4jSink`, `MinioFileStorage`;
- включает `RemoteExtractionProvider` через `EXTRACTION_SERVICE_URL`;
- инициализирует sample data для локальной демонстрации;
- регистрирует FastAPI endpoints.

Важное решение: sample data нужна только для того, чтобы после `docker compose up --build` UI сразу показывал живую цепочку. Реальные документы попадают через `/ingest` и сохраняются в MinIO.

## Модуль `storage`

### `ApplicationStore`

Центральный application service. Отвечает за:

- прием файла;
- вычисление SHA-256;
- защиту от дублей;
- создание `DocumentRecord` и `DocumentVersion`;
- сохранение оригинала через `MinioFileStorage`;
- вызов parser adapter;
- сохранение source fragments;
- вызов extraction provider;
- валидацию candidates;
- auto approve по confidence threshold;
- запись facts в PostgreSQL и Neo4j;
- индексацию fragments через deterministic embeddings.

### `SourceRequiredError`

Исключение для защиты правила данных: утвержденный факт не может попасть в граф без ссылки на источник.

## Модуль `pipeline.parsers`

Parser layer приводит разные форматы к единой модели `SourceFragment`.

Поддерживаемые форматы:

- `.txt`, `.md`, `.json` через `PlainTextParser`;
- `.csv` через `CsvParser`;
- `.xlsx`, `.xlsm` через `XlsxParser`;
- `.pdf` через `PdfParser`/`pdfplumber`;
- `.docx`, `.docm` через `DocxParser`;
- `.pptx` через `PptxParser`;
- неизвестные форматы через `BinaryPlaceholderParser`.

Решение: если файл зарегистрирован, но текст извлечь нельзя, создается placeholder fragment. Это лучше, чем молча терять документ: пользователь видит, что нужен OCR, конвертация или новый adapter.

## Модуль `pipeline.providers`

### `MockLLMProvider`

Детерминированный mock provider для первого этапа. Умеет:

- извлекать candidates из CSV/XLSX row data;
- извлекать простой факт про `Сплав X` из plain text;
- парсить контрольные вопросы про материал, свойство и температурный диапазон;
- возвращать baseline summary.

### `RemoteExtractionProvider`

HTTP adapter к внешнему сервису извлечения. Отправляет fragments на `EXTRACTION_SERVICE_URL` и ожидает JSON с candidates. Если сервис недоступен или вернул некорректный JSON, ingest падает с явной ошибкой: пустое извлечение не маскируется под успешную обработку.

Это позволяет подключать реальный extraction service без изменения REST API и UI.

### `DeterministicEmbeddingProvider`

Простой hash-based embedding provider на 64 измерения. Нужен как baseline без API-ключей и без GPU. В production-цепочке должен быть заменен на `bge-m3` или другой embeddings service.

## Модуль `pipeline.query`

### `QueryOrchestrator`

Оркестратор для `/ask` и `/search`.

Алгоритм `/ask`:

1. Парсит вопрос через `LLMProvider.parse_question`.
2. Фильтрует facts по confidence, material, property, laboratory и temperature range.
3. Убирает hypotheses, если `include_hypotheses=false`.
4. Ранжирует facts по confidence и совпадению с parsed query.
5. Формирует таблицу экспериментов.
6. Собирает sources.
7. Находит противоречия по направлениям эффекта.
8. Находит пробелы: мало лабораторий, мало длительностей, неполное покрытие температур.
9. Собирает graph payload по выбранным facts.
10. Возвращает `QueryResponse`.

## Модуль `pipeline.validation`

Числовая валидация выполняется в backend рядом с ingest pipeline.

`validate_candidate_numbers`:

- ищет числа и единицы через regex;
- нормализует температуру, длительность, проценты, концентрации, давление, длину, напряжение, плотность тока, расход, энергоёмкость и pH;
- сопоставляет извлеченные поля `temperature_c`, `duration_h`, `effect_value` с source evidence;
- добавляет в payload diagnostics: `validated`, `issues`, `matched_fields`, `quantities`.

Поддерживаемые единицы:

- `°C`, `C`, `С`;
- `h`, `ч`, `час`, `часа`, `часов`;
- `%`;
- `мг/л`, `мг/дм3`, `мг/дм³`, `mg/l`, `ppm`, `г/л`, `г/дм3`, `г/т`;
- `м/с`, `m/s`, `м3/ч`, `м³/ч`;
- `MPa`, `МПа`, `bar`, `бар`, `atm`, `атм`;
- `mm`, `мм`, `m`, `м`, `мкм`, `um`;
- `V`, `В`, `mV`, `мВ`;
- `A/m2`, `А/м²`, `кВт·ч/т`, `t/h`, `т/ч`, `pH`.

## Модуль `persistence`

### `PostgresSink`

Адаптер записи в PostgreSQL. Отвечает за:

- upsert документов и версий;
- сохранение source fragments;
- сохранение extraction candidates;
- сохранение facts;
- запись vectors в pgvector;
- lookup документа по checksum для защиты от дублей после рестарта backend-а;
- compatibility migration для storage columns через `ensure_schema`.

### `Neo4jSink`

Адаптер записи графа. Проецирует `Fact` в узлы и связи:

- `Material`;
- `Experiment`;
- `Property`;
- `Effect`;
- `Laboratory`;
- `SourceFragment`;
- `Claim`;
- `ABOUT`;
- `BASED_ON`;
- `MEASURES`;
- `PRODUCED`;
- `SUPPORTS`;
- `CONDUCTED_BY`.

## Модуль `file_storage`

### `MinioFileStorage`

S3-compatible adapter для исходных файлов. Путь объекта:

```text
documents/{document_id}/{version_id}/{filename}
```

Возвращает:

- bucket;
- object name;
- `s3://...` URI.

Если MinIO временно недоступен, adapter сохраняет ошибку в `last_error` и не валит весь ingest. Это fail-soft поведение важно для локальной разработки.

## Модуль `ml_mock`

`backend/app/ml_mock.py` - отдельное FastAPI-приложение mock extraction service.

Endpoint:

- `GET /health`;
- `POST /extract`.

Сервис принимает fragments и возвращает extraction candidates в том же формате, который backend ожидает от будущей реальной модели. В docker-compose он поднимается как отдельный service `ml-mock`.

## Модуль `streamlit_app`

Streamlit UI предназначен для демонстрации полного сценария без знания графовых БД.

UI содержит:

- индикатор состояния backend, PostgreSQL, Neo4j, MinIO и extraction service;
- загрузку одного или нескольких файлов;
- поле вопроса;
- переключатель `include_hypotheses`;
- slider `confidence_min`;
- блок ответа;
- таблицу фактов/экспериментов;
- блоки "Противоречия", "Пробелы", "Гипотезы";
- раскрывающиеся источники;
- граф через `st.graphviz_chart`;
- таблицу документов;
- таблицу facts;
- общий graph payload.

## Domain Package

`domain/default` хранит заменяемую доменную конфигурацию.

### `ontology.yaml`

Типы сущностей и отношений для горно-металлургического R&D GraphRAG.

### `extraction-schema.json`

JSON schema для результата извлечения. Используется как контракт candidates между backend и сервисом извлечения.

### `synonyms.csv`

Словарь алиасов и терминов RU/EN. Используется normalizer-ом для canonical entity names.

### `units.yaml`

Канонические единицы измерения.

### `validation-rules.yaml`

Правила диапазонов и plausibility checks для чисел.

### `ranking.yaml`

Baseline ranking weights.

### `query-templates.yaml`

Шаблоны контрольных вопросов и evidence patterns.

## Схема данных

SQL schema находится в `backend/migrations/001_init.sql`.

### `documents`

Хранит метаданные документа:

- `id`;
- `filename`;
- `document_type`;
- `source_label`;
- `access_level`;
- `checksum`;
- `current_version_id`;
- `status`;
- `element_count`;
- `storage_bucket`;
- `storage_object`;
- `storage_uri`;
- `created_at`.

`checksum` уникален и используется для idempotent upload.

### `document_versions`

Хранит версии документа:

- `id`;
- `document_id`;
- `checksum`;
- `version_number`;
- `status`;
- `parser`;
- `created_at`.

### `source_fragments`

Хранит canonical evidence units:

- `id`;
- `document_id`;
- `version_id`;
- `page`;
- `element_type`;
- `section`;
- `text`;
- `normalized_text`;
- `fragment_metadata`.

### `extraction_candidates`

Хранит кандидаты фактов:

- `id`;
- `type`;
- `payload`;
- `source`;
- `confidence`;
- `status`;
- `review_note`.

Кандидаты могут быть `pending_review`, `approved`, `rejected`.

### `facts`

Хранит утвержденные факты:

- `id`;
- `candidate_id`;
- `material`;
- `material_id`;
- `experiment_id`;
- `sample`;
- `process`;
- `temperature_c`;
- `duration_h`;
- `property`;
- `effect_direction`;
- `effect_value`;
- `effect_unit`;
- `result_value`;
- `result_unit`;
- `lab`;
- `team`;
- `equipment`;
- `confidence`;
- `status`;
- `is_hypothesis`;
- `source`.

Правило: final fact всегда должен иметь source с `document_id`, `version_id`, `fragment_id`.

### `fragment_vectors`

Хранит embedding vector для source fragment:

- `fragment_id`;
- `embedding_model`;
- `embedding vector(64)`;
- `vector_metadata`.

### `ontology_versions`

Хранит версии онтологии:

- `id`;
- `version`;
- `status`;
- `config`.

### `jobs`

Зарезервированная таблица для фоновых задач:

- `id`;
- `job_type`;
- `status`;
- `payload`;
- `error`.

Сейчас pipeline выполняется синхронно, но таблица оставлена для будущего job runner-а.

## Конфигурация

Основные параметры задаются через environment variables в `docker-compose.yml` и `.env.example`.

### PostgreSQL

```text
DATABASE_URL=postgresql+psycopg://graphrag:graphrag@postgres:5432/graphrag
```

### Neo4j

```text
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=graphrag-demo
```

### MinIO

```text
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=graphrag
MINIO_SECRET_KEY=graphrag-demo
MINIO_BUCKET=graphrag-documents
MINIO_SECURE=false
```

### Extraction service

```text
EXTRACTION_SERVICE_URL=http://ml-mock:8001/extract
```

Если переменная не задана, backend использует локальный `MockLLMProvider`.

### Providers

```text
LLM_PROVIDER=mock
EMBEDDING_PROVIDER=deterministic-hash
```

Эти переменные сейчас документируют режим работы. Реальная замена провайдеров выполняется через wiring в `ApplicationStore` и adapters.

### Streamlit

```text
BACKEND_URL=http://backend:8000
```

## Важные проектные решения

### Mock-first, но с реальными портами

Система работает без API-ключей, GPU и скачивания моделей. При этом интерфейсы `LLMProvider`, `EmbeddingProvider`, `VectorStore`, `GraphStore`, `ParserAdapter` уже выделены. Это позволяет показывать сквозной сценарий локально и подключать реальные ML-сервисы позже.

### LLM создает кандидатов, не факты

Извлечение возвращает `ExtractionCandidate`. Final `Fact` создается только после validation/approve. Это защищает граф от непроверенных данных.

### Source required

Факт не может быть утвержден без ссылки на source fragment. Это зафиксировано в `SourceRequiredError` и тесте `test_fact_requires_source`.

### Оригиналы файлов хранятся в MinIO

Compose не монтирует локальную папку `Источники информации`. Это намеренно: локальные материалы не должны быть скрытой зависимостью инфраструктуры. Документы попадают в систему через upload/API и дальше живут в MinIO.

### Idempotent upload по checksum

Повторная загрузка того же файла возвращает существующий документ. Проверка работает и после рестарта backend-а, потому что есть lookup по PostgreSQL.

### Neo4j как projection store

PostgreSQL остается источником фактов, источников и статусов. Neo4j используется для graph projection и навигации по связям.

### pgvector baseline

Векторы пишутся в PostgreSQL через pgvector. Сейчас embeddings deterministic, но схема и запись уже готовы к bge-m3 или другому embedding service.

### External adapters

`RemoteExtractionProvider` останавливает ingest с явной ошибкой, если внешний сервис извлечения недоступен. `MinioFileStorage` не валит весь ingest при временной ошибке MinIO. Это повышает видимость ошибок извлечения и сохраняет удобство локальной разработки.

### Streamlit вместо React

Основной интерфейс сделан на Streamlit: он быстро показывает полный сценарий без отдельной frontend-сборки.

## Тесты

Backend tests находятся в `backend/tests/`.

Проверяется:

- query contract для прямых и смежных фактов;
- quality gates для грязных фактов и числовой валидации;
- нормализация совместимых единиц в числовых условиях;
- фиксация противоречий по противоположным направлениям эффекта;
- web answer contract при отсутствии прямых фактов.

ML extraction tests находятся в `ml_extraction/tests/`.

Запуск локально:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests
```

Запуск в Docker:

```powershell
docker compose exec backend python -m pytest tests
```

## Smoke сценарий

1. Поднять стек:

```powershell
docker compose up --build
```

2. Открыть Streamlit:

```text
http://localhost:8501
```

3. Проверить health в верхнем блоке UI.

4. Загрузить документ из `sample_corpus` или свой PDF/DOCX/XLSX.

5. Задать контрольный вопрос:

```text
Что делали со сплавом X при температуре 700-750 °C и как изменялась твёрдость?
```

6. Проверить, что UI показывает:

- summary;
- таблицу экспериментов;
- источники;
- противоречия;
- пробелы;
- гипотезы;
- graph visualization.

## План развития

Текущая версия уже поднимает полный локальный контур и показывает сквозной сценарий. При активном `docker-compose.override.yml` backend использует реальный extraction service, bge-m3 embeddings и веб-поиск через ML-сервис. Для перехода к более сильной версии нужно усилить несколько частей:

- прогнать извлечение по реальному корпусу документов;
- расширить query planner для сложных многофакторных вопросов;
- собрать evidence pack через Cypher, semantic search и numeric filters;
- добавить grounded answer generation строго по найденным источникам;
- финализировать онтологию, aliases RU/EN и validation ranges;
- подготовить mini-golden set для проверки качества извлечения;
- вручную проверить candidates, дубли и конфликтующие факты перед показом;
- добавить справочники экспертов, лабораторий, географии, годов публикации и тегов.

## Ограничения текущей версии

- Базовый `docker-compose.yml` остаётся mock-first для автономного локального запуска; реальный ML-контур подключается через `docker-compose.override.yml`.
- Query planner покрывает базовые вопросы и контрольный вопрос, но не все сложные multiparameter queries.
- OCR, chart understanding и обработка сканов без текстового слоя не реализованы.
- Полноценная ролевая модель доступа и аудит действий не реализованы.
- Географические и временные фильтры заложены как будущие расширения, но не покрыты полностью.
- Ingest выполняется через очередь воркеров; синхронный режим доступен через `INGEST_WORKERS=0`.

## Полезные файлы

- `docker-compose.yml` - весь локальный стек.
- `backend/migrations/001_init.sql` - схема PostgreSQL.
- `backend/app/main.py` - FastAPI endpoints.
- `backend/app/storage.py` - основной ingest pipeline.
- `backend/app/persistence.py` - PostgreSQL/Neo4j adapters.
- `backend/app/file_storage.py` - MinIO adapter.
- `backend/app/ml_mock.py` - mock extraction service.
- `streamlit_app/app.py` - UI.
- `domain/default/ontology.yaml` - онтология.
- `domain/default/extraction-schema.json` - контракт извлечения.
- `sample_corpus/sample_alloy_x.txt` - минимальный sample-файл для smoke-проверки.
