# Scientific Multimodal GraphRAG

Scientific Multimodal GraphRAG - локальный стек для построения карты знаний R&D в горно-металлургической предметной области. Система загружает документы, хранит исходники, разбивает содержимое на evidence units, извлекает кандидаты фактов, сохраняет источники и статусы в PostgreSQL, проецирует подтвержденные факты в Neo4j и показывает результат в Streamlit.

Проект сделан как Python/FastAPI backend с заменяемыми адаптерами для парсеров, LLM, embeddings и хранилищ. Извлечение знаний вынесено в отдельный HTTP-сервис: базовый compose поднимает `ml-mock`, а `docker-compose.override.yml` подключает реальный `ml-extraction` на Yandex AI Studio без переписывания API и UI.

Основной сценарий:

1. Пользователь загружает документ через Streamlit UI или REST API.
2. Backend ставит документ в очередь фоновых воркеров (`INGEST_WORKERS`, по умолчанию 2) и сразу отвечает `{"job_id", "filename", "status": "queued"}`; статус обработки поллится через `GET /jobs/{job_id}`.
3. Оригинальный файл сохраняется в S3-совместимое хранилище MinIO.
4. Метаданные документа, версия, статус и `storage_uri` пишутся в PostgreSQL.
5. Parser adapter извлекает текст и табличные строки в canonical source fragments / evidence units. PDF-страницы, где текстовый слой короче 40 символов или больше половины площади занято растровым изображением, дополнительно рендерятся в PNG для vision-обработки.
6. Backend вызывает сервис извлечения знаний по HTTP-контракту `/extract` пачками фрагментов.
7. Сервис извлечения возвращает кандидаты фактов с обязательной ссылкой на source fragment. Фатальная ошибка LLM-провайдера даёт HTTP 502, и документ помечается `failed` - не `completed` без фактов.
8. Backend валидирует числовые значения: regex-извлечение чисел и диапазонов, сверка с источником в нормализованных единицах с проверкой размерности, правдоподобие по правилам `validation-rules.yaml`.
9. Кандидаты с достаточной уверенностью и корректными числами утверждаются автоматически, остальные остаются в review.
10. Утвержденные факты пишутся в PostgreSQL и проецируются в Neo4j; противоположные эффекты по одной паре материал/свойство помечаются `conflicting` со ссылками друг на друга.
11. Source fragments индексируются embedding provider-ом в pgvector (пачками по 16).
12. `/ask` (и его полный алиас `/query`) собирает evidence pack, graph payload, источники, противоречия, пробелы, гипотезы и краткий ответ; при отсутствии прямых фактов подключается ступень веб-поиска через ML-сервис.
13. Streamlit показывает загрузку, вопрос, ответ, источники, блоки "противоречия / пробелы / гипотезы" и простую визуализацию графа.

## Технологический стек

- Python 3.12.
- FastAPI для REST API.
- Pydantic v2 для схем запросов и ответов.
- PostgreSQL 16 + pgvector для документов, source fragments, candidates, facts, vector rows и статусов jobs.
- Neo4j 5 Community для графа знаний.
- MinIO как S3-compatible file storage для оригиналов документов.
- Streamlit для основного UI.
- Graphviz через `st.graphviz_chart` для простой визуализации цепочки графа.
- SQL-схема через `backend/migrations/001_init.sql` (без ORM).
- `psycopg` для прямой записи в PostgreSQL, включая pgvector-колонки.
- `neo4j` Python driver для записи графа (один драйвер на процесс).
- `pdfplumber` + `PyMuPDF` для PDF (текстовый слой + vision-рендер страниц-сканов), `python-docx`, `python-pptx`, `openpyxl` для остальных форматов.
- Собственная таблица единиц измерения для нормализации чисел (regex + проверка размерности, без Pint).
- `httpx` для HTTP-вызовов между сервисами.
- В сервисе `ml-extraction`: Yandex AI Studio (OpenAI-совместимый API), локальные эмбеддинги `BAAI/bge-m3` через sentence-transformers, веб-поиск через `ddgs`.
- Docker Compose для локального запуска всего стека.
- Pytest/unittest для smoke и core pipeline тестов.

## Как запустить

Полный локальный стенд описан в `docker-compose.yml`:

```powershell
docker compose up --build
```

Если рядом лежит `docker-compose.override.yml`, Docker Compose применит его автоматически: backend будет использовать `ml-extraction`, bge-m3 embeddings и веб-поиск через ML-сервис. Для этого в `.env` должен быть задан `YANDEX_API_KEY` (и при необходимости `YANDEX_FOLDER_ID`). Стек поднимается и без ключа, но извлечение будет отвечать явной ошибкой авторизации (документы получат статус `failed`); для полностью автономного mock-режима переименуйте override-файл - стек вернётся на `ml-mock`.

Сервисы:

- Streamlit UI: `http://localhost:8501`;
- FastAPI backend: `http://localhost:8000`;
- OpenAPI: `http://localhost:8000/docs`;
- ML mock extraction service: `http://localhost:8001`;
- ML extraction service при активном override: `http://localhost:8002`;
- PostgreSQL: `localhost:5432`, база `graphrag` (при активном override хост-порт `15432` - базовый занят локальным сервисом);
- Neo4j Browser: `http://localhost:7474`;
- MinIO API: `http://localhost:9000`;
- MinIO Console: `http://localhost:9001`.

