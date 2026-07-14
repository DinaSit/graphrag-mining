"""Сервис извлечения фактов на базе Yandex AI Studio (зона ML-A).

Реализует контракт ml-mock: POST /extract {fragments} → {candidates}.
Подключается к backend через EXTRACTION_SERVICE_URL
(см. docker-compose.override.yml в корне репозитория).
"""
import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from app import config, embeddings, web_search, yandex_client
from app.extractor import extract_fragments
from app.schemas import (
    EmbedRequest,
    EmbedResponse,
    ExtractRequest,
    ExtractResponse,
    WebAnswerRequest,
    WebAnswerResponse,
    WebSearchRequest,
    WebSearchResponse,
    WebSourcesResponse,
)
from app.yandex_client import YandexClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="ML Extraction Service (Yandex AI Studio)", version="0.1.0")


@app.post("/chat_json")
async def chat_json_endpoint(request: dict) -> dict:
    """Общая точка доступа к LLM для query-слоя backend (планировщик, генерация ответа).

    Принимает {"messages": [...], "model": опционально}, возвращает {"result": <JSON от модели>}.
    """
    messages = request.get("messages")
    if not messages:
        raise HTTPException(status_code=422, detail="messages обязательны")
    try:
        # CHAT_DEADLINE < 120 с (таймаут httpx в backend/llm_bridge): каскад
        # Яндекс→запасной обязан ответить до обрыва соединения вызывающей стороной
        result = await yandex_client.chat_json(
            messages, model=request.get("model"), allow_fallback=True,
            deadline=config.CHAT_DEADLINE,
        )
    except YandexClientError as e:
        # kind даёт backend-у различить причину: auth / quota / bad_response / unavailable
        raise HTTPException(status_code=502, detail={"kind": e.kind, "message": str(e)}) from e
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail={"kind": "bad_response", "message": str(e)}) from e
    return {"result": result}


@app.post("/chat_stream")
async def chat_stream_endpoint(request: dict) -> StreamingResponse:
    """Стриминговый доступ к LLM для /ask/stream backend (контракт К2).

    Запрос — как у /chat_json: {"messages": [...], "model": null|str}.
    Ответ — text/event-stream строками "data: <json>": {"delta": ...} по мере
    генерации и ровно одна терминальная {"done": true, "text", "provider",
    "error"} — всегда последняя, даже при отказе обоих провайдеров.
    """
    messages = request.get("messages")
    if not messages:
        raise HTTPException(status_code=422, detail="messages обязательны")
    model = request.get("model")

    async def event_stream():
        terminal_sent = False
        try:
            # тот же бюджет CHAT_DEADLINE, что у /chat_json: каскад обязан
            # начать отвечать до обрыва соединения вызывающей стороной
            async for event in yandex_client.chat_stream(messages, model=model, deadline=config.CHAT_DEADLINE):
                terminal_sent = terminal_sent or bool(event.get("done"))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # страховка контракта: терминальная запись обязана уйти
            log.exception("Сбой генератора /chat_stream")
            if not terminal_sent:
                # error_text: у TimeoutError/CancelledError str(e) пуст — причина не теряется
                terminal = {"done": True, "text": "", "provider": "none",
                            "error": {"kind": "unavailable", "message": yandex_client.error_text(e)[:300]}}
                yield f"data: {json.dumps(terminal, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/web_sources", response_model=WebSourcesResponse)
def web_sources() -> WebSourcesResponse:
    """Дефолтный реестр веб-источников: ddgs-домены (ALLOWED_DOMAINS) + научные
    API. Единственный источник правды — этот сервис; UI берёт реестр отсюда."""
    return WebSourcesResponse(sources=web_search.default_sources())


@app.post("/web_search", response_model=WebSearchResponse)
async def web_search_endpoint(request: WebSearchRequest) -> WebSearchResponse:
    """Чистый поиск (контракт К3): ddgs по разрешённым доменам + научные API, без LLM.

    UI напрямую не обращается к этому эндпоинту (запросы идут через /web_answer),
    но эндпоинт — часть контракта К3 с backend и диагностический вход: запрос
    curl показывает вклад каждой ветки поиска по меткам source. Не удалять."""
    result = await web_search.search_results(request.question)
    return WebSearchResponse(**result)


@app.post("/web_answer", response_model=WebAnswerResponse)
async def web_answer(request: WebAnswerRequest) -> WebAnswerResponse:
    """Ответ из внешних источников (разрешённый список доменов). В граф не пишет.

    Этап поиска LLM не требует; связная формулировка — через каскад LLM,
    при отказе обеих моделей возвращаются сырые выдержки со ссылками.
    Если в запросе передана готовая выдача results (К3) — поиск пропускается.
    sources — реестр источников из UI (см. WebAnswerRequest), None = значения по умолчанию.
    """
    result = await web_search.answer_from_web(
        request.question, results=request.results, sources=request.sources)
    return WebAnswerResponse(**result)


@app.get("/health")
def health() -> dict:
    query_model = yandex_client.query_model_status()
    return {
        "status": "ok",
        "service": "ml-extraction",
        "mode": "yandex",
        "model": config.model_uri(config.YANDEX_MODEL),
        "fallback_model": config.FALLBACK_MODEL if config.FALLBACK_BASE_URL else None,
        "configured": bool(config.YANDEX_API_KEY),
        "query_llm_status": query_model.get("status"),
        "query_llm_provider": query_model.get("provider"),
        "query_llm_model": query_model.get("model"),
        "query_llm_error_kind": query_model.get("error_kind"),
        "query_llm_updated_at": query_model.get("updated_at"),
    }


@app.post("/extract", response_model=ExtractResponse)
async def extract(request: ExtractRequest) -> ExtractResponse:
    if not config.YANDEX_API_KEY:
        raise HTTPException(status_code=503, detail="YANDEX_API_KEY не задан — сервис не сконфигурирован")
    try:
        candidates = await extract_fragments(request.fragments)
    except YandexClientError as e:
        # Фатальный отказ провайдера (auth/quota/недоступность): backend пометит
        # документ failed вместо completed без фактов
        raise HTTPException(status_code=502, detail={"kind": e.kind, "message": str(e)}) from e
    return ExtractResponse(candidates=candidates)


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    """Эмбеддинги для семантического индекса: локальная bge-m3 (1024).

    kind принимается для совместимости контракта: у bge-m3 единый режим
    для документов и запросов.
    """
    try:
        vectors = await embeddings.embed(request.texts, kind=request.kind)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Модель эмбеддингов недоступна: {e}") from e
    return EmbedResponse(
        embeddings=vectors,
        dimensions=len(vectors[0]) if vectors else 0,
        model=embeddings.MODEL_NAME,
    )
