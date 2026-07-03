"""Сервис извлечения фактов на базе Yandex AI Studio (зона ML-A).

Реализует контракт ml-mock: POST /extract {fragments} → {candidates}.
Подключается к backend через EXTRACTION_SERVICE_URL
(см. docker-compose.override.yml в корне репозитория).
"""
import logging

from fastapi import FastAPI, HTTPException

from app import config
from app.extractor import extract_fragments
from app.schemas import ExtractRequest, ExtractResponse
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