Локальные доступы:

- PostgreSQL: `graphrag / graphrag`;
- Neo4j: `neo4j / graphrag-demo`;
- MinIO: `graphrag / graphrag-demo`, bucket `graphrag-documents`.

Каталог `./domain` монтируется read-only и в `backend`, и в `ml-extraction`: правки `synonyms.csv`, `validation-rules.yaml`, `ontology.yaml` подхватываются рестартом контейнеров без пересборки образов.

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
- `extraction`: `remote` или `local`;
- `postgres_last_error`, `neo4j_last_error`, `minio_last_error` - текст последней ошибки каждого хранилища (или `null`): сбой записи виден в мониторинге, даже если операция уже отработала свой failed-путь;
- `answer_llm_status`, `answer_llm_provider`, `answer_llm_model`, `answer_llm_error` - доступность LLM-каскада для query-слоя (backend опрашивает `/health` сервиса по `LLM_CHAT_URL`; без ml-extraction - `unavailable`).

### Ingest

`POST /ingest`

Загружает multipart-файл и ставит его в очередь ingest pipeline. Параметры:

- `file` - обязательная часть multipart-запроса;
- `document_type` - необязательный тип документа, например `pdf`, `txt`, `xlsx`;
- `source_label` - необязательная пользовательская метка источника;
- `access_level` - область доступа, по умолчанию `uploaded`.

При постановке в очередь возвращает:

```json
{
  "job_id": "job-...",
  "filename": "sample_alloy_x.txt",
  "status": "queued"
}
```

Дальше статус отслеживается через `GET /jobs/{job_id}`; так работает и Streamlit UI. Повторная загрузка того же файла не создает дубль: backend проверяет SHA-256 сначала в памяти, затем в PostgreSQL - и для дубликата сразу возвращает `{"document", "status", "evidence_units"}` без постановки в очередь. Тот же синхронный формат ответа возвращается при `INGEST_WORKERS=0`.

Пример (сначала создаётся минимальный тестовый файл):

```powershell
Set-Content -Path .\sample_alloy_x.txt -Encoding UTF8 -Value "Сплав X отжигали при 700 °C 2 ч, твёрдость повышалась на 12 %."
curl.exe -X POST "http://localhost:8000/ingest" `
  -F "file=@.\sample_alloy_x.txt" `
  -F "document_type=txt" `
  -F "source_label=sample_alloy_x.txt" `
  -F "access_level=uploaded"
```

### Jobs

`GET /jobs/{job_id}`

Статус фоновой задачи: `{"job_id", "status": "queued|processing|completed|failed", "error": null|"..."}`. Источник статусов - in-memory реестр очереди, поэтому endpoint работает и без PostgreSQL; таблица `jobs` в PG - персистентная копия для задач из прошлых запусков backend-а.

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

- `summary` - краткую сводку (генерируется LLM строго по evidence pack; при недоступности LLM собирается деградированный ответ);
- `experiments` - таблицу найденных фактов/экспериментов;
- `sources` - source refs с `document_id`, `version_id`, `fragment_id`, page и quote;
- `graph` - nodes/edges для визуализации;
- `contradictions` - противоречия между источниками;
- `gaps` - пробелы в покрытии данных;
- `hypotheses` - гипотезы на основе косвенных данных;
- `confidence` - среднюю уверенность по выбранным фактам;
- `search_hits` - результаты семантического поиска (работает без LLM);
- `has_direct_facts` и `evidence_status` (`direct` / `partial` / `none`) - нашлись ли прямые факты;
- `related_experiments`, `related_sources`, `related_graph` - смежные факты, когда прямого ответа нет;
- `web_answer` - ответ из внешних источников (не верифицирован, в граф не пишется), заполняется при `WEB_ANSWER_URL` и отсутствии прямых фактов;
- `llm_error` - человекочитаемая причина, если LLM-каскад недоступен;
- `offtopic` - вопрос не про базу знаний (смолток вроде «как дела?»): пайплайн, LLM и веб-поиск не запускались, ответ мгновенный.

`POST /query` - полный алиас `/ask`, включая ступень веб-поиска.

Ускорение `/ask`:

- оффтоп-роутер: вопрос без цифр, единиц и доменных терминов (словарь синонимов + стемы R&D-лексики) отвечается мгновенно подсказкой, не тратя LLM-вызовы; эвристика ошибается только в безопасную сторону - сомнительный вопрос идёт в полный пайплайн;
- семантический поиск и LLM-разбор вопроса выполняются параллельно;
- готовые ответы кэшируются (`ANSWER_CACHE_TTL`, по умолчанию 1800 с): повторный вопрос возвращается мгновенно, кэш сбрасывается при любом изменении данных (инжест, approve/reject, удаление);
- эмбеддинги кэшируются по SHA-256 текста (раздельно для query/doc-режимов) - повторные вопросы и переиндексация не ходят в сервис эмбеддингов.

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

Возвращает утвержденные факты из PostgreSQL (включая `conflicts_with` и `is_hypothesis`) или in-memory fallback. Используется для проверки, что extraction и approve pipeline записали результат в storage layer.

### Documents

`GET /documents`

Возвращает загруженные документы, статусы, количество evidence units и `storage_uri`.

`GET /documents/{document_id}`

Возвращает документ и его source fragments.

`GET /documents/{document_id}/status`

