from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from app.pipeline.providers import MockLLMProvider
from app.schemas import ExtractionCandidate, SourceFragment


class ExtractRequest(BaseModel):
    fragments: list[SourceFragment]


class ExtractResponse(BaseModel):
    candidates: list[ExtractionCandidate]


provider = MockLLMProvider()
app = FastAPI(title="ML Mock Extraction Service", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ml-mock", "mode": "mock"}


@app.post("/extract")
def extract(request: ExtractRequest) -> ExtractResponse:
    return ExtractResponse(candidates=provider.extract_entities(request.fragments))
