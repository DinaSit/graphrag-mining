"""Сервис извлечения фактов на базе Yandex AI Studio (зона ML-A).

Реализует контракт ml-mock: POST /extract {fragments} → {candidates}.
Подключается к backend через EXTRACTION_SERVICE_URL
(см. docker-compose.override.yml в корне репозитория).
"""
import logging

from fastapi import FastAPI, HTTPException

from app import config, yandex_client
from app.extractor import extract_fragments
from app.schemas import EmbedRequest, EmbedResponse, ExtractRequest, ExtractResponse
from app.yandex_client import YandexClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="ML Extraction Service (Yandex AI Studio)", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "ml-extraction",
        "mode": "yandex",
        "model": config.model_uri(config.YANDEX_MODEL),
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
    """Эмбеддинги для семантического индекса. kind: doc — фрагменты, query — запросы."""
    if not config.YANDEX_API_KEY:
        raise HTTPException(status_code=503, detail="YANDEX_API_KEY не задан — сервис не сконфигурирован")
    if request.kind not in ("doc", "query"):
        raise HTTPException(status_code=422, detail="kind должен быть 'doc' или 'query'")
    try:
        vectors = await yandex_client.embed(request.texts, kind=request.kind)
    except YandexClientError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    model = config.YANDEX_EMB_DOC_MODEL if request.kind == "doc" else config.YANDEX_EMB_QUERY_MODEL
    return EmbedResponse(
        embeddings=vectors,
        dimensions=len(vectors[0]) if vectors else 0,
        model=config.model_uri(model, scheme="emb"),
    )