Возвращает текущий статус документа.

`DELETE /documents/{document_id}`

Каскадно удаляет документ со всем извлечённым: фрагменты, кандидаты, факты, векторы, узлы графа, файл в MinIO; у оставшихся фактов снимается пометка спора с удалёнными. При сбое каскада отвечает 500 с `detail` - память не тронута, удаление можно повторить.

`POST /documents/{document_id}/reprocess`

Повторно запускает extraction по уже сохранённым fragments - как фоновая задача через ту же очередь (`{"job_id", "status": "queued"}`). Отклонённые экспертом кандидаты не перезаписываются.

### Candidates

`POST /candidates`

Принимает готовых кандидатов извне (бэкфилл, ручная разметка). Кандидаты проходят штатный путь: валидация чисел, пороги утверждения, фиксация противоречий, проекция в граф; кандидат без source не может стать approved.

### Entities / Experiments

`GET /entities/{entity_id}` - факты, связанные с сущностью (материал, эксперимент, факт, фрагмент).

`GET /entities/{entity_id}/graph?depth=N` - окрестность сущности; глубину обхода (1..5) выполняет Neo4j, без него отдаётся in-memory окрестность фиксированной глубины.

`GET /experiments/{experiment_id}` - факты и граф одного эксперимента.

### Search

`POST /search`

Запускает hybrid-поиск по source fragments: близость векторов считает pgvector, финальный порядок определяет гибридный скоринг с лексической добавкой. Требует PostgreSQL (pgvector).

Параметры:

- `query` - поисковая строка;
- `top_k` - лимит результатов;
- `filters` - базовые фильтры.

### Review

`GET /review/facts`

Возвращает extraction candidates. Можно фильтровать по статусу.

`POST /review/facts/{candidate_id}/approve`

Утверждает кандидата и создает `Fact`. Кандидат без `SourceRef` не утверждается - ответ 400.

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

Решения по кандидатам онтологии персистятся в таблицу `ontology_versions` и переживают рестарт backend-а.

## Модульная структура

Проект не разбит на отдельные Python packages, но логически организован как модульный backend с явными адаптерами.

```text
backend/
  app/
    main.py                 FastAPI composition root и REST endpoints
    jobs.py                 очередь фоновой обработки загрузок (in-memory + запись в jobs)
    storage.py              ApplicationStore и основной ingest pipeline
    persistence.py          PostgreSQL и Neo4j adapters
    file_storage.py         MinIO/S3 adapter
    schemas.py              Pydantic модели API и домена
    ml_mock.py              mock extraction service
    pipeline/
      parsers.py            PDF/DOCX/PPTX/XLSX/CSV/plain text parsers
      providers.py          MockLLM, RemoteExtractionProvider, embedding providers
      normalization.py      канонизация сущностей по synonyms.csv
      validation.py         regex + unit-compatible numeric validation
      llm_bridge.py         HTTP-мост к LLM (POST /chat_json сервиса ml-extraction)
      query_parsing.py      LLMQuestionParser (Query Planner)
      query.py              QueryOrchestrator для /ask и /search
      sample_data.py        synthetic sample corpus seeding (по запросу, через скрипт)
  migrations/
    001_init.sql            PostgreSQL + pgvector schema
  scripts/
    seed_sample_data.py     ручной посев синтетических данных
    reprocess_documents.py  массовая переобработка документов
  tests/
    test_number_validation.py числовое извлечение и сверка единиц
    test_parsers.py         кодировки, CSV-заголовки, DOCX-нумерация
    test_quality_gates.py   quality gates и numeric validation
    test_query_contract.py  contract tests для /ask
    test_web_answer.py      web answer contract

ml_extraction/
  app/                      сервис извлечения на Yandex AI Studio
  scripts/                  probe_api.py, load_corpus.py, run_corpus.py
  tests/                    test_web_search.py
  README.md                 подробности сервиса

domain/default/
  ontology.yaml             онтология R&D металлургии (читается backend и ml-extraction)
  synonyms.csv              aliases RU/EN (читается backend и ml-extraction)
  validation-rules.yaml     правила числовой валидации (читается backend)
  extraction-schema.json    JSON schema извлечения (справочный контракт)
  units.yaml                единицы измерения (справочный)
  ranking.yaml              baseline ranking config (справочный)
  query-templates.yaml      шаблоны вопрос-ответ (справочные)
  prompts/extraction.ru.md  prompt contract для extraction (справочный)

streamlit_app/
  app.py                    основной UI
  Dockerfile
  requirements.txt
```

Constraints уникальности id в Neo4j применяет сам backend идемпотентно при инициализации `Neo4jSink` - отдельного cypher-файла в `infra/` больше нет.

## Модуль `backend.app.main`

`main.py` является composition root backend-а:

- создает `ApplicationStore`;
- подключает `PostgresSink`, `Neo4jSink`, `MinioFileStorage`;
- включает `RemoteExtractionProvider` через `EXTRACTION_SERVICE_URL`;
- восстанавливает состояние из PostgreSQL (`hydrate_from_postgres`) и запускает фоновую переиндексацию фрагментов без векторов;
- поднимает `IngestQueue` с воркерами фоновой обработки;
- регистрирует FastAPI endpoints, включая ступень веб-поиска в `/ask`.

Синтетические данные при старте не создаются: реальные документы попадают через `/ingest` и живут в MinIO. Для демонстрации без документов есть ручной скрипт `backend/scripts/seed_sample_data.py`.

