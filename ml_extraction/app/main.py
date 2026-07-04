"""Сервис извлечения фактов на базе Yandex AI Studio (зона ML-A).

Реализует контракт ml-mock: POST /extract {fragments} → {candidates}.
Подключается к backend через EXTRACTION_SERVICE_URL
(см. docker-compose.override.yml в корне репозитория).
"""
import json
import logging

from fastapi import FastAPI, HTTPException

from app import config, embeddings, web_search, yandex_client
from app.extractor import extract_fragments
from app.schemas import (
    EmbedRequest,
    EmbedResponse,
    ExtractRequest,
    ExtractResponse,
    WebAnswerRequest,
    WebAnswerResponse,
)
from app.yandex_client import YandexClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

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
        result = await yandex_client.chat_json(messages, model=request.get("model"), allow_fallback=True)
    except YandexClientError as e:
        # kind даёт backend-у различить причину: auth / quota / bad_response / unavailable
        raise HTTPException(status_code=502, detail={"kind": e.kind, "message": str(e)}) from e
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail={"kind": "bad_response", "message": str(e)}) from e
    return {"result": result}


@app.post("/web_answer", response_model=WebAnswerResponse)
async def web_answer(request: WebAnswerRequest) -> WebAnswerResponse:
    """Ответ из внешних источников (разрешённый список доменов). В граф не пишет.

    Сам поиск LLM не требует; связная формулировка — через каскад LLM,
    при отказе обеих моделей возвращаются сырые выдержки со ссылками.
    """
    result = await web_search.answer_from_web(request.question)
    return WebAnswerResponse(**result)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "ml-extraction",
        "mode": "yandex",
        "model": config.model_uri(config.YANDEX_MODEL),
        "fallback_model": config.FALLBACK_MODEL if config.FALLBACK_BASE_URL else None,
        "configured": bool(config.YANDEX_API_KEY),
    }


@app.post("/extract", response_model=ExtractResponse)
async def extract(request: ExtractRequest) -> ExtractResponse:
    if not config.YANDEX_API_KEY:
        raise HTTPException(status_code=503, detail="YANDEX_API_KEY не задан — сервис не сконфигурирован")
    try:
        candidates = await extract_fragments(request.fragments)
    except YandexClientError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
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