## Модуль `jobs`

`IngestQueue` - очередь в памяти плюс фиксированное число воркеров-потоков. Загрузка и переобработка выполняются вне HTTP-запроса: API отвечает сразу, статусы задач хранятся в памяти процесса (источник для `GET /jobs/{id}`) и дублируются в таблицу `jobs` PostgreSQL. Недоступность PG не роняет воркер и не теряет задачу. `INGEST_WORKERS=0` возвращает синхронный режим.

## Модуль `storage`

### `ApplicationStore`

Центральный application service. Отвечает за:

- прием файла;
- вычисление SHA-256 и защиту от дублей (атомарно под локом; UNIQUE(checksum) в PG - последний рубеж при двух процессах);
- создание `DocumentRecord` и `DocumentVersion`;
- сохранение оригинала через `MinioFileStorage`;
- вызов parser adapter;
- сохранение source fragments;
- вызов extraction provider;
- валидацию candidates (числа сверяются с полным текстом фрагмента, а не с обрезанной цитатой);
- auto approve по порогам из `validation-rules.yaml` (по умолчанию approve от 0.85, reject ниже 0.60); кандидаты с непрошедшими числовую валидацию значениями в граф автоматически не попадают - только через эксперта;
- фиксацию противоречий: противоположные направления эффекта по одной паре материал/свойство помечают оба факта `conflicting` и связывают через `conflicts_with`;
- запись facts в PostgreSQL и Neo4j (включая проекцию извлечённых сущностей и связей онтологии);
- индексацию fragments пачками по 16;
- каскадное удаление документа;
- переобработку, которая не воскрешает удалённые документы и не перезаписывает rejected-кандидатов.

Ошибки записи PG/Neo4j/MinIO логируются и пробрасываются: инжест помечается `failed` с текстом ошибки, тихой потери данных нет. Ошибка `load_state` при гидрации роняет старт backend-а - частичное состояние запрещено (в compose backend стартует после `service_healthy` PostgreSQL).

### `SourceRequiredError`

Исключение для защиты правила данных: утвержденный факт не может попасть в граф без ссылки на источник.

## Модуль `pipeline.parsers`

Parser layer приводит разные форматы к единой модели `SourceFragment`. Реестр `_PARSER_BY_EXTENSION` - единственный источник правды по форматам; `SUPPORTED_EXTENSIONS` выводится из него.

Поддерживаемые форматы:

- `.txt`, `.md`, `.json` через `PlainTextParser` (декодирование: utf-8-sig strict, затем cp1251 strict, затем utf-8 с replace);
- `.csv` через `CsvParser` (дублирующиеся заголовки получают суффиксы `_2`, `_3`; лишние ячейки именуются `column_N`);
- `.xlsx`, `.xlsm` через `XlsxParser`;
- `.pdf` через `PdfParser`: текстовый слой берёт `pdfplumber`, а страницы с текстовым слоем короче 40 символов либо с растром, покрывающим больше половины площади (сканы с колонтитулом/OCR-штампом), рендерятся PyMuPDF в PNG и передаются сервису извлечения для vision-обработки;
- `.docx`, `.docm` через `DocxParser`: у DOCX нет страниц, адрес фрагмента - единый сквозной номер блока, общий для абзацев и табличных строк;
- `.pptx` через `PptxParser`;
- неизвестные форматы через `BinaryPlaceholderParser`.

Решение: если файл зарегистрирован, но текст извлечь нельзя, создается placeholder fragment. Это лучше, чем молча терять документ: пользователь видит, что нужен OCR, конвертация или новый adapter.

## Модуль `pipeline.providers`

### `MockLLMProvider`

Детерминированный mock provider для автономного режима. Реализует `extract_entities`: извлекает candidates из CSV/XLSX row data и простой факт про `Сплав X` из plain text.

### `RemoteExtractionProvider`

HTTP adapter к внешнему сервису извлечения. Отправляет fragments на `EXTRACTION_SERVICE_URL` пачками (`EXTRACTION_BATCH_SIZE`, по умолчанию 8) с таймаутом `EXTRACTION_TIMEOUT`. Если сервис недоступен, вернул ошибку или некорректный JSON, ingest падает с явной ошибкой: пустое извлечение не маскируется под успешную обработку.

Это позволяет подключать реальный extraction service без изменения REST API и UI.

### `RemoteEmbeddingProvider`

HTTP adapter к сервису эмбеддингов (`EMBEDDINGS_URL`, POST → `{"embeddings": [...]}`), с раздельными режимами документов и запросов. Размерность задаётся `EMBEDDING_DIM`. Fallback на детерминированный провайдер отсутствует намеренно: сбой индексации должен быть видимым, смешение векторов разных моделей делает поиск некорректным.

### `DeterministicEmbeddingProvider`

Простой hash-based embedding provider на 64 измерения. Нужен как baseline без API-ключей и без GPU; используется, когда `EMBEDDINGS_URL` не задан.

## Модули `pipeline.llm_bridge`, `pipeline.query_parsing`, `pipeline.query`

### `llm_bridge`

Backend не вызывает LLM напрямую: query-слой ходит по HTTP в `POST /chat_json` сервиса `ml-extraction` (`LLM_CHAT_URL`), где работает каскад моделей - основная Yandex AI Studio, запасная задаётся `FALLBACK_BASE_URL`/`FALLBACK_MODEL`. При отказе обеих моделей поднимается `LLMUnavailableError` с типом причины (`auth` / `quota` / `bad_response` / `unavailable`) - ответ пользователю собирается без генерации, а причина показывается явно в `llm_error`.

### `LLMQuestionParser`

Единственный разборщик вопросов в системе: Query Planner поверх `chat_json` разбирает вопрос в структуру (intent, material, process, property, entities, числовые conditions, целевой показатель, keywords).

### `QueryOrchestrator`

Оркестратор для `/ask` и `/search`. Алгоритм `/ask`:

1. Разбирает вопрос через `LLMQuestionParser` (отказ LLM не прячется, а копится в `llm_errors`).
2. Обходит граф по разобранным сущностям и фильтрует facts по confidence, material, property, laboratory и числовым условиям; фильтры и ранжирование канонизируются с обеих сторон (synonyms + casefold + ё→е); числовые условия сравниваются в совместимых единицах.
3. Убирает hypotheses, если `include_hypotheses=false`.
4. Всегда выполняет семантический поиск (не зависит от LLM) и возвращает его результаты.
5. Если прямых фактов нет - ищет смежные факты и формирует гипотезы.
6. Формирует таблицу экспериментов, sources, противоречия (с учётом условий: совпадающий процесс и близость температур ±5 °C), пробелы и graph payload.
7. Собирает evidence pack и генерирует ответ LLM строго по нему; если модель сама оценила данные как недостаточные (`sufficient=false`), факты перемещаются в блок related и включается ступень веб-поиска.
8. Возвращает `QueryResponse`.

## Модуль `pipeline.validation`

Числовая валидация выполняется в backend рядом с ingest pipeline.

Извлечение чисел двухпроходное: сначала диапазоны («700-750 °C», «200...300 мг/л» - единица приписывается обоим концам), затем одиночные числа. Поддерживаются разделители тысяч пробелом/NBSP («12 000» = 12000), десятичная запятая, отрицательные значения (включая минус U+2212), кириллическое и смешанное «рН» (в том числе диапазон pH 4-6). Однобуквенные единицы регистрозависимы: «С» - Цельсий, «с» - секунды, «В» - вольты, строчное «в» и буквы внутри слов единицами не считаются.

`validate_candidate_numbers`:

- сопоставляет извлеченные поля `temperature_c`, `duration_h`, `effect_value` и именованные `numeric_parameters` с числами источника - в нормализованных единицах с проверкой совместимости размерности («5 г/л» в тексте не подтверждает извлечённые «5 мг/л»);
- `effect_value` сверяется в `effect_unit`; без единицы трактуется как проценты;
- проверяет правдоподобие значений по именованным правилам из `validation-rules.yaml` (диапазон правила применяется после приведения к единице правила);
- добавляет в payload diagnostics: `validated`, `issues`, `matched_fields`, `quantities`.

Поддерживаемые единицы (с автоконверсией внутри размерности):

- температура: `°C`, `C`, `С`;
- длительность: `h`, `ч`, `час(а/ов)`, `мин`/`min` и `сек`/`sec`/`s` с конверсией в часы;
- проценты: `%`;
- концентрации: `мг/л`, `мг/дм3`, `мг/дм³`, `mg/l`, `ppm`, `г/л`, `г/дм3`, `г/т`;
- скорость и расход: `м/с`, `m/s`, `м3/ч`, `м³/ч`;
- давление: `MPa`, `МПа`, `bar`, `бар`, `atm`, `атм`;
- длина: `mm`, `мм`, `m`, `м`, `мкм`, `um`;
- напряжение: `V`, `В`, `mV`, `мВ`;
- прочее: `A/m2`, `А/м²`, `кВт·ч/т`, `t/h`, `т/ч`, `т/сут` (= т/ч ÷ 24), `кгс`/`kgf`, `NTU`, `мСм/см`, `pH`.

## Модуль `persistence`

### `PostgresSink`

Адаптер записи в PostgreSQL. Отвечает за:

- upsert документов и версий;
- сохранение source fragments;
- сохранение extraction candidates;
- сохранение facts (включая `conflicts_with` и `is_hypothesis`);
- запись vectors в pgvector и векторный поиск;
- статусы jobs;
- персистентность кандидатов онтологии (строки `ontology-candidate` в `ontology_versions`);
- lookup документа по checksum для защиты от дублей после рестарта backend-а;
- `load_state` для полной гидрации in-memory состояния;
- `ensure_schema`: compatibility-миграции колонок; тип `fragment_vectors.embedding` приводится к `vector(EMBEDDING_DIM)` только при расхождении - несовместимые векторы вычищаются, а фоновая переиндексация при старте восстанавливает индекс.

### `Neo4jSink`

Адаптер записи графа: один драйвер на процесс, constraints уникальности `id` применяются идемпотентно при инициализации для всех меток онтологии плюс `Claim`, `SourceFragment`, `Effect`, `Laboratory`. Недоступность Neo4j на старте не валит backend - constraints доприменяются при первой записи.

Проекция `Fact` в узлы и связи:

- `Material`, `Experiment`, `Property`, `Effect`, `Laboratory`, `SourceFragment`, `Claim`;
- `ABOUT`, `BASED_ON`, `MEASURES`, `PRODUCED`, `SUPPORTS`, `CONDUCTED_BY`.

Дополнительно `upsert_semantics` проецирует извлечённые сущности и связи доменной онтологии (whitelist меток `Material`, `Process`, `Equipment`, `Property`, `NumericParameter`, `Condition`, `Experiment`, `Publication`, `Expert`, `Facility`, `Result`, `Recommendation`, `Region` и типов связей из `ontology.yaml`).

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

Ошибка MinIO записывается в `last_error` (виден в `/health`) и пробрасывается: инжест помечается `failed`, вместо тихой потери оригинала. Каскадное удаление документа убирает и его файлы из bucket-а.

## Модуль `ml_mock`

`backend/app/ml_mock.py` - отдельное FastAPI-приложение mock extraction service.

Endpoint:

- `GET /health`;
- `POST /extract`.

Сервис принимает fragments и возвращает extraction candidates в том же формате, который backend ожидает от реального сервиса. В docker-compose он поднимается как отдельный service `ml-mock`.

## Сервис `ml_extraction`

Реальный сервис извлечения на Yandex AI Studio (подключается через `docker-compose.override.yml`). Endpoint-ы:

- `POST /extract` - извлечение кандидатов; промпт собирается из `ontology.yaml` и `synonyms.csv` при старте; лёгкий текст без чисел идёт по укороченному промпту (пред-фильтр-маршрутизатор). Фатальный отказ LLM-провайдера (401/403, исчерпанные ретраи 429, недоступность) отвечает HTTP 502 с `{"kind", "message"}` - backend помечает документ `failed`. Локальные проблемы одного фрагмента (кривой JSON) пропускают только этот фрагмент с warning-логом.
- `POST /embed` - локальные эмбеддинги `BAAI/bge-m3` (1024 измерения, веса кэшируются в volume).
- `POST /chat_json` - общая точка доступа к LLM для query-слоя backend; каскад Яндекс→запасной OpenAI-совместимый сервер с суммарным бюджетом `CHAT_DEADLINE` (основной модели ~60% бюджета, фолбэку остаток). Ключ Яндекса на сторонний `FALLBACK_BASE_URL` не отправляется - используется отдельный `FALLBACK_API_KEY`.
- `POST /web_answer` - ответ из внешних источников (веб-поиск через `ddgs` по разрешённым доменам); при отказе обеих LLM возвращаются сырые выдержки со ссылками. В граф ничего не пишет.
- `GET /health`.

Подробности - в `ml_extraction/README.md`.

## Модуль `streamlit_app`

Streamlit UI предназначен для демонстрации полного сценария без знания графовых БД. Вкладки: «Запрос», «Документы», «Review», «Факты», «Граф».

UI содержит:

- индикатор состояния backend, PostgreSQL, Neo4j, MinIO и extraction service;
- загрузку одного или нескольких файлов с поллингом `GET /jobs/{job_id}` (шаг 4 с, до 15 минут; дальше - ручная кнопка «Обновить статус»);
- поле вопроса с шаблонами, переключатель `include_hypotheses`, slider `confidence_min`;
- блок ответа, таблицу фактов/экспериментов, блоки «Противоречия», «Пробелы», «Гипотезы», смежные факты и результаты семантического поиска;
- раскрывающиеся источники с адресами фрагментов (страница/слайд/блок);
- удаление документа с подтверждением;
- review-очередь кандидатов с кнопками Approve/Reject;
- таблицы документов и facts (с пометкой спорных);
- граф через `st.graphviz_chart` и общий graph payload.

## Domain Package

`domain/default` хранит заменяемую доменную конфигурацию. Каталог монтируется read-only в контейнеры backend и ml-extraction: правки подхватываются рестартом без пересборки.

Фактически читаются кодом:

- `ontology.yaml` - типы сущностей и отношений; backend отдаёт его в `/ontology`, ml-extraction собирает из него промпт извлечения;
- `synonyms.csv` - словарь алиасов RU/EN; используется normalizer-ом backend для canonical entity names и ml-extraction в промпте;
- `validation-rules.yaml` - пороги кандидатов (`candidate_thresholds`) и именованные диапазоны правдоподобия чисел (`params`).

Справочные (кодом не читаются, фиксируют контракт и планы):

- `extraction-schema.json` - JSON schema результата извлечения;
- `units.yaml` - канонические единицы измерения (рабочая таблица единиц живёт в `backend/app/pipeline/validation.py`);
- `ranking.yaml` - baseline ranking weights;
- `query-templates.yaml` - шаблоны контрольных вопросов;
- `prompts/extraction.ru.md` - prompt contract (боевые шаблоны промптов - в `ml_extraction/app/prompts/`).

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

Сейчас создаётся ровно одна версия на документ (`version_number` всегда 1): повторная загрузка того же файла возвращает существующий документ, а механизм новых версий не реализован.

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
- `conflicts_with` - ссылки на противоречащие факты;
- `source`.

Правило: final fact всегда должен иметь source с `document_id`, `version_id`, `fragment_id`.

### `fragment_vectors`

Хранит embedding vector для source fragment:

- `fragment_id`;
- `embedding_model`;
- `embedding vector(64)` - размерность дефолтного hash-эмбеддера; при другой модели `ensure_schema` приводит колонку к `EMBEDDING_DIM` на старте backend;
- `vector_metadata`.

### `ontology_versions`

Хранит версии онтологии и решения по кандидатам расширения (строки с `version='ontology-candidate'`):

- `id`;
- `version`;
- `status`;
- `config`.

### `jobs`

Статусы фоновых задач ingest/reprocess:

- `id`;
- `job_type`;
- `status`;
- `payload`;
- `error`.

Источник статусов для API - реестр очереди в памяти; таблица - персистентная копия для задач прошлых запусков.

## Конфигурация

Основные параметры задаются через environment variables в `docker-compose.yml`, `docker-compose.override.yml` и `.env.example`.

### PostgreSQL

```text
DATABASE_URL=postgresql+psycopg://graphrag:graphrag@postgres:5432/graphrag
```

Без переменной backend работает в in-memory режиме (без персистентности).

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
EXTRACTION_TIMEOUT=8          # секунды; override ставит 600 для реального LLM
EXTRACTION_BATCH_SIZE=8       # фрагментов в одном запросе /extract
```

Если `EXTRACTION_SERVICE_URL` не задан, backend использует локальный `MockLLMProvider`.

### Embeddings

```text
EMBEDDING_DIM=64              # базовый compose: deterministic-hash
EMBEDDINGS_URL=               # override: http://ml-extraction:8002/embed (bge-m3)
EMBEDDING_TIMEOUT=120         # override ставит 300 (CPU-обсчёт пачки)
```

`EMBEDDING_DIM` задаёт размерность колонки `fragment_vectors.embedding`; override устанавливает 1024 (bge-m3). При заданном `EMBEDDINGS_URL` переменную `EMBEDDING_DIM` нужно задавать явно - она должна совпадать с моделью сервиса.

### Ingest и query

```text
INGEST_WORKERS=2              # 0 - синхронный режим без очереди
LLM_CHAT_URL=http://ml-extraction:8002/chat_json   # значение по умолчанию в коде backend
WEB_ANSWER_URL=               # override: http://ml-extraction:8002/web_answer; пусто - ступень отключена
WEB_ANSWER_TIMEOUT=20
```

### ml-extraction (при активном override)

```text
YANDEX_API_KEY=               # обязателен
YANDEX_FOLDER_ID=
YANDEX_BASE_URL=https://llm.api.cloud.yandex.net/v1
YANDEX_MODEL=qwen3.6-35b-a3b/latest
FALLBACK_BASE_URL=            # запасной OpenAI-совместимый сервер (например, Ollama); пусто - ступень отключена
FALLBACK_MODEL=minimax-m3:cloud
FALLBACK_API_KEY=             # ключ запасного сервера; ключ Яндекса на него не отправляется
CHAT_DEADLINE=100             # суммарный бюджет одного /chat_json, меньше 120 с таймаута моста
LLM_CONCURRENCY=4
LLM_RETRIES=6
WEB_SEARCH_TIMEOUT=8
WEB_LLM_TIMEOUT=6
DOMAIN_DIR=/domain/default
```

### Streamlit

```text
BACKEND_URL=http://backend:8000
```

## Важные проектные решения

### Mock-first, но с реальными контрактами

Базовый стек работает без API-ключей, GPU и скачивания моделей: mock-извлечение и hash-эмбеддинги. При этом контракты (`/extract`, `/embed`, `/chat_json`) уже финальные - override подключает реальные ML-сервисы без изменения API и UI. Оговорка: query-слой (разбор вопроса и генерация ответа) требует LLM через `/chat_json`, которого в базовом compose нет, поэтому без override `/ask` возвращает деградированный ответ с `llm_error` и результатами семантического поиска.

### LLM создает кандидатов, не факты

Извлечение возвращает `ExtractionCandidate`. Final `Fact` создается только после validation/approve. Это защищает граф от непроверенных данных.

### Source required

Факт не может быть утвержден без ссылки на source fragment. Это зафиксировано в `SourceRequiredError`; кандидат без source, даже пришедший со статусом approved через `/candidates`, возвращается в review.

### Ошибки видимы, а не маскируются

`RemoteExtractionProvider` останавливает ingest с явной ошибкой, если сервис извлечения недоступен; фатальный отказ LLM-провайдера в `/extract` дает 502 и статус `failed` у документа. Ошибки записи PG/Neo4j/MinIO пробрасываются и видны в `last_error` внутри `/health`. Отказ LLM-каскада в query-слое возвращается пользователю в `llm_error`, а ответ собирается из доступного без модели.

### Оригиналы файлов хранятся в MinIO

Compose не монтирует локальную папку с материалами. Это намеренно: локальные материалы не должны быть скрытой зависимостью инфраструктуры. Документы попадают в систему через upload/API и дальше живут в MinIO.

### Idempotent upload по checksum

Повторная загрузка того же файла возвращает существующий документ. Проверка работает и после рестарта backend-а, потому что есть lookup по PostgreSQL, и при двух конкурентных загрузках - за счет UNIQUE(checksum).

### Neo4j как projection store

PostgreSQL остается источником фактов, источников и статусов. Neo4j используется для graph projection и навигации по связям; constraints применяет сам backend идемпотентно.

### pgvector baseline с заменяемой моделью

Векторы пишутся в PostgreSQL через pgvector. Модель эмбеддингов фиксируется конфигурацией (`EMBEDDINGS_URL` + `EMBEDDING_DIM`); при смене размерности `ensure_schema` пересоздает колонку, несовместимые векторы вычищаются и переиндексируются в фоне. Векторы разных моделей не смешиваются.

### Доменная конфигурация без пересборки

`domain/` монтируется read-only в backend и ml-extraction: словарь синонимов, онтологию и правила валидации правит инженер знаний, изменения подхватываются рестартом контейнеров.

### Streamlit вместо React

Основной интерфейс сделан на Streamlit: он быстро показывает полный сценарий без отдельной frontend-сборки.

## Тесты

Backend tests находятся в `backend/tests/`.

Проверяется:

- извлечение чисел и диапазонов, регистрозависимые единицы, конверсии минут/секунд и т/сут, сверка в совместимых размерностях (`test_number_validation.py`);
- кодировки TXT/CSV, дублирующиеся CSV-заголовки, сквозная нумерация блоков DOCX (`test_parsers.py`; DOCX-часть требует python-docx и пропускается без него);
- quality gates для грязных фактов, валидация именованных параметров в единицах правил, нормализация направлений эффекта при поиске противоречий (`test_quality_gates.py`);
- query contract: прямые факты, перенос смежных фактов в related-блок, indirect search (`test_query_contract.py`);
- web answer contract при отсутствии прямых фактов (`test_web_answer.py`).

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

3. Проверить индикаторы состояния сервисов в UI (или `GET /health`).

4. Загрузить свой PDF/DOCX/XLSX/TXT через панель «Загрузка документов» и дождаться статуса `completed` (статус обновляется автоматически, есть кнопка «Обновить статус»). В mock-режиме кандидаты извлекаются только из CSV/XLSX-таблиц со знакомыми колонками и текста про «Сплав X»; произвольные документы обрабатывает реальный `ml-extraction` при активном override.

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

Связный summary и разбор вопроса требуют LLM, то есть активного override; в mock-режиме UI покажет причину в `llm_error`, семантический поиск и граф.

Для демонстрации без своих документов можно посеять синтетические данные: `docker compose exec backend python scripts/seed_sample_data.py`.

## План развития

Текущая версия уже поднимает полный локальный контур и показывает сквозной сценарий. При активном `docker-compose.override.yml` backend использует реальный extraction service, bge-m3 embeddings и веб-поиск через ML-сервис. Для перехода к более сильной версии нужно усилить несколько частей:

- прогнать извлечение по реальному корпусу документов;
- расширить query planner для сложных многофакторных вопросов;
- задействовать фильтры по годам публикации и географии, которые планировщик уже распознает;
- реализовать версионирование документов (сейчас `version_number` всегда 1);
- финализировать онтологию, aliases RU/EN и validation ranges;
- подготовить mini-golden set для проверки качества извлечения;
- вручную проверить candidates, дубли и конфликтующие факты перед показом;
- добавить справочники экспертов, лабораторий, географии, годов публикации и тегов.

## Ограничения текущей версии

- Базовый `docker-compose.yml` остаётся mock-first для автономного локального запуска; реальный ML-контур подключается через `docker-compose.override.yml`.
- Query planner покрывает базовые вопросы и контрольный вопрос, но не все сложные multiparameter queries; распознанные `region` и `year_from` в фильтрации фактов пока не применяются.
- `domain/default/units.yaml`, `ranking.yaml`, `query-templates.yaml`, `extraction-schema.json` и `prompts/extraction.ru.md` кодом не читаются - это справочные документы контракта.
- Версионирование документов не реализовано: `version_number` документа всегда 1, повторная загрузка того же файла возвращает существующий документ.
- OCR-слоя нет: страницы сканов (короткий текстовый слой или растр на большей части страницы) рендерятся в PNG и уходят на vision-обработку в реальный extraction service; `ml-mock` изображения игнорирует.
- `/search` и семантический поиск требуют PostgreSQL (pgvector).
- Полноценная ролевая модель доступа и аудит действий не реализованы.
- Масштабирование ограничено: рабочее состояние (документы, кандидаты, факты) живёт в памяти одного процесса backend, PostgreSQL служит персистентной копией для гидрации при старте.
- Ingest выполняется через очередь воркеров; синхронный режим доступен через `INGEST_WORKERS=0`.
- CSS интерфейса свёрстан под светлую тему; тёмная тема Streamlit зафиксирована как отключённая.

## Полезные файлы

- `docker-compose.yml` - весь локальный стек (mock-first).
- `docker-compose.override.yml` - реальный ML-контур на Yandex AI Studio.
- `backend/migrations/001_init.sql` - схема PostgreSQL.
- `backend/app/main.py` - FastAPI endpoints.
- `backend/app/storage.py` - основной ingest pipeline.
- `backend/app/jobs.py` - очередь фоновой обработки.
- `backend/app/persistence.py` - PostgreSQL/Neo4j adapters.
- `backend/app/file_storage.py` - MinIO adapter.
- `backend/app/pipeline/validation.py` - числовая валидация и таблица единиц.
- `backend/app/ml_mock.py` - mock extraction service.
- `ml_extraction/README.md` - сервис извлечения на Yandex AI Studio.
- `streamlit_app/app.py` - UI.
- `domain/default/ontology.yaml` - онтология.
- `domain/default/validation-rules.yaml` - пороги и диапазоны числовой валидации.
- `backend/scripts/seed_sample_data.py` - синтетические данные для smoke-проверки без своих документов.
