from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.concurrency import run_in_threadpool
except ImportError as exc:  # pragma: no cover - helps local smoke tests without installed deps
    raise RuntimeError("Install backend dependencies from backend/requirements.txt to run the API.") from exc

from app.file_storage import MinioFileStorage
from app.jobs import IngestQueue
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.query import QueryOrchestrator
from app.pipeline.llm_bridge import LLM_CHAT_URL
from app.schemas import CandidateStatus, ExtractionCandidate, QueryRequest, SearchRequest
from app.storage import ApplicationStore, SourceRequiredError


ROOT_DIR = Path(__file__).resolve().parents[2]
DOMAIN_DIR = ROOT_DIR / "domain" / "default"

store = ApplicationStore(
    DOMAIN_DIR,
    postgres_sink=PostgresSink(os.getenv("DATABASE_URL")),
    graph_sink=Neo4jSink(os.getenv("NEO4J_URI"), os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")),
    file_storage=MinioFileStorage(
        endpoint=os.getenv("MINIO_ENDPOINT"),
        access_key=os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER"),
        secret_key=os.getenv("MINIO_SECRET_KEY") or os.getenv("MINIO_ROOT_PASSWORD"),
        bucket=os.getenv("MINIO_BUCKET"),
        secure=os.getenv("MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"},
    ),
    extraction_service_url=os.getenv("EXTRACTION_SERVICE_URL"),
)
store.hydrate_from_postgres()
orchestrator = QueryOrchestrator(store)

# Фоновая обработка загрузок: 2 воркера по умолчанию (лимит LLM-вызовов
# держит сервис извлечения). INGEST_WORKERS=0 возвращает синхронный режим.
_workers = int(os.getenv("INGEST_WORKERS", "2"))
ingest_queue = IngestQueue(store, workers=_workers) if _workers > 0 else None


def _load_ontology_payload() -> dict[str, str]:
    ontology_path = DOMAIN_DIR / "ontology.yaml"
    if not ontology_path.exists():
        return {"version": "unknown", "path": str(ontology_path), "text": ""}
    text = ontology_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {"version": str(data.get("version", "unknown")), "path": str(ontology_path), "text": text}


def _llm_health() -> dict[str, str | None]:
    split = urlsplit(LLM_CHAT_URL)
    health_url = urlunsplit((split.scheme, split.netloc, "/health", "", ""))
    try:
        response = httpx.get(health_url, timeout=2)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "answer_llm_status": "unavailable",
            "answer_llm_provider": "none",
            "answer_llm_model": None,
            "answer_llm_error": exc.__class__.__name__,
        }
    return {
        "answer_llm_status": str(payload.get("query_llm_status") or ("configured" if payload.get("configured") else "unavailable")),
        "answer_llm_provider": str(payload.get("query_llm_provider") or payload.get("mode") or "unknown"),
        "answer_llm_model": payload.get("query_llm_model") or payload.get("model"),
        "answer_llm_error": payload.get("query_llm_error_kind"),
    }


app = FastAPI(
    title="Scientific Multimodal GraphRAG",
    version="0.1.0",
    description="Scientific document GraphRAG API with mock-first extraction interfaces.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str | None]:
    # last_error хранит последнюю ошибку каждого хранилища: сбой записи виден
    # в мониторинге, даже если сама операция уже отработала свой failed-путь
    payload = {
        "status": "ok",
        "mode": "mock-first",
        "postgres": "enabled" if store.postgres_sink and store.postgres_sink.enabled else "memory",
        "postgres_last_error": store.postgres_sink.last_error if store.postgres_sink else None,
        "neo4j": "enabled" if store.graph_sink and store.graph_sink.enabled else "memory",
        "neo4j_last_error": store.graph_sink.last_error if store.graph_sink else None,
        "minio": "enabled" if store.file_storage and store.file_storage.enabled else "disabled",
        "minio_last_error": store.file_storage.last_error if store.file_storage else None,
        "extraction": "remote" if os.getenv("EXTRACTION_SERVICE_URL") else "local",
    }
    payload.update(_llm_health())
    return payload


# Кэш готовых ответов /ask: повторный и совпадающий вопрос не тратит LLM-вызовы.
# Ключ — весь запрос, валидность — версия данных store + TTL (веб-ступень стареет)
_ANSWER_CACHE: OrderedDict[str, tuple[int, float, dict]] = OrderedDict()
_ANSWER_CACHE_LOCK = threading.Lock()
_ANSWER_CACHE_MAX = 256
_ANSWER_CACHE_TTL = float(os.getenv("ANSWER_CACHE_TTL", "1800"))


def _answer_cache_key(request: QueryRequest) -> str:
    payload = request.model_dump()
    payload["question"] = request.question.strip().casefold().replace("ё", "е")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _answer_cache_get(key: str) -> dict | None:
    with _ANSWER_CACHE_LOCK:
        entry = _ANSWER_CACHE.get(key)
        if entry is None:
            return None
        version, stamp, payload = entry
        if version != store.data_version or time.monotonic() - stamp > _ANSWER_CACHE_TTL:
            _ANSWER_CACHE.pop(key, None)
            return None
        _ANSWER_CACHE.move_to_end(key)
        return payload


def _answer_cache_put(key: str, payload: dict) -> None:
    with _ANSWER_CACHE_LOCK:
        _ANSWER_CACHE[key] = (store.data_version, time.monotonic(), payload)
        _ANSWER_CACHE.move_to_end(key)
        if len(_ANSWER_CACHE) > _ANSWER_CACHE_MAX:
            _ANSWER_CACHE.popitem(last=False)


@app.post("/ask")
async def ask(request: QueryRequest):
    # Смолток и оффтоп («как дела?») отвечаются мгновенно, без LLM,
    # графа, эмбеддингов и веб-поиска
    if orchestrator.is_offtopic(request.question):
        return orchestrator.offtopic_response()

    cache_key = _answer_cache_key(request)
    cached = _answer_cache_get(cache_key)
    if cached is not None:
        return cached

    response = await orchestrator.answer(request)
    web_answer_url = os.getenv("WEB_ANSWER_URL")
    if web_answer_url and not response.has_direct_facts and not response.offtopic:
        try:
            timeout = float(os.getenv("WEB_ANSWER_TIMEOUT", "20"))
            async with httpx.AsyncClient(timeout=timeout) as client:
                raw = await client.post(web_answer_url, json={"question": request.question})
            raw.raise_for_status()
            web = raw.json()
            if web.get("found"):
                response.web_answer = {
                    "answer": web.get("answer"),
                    "url": web.get("url"),
                    "snippets": web.get("snippets") or [],
                    "llm_error": web.get("llm_error"),
                }
        # Веб-ступень факультативна: любой её сбой (таймаут, 5xx, не-JSON тело)
        # деградирует в обычный ответ из графа, а не в 500
        except httpx.TimeoutException:
            response.web_answer = {
                "answer": None,
                "url": None,
                "snippets": [],
                "llm_error": f"веб-поиск не уложился в таймаут {timeout:.0f} с",
            }
        except (httpx.HTTPError, ValueError) as exc:
            response.web_answer = {
                "answer": None,
                "url": None,
                "snippets": [],
                "llm_error": f"веб-поиск недоступен: {exc.__class__.__name__}",
            }
    # Кэшируются только полноценные ответы: оффтоп и так мгновенный,
    # а ответ с llm_error — деградация, которую не стоит закреплять
    if not response.offtopic and response.llm_error is None:
        _answer_cache_put(cache_key, response.model_dump(mode="json"))
    return response


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    document_type: str | None = Form(default=None),
    source_label: str | None = Form(default=None),
    access_level: str = Form(default="uploaded"),
):
    content = await file.read()
    filename = file.filename or "uploaded.txt"
    # Дубль определяется сразу, без постановки в очередь; блокирующие вызовы
    # (PG, LLM-извлечение) уводятся из event loop в поток
    existing = await run_in_threadpool(store.find_document_by_checksum, hashlib.sha256(content).hexdigest())
    if existing:
        return {"document": existing, "status": existing.status, "evidence_units": existing.element_count}
    if ingest_queue is None:  # синхронный режим (INGEST_WORKERS=0)
        document = await run_in_threadpool(
            store.ingest_document, filename, content, document_type, source_label, access_level
        )
        return {"document": document, "status": document.status, "evidence_units": document.element_count}
    job_id = ingest_queue.enqueue_ingest(filename, content, document_type, source_label, access_level)
    return {"job_id": job_id, "filename": filename, "status": "queued"}


@app.post("/candidates")
def add_candidates(candidates: list[ExtractionCandidate]):
    """Приём готовых кандидатов извне (бэкфилл, ручная разметка).

    Кандидаты проходят штатный путь: валидация чисел по правилам,
    пороги утверждения, фиксация противоречий, проекция в граф.
    """
    accepted = [store.add_candidate(candidate).id for candidate in candidates]
    return {"accepted": len(accepted)}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    """Статус фоновой задачи: {"job_id", "status": queued|processing|completed|failed, "error"}."""
    # Источник — реестр очереди в памяти (работает без PostgreSQL);
    # PG — персистентная копия для задач из прошлых запусков backend-а
    job = ingest_queue.get_job(job_id) if ingest_queue else None
    if job is None and store.postgres_sink:
        row = store.postgres_sink.get_job(job_id)
        if row:
            job = {"job_id": row["job_id"], "status": row["status"], "error": row["error"]}
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/graph")
def graph():
    return store.persistent_graph()


@app.get("/facts")
def facts():
    return {"facts": store.persistent_facts()}


@app.get("/documents")
def list_documents():
    return list(store.documents.values())


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    # list(): фрагменты добавляются из фоновых воркеров параллельно
    fragments = [fragment for fragment in list(store.fragments.values()) if fragment.document_id == document_id]
    return {"document": document, "fragments": fragments}


@app.get("/documents/{document_id}/status")
def get_document_status(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document.id, "status": document.status, "element_count": document.element_count}


@app.delete("/documents/{document_id}")
def delete_document(document_id: str):
    """Удаляет документ и всё извлечённое из него (фрагменты, факты, узлы графа)."""
    try:
        return store.delete_document(document_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Document not found")
    except Exception as exc:
        # Каскад прошёл не до конца (PG/Neo4j/MinIO): клиент должен знать,
        # что удаление нужно повторить
        raise HTTPException(status_code=500, detail=f"Удаление не завершено: {exc}")


@app.post("/documents/{document_id}/reprocess")
def reprocess_document(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if ingest_queue is None:  # синхронный режим (INGEST_WORKERS=0)
        candidates = store.reprocess_document(document_id)
        return {"document_id": document_id, "status": "completed", "candidates": candidates}
    job_id = ingest_queue.enqueue_reprocess(document_id)
    return {"document_id": document_id, "job_id": job_id, "status": "queued"}


@app.post("/search")
def search(request: SearchRequest):
    return orchestrator.search(request.query, top_k=request.top_k)


@app.post("/query")
async def query(request: QueryRequest):
    """Полный алиас /ask: та же логика, включая ступень веб-поиска."""
    return await ask(request)


@app.get("/entities/{entity_id}")
def get_entity(entity_id: str):
    # list(): факты добавляются из фоновых воркеров параллельно
    facts = [
        fact
        for fact in list(store.facts.values())
        if entity_id in {fact.material_id, fact.experiment_id, fact.id, fact.source.fragment_id}
    ]
    if not facts:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"entity_id": entity_id, "facts": facts}


@app.get("/entities/{entity_id}/graph")
def get_entity_graph(entity_id: str, depth: int = 2):
    # Глубина обхода реализуется графовой базой; без Neo4j отдаётся
    # in-memory окрестность фактов (глубина фиксированная)
    if store.graph_sink and store.graph_sink.enabled:
        graph = store.graph_sink.get_entity_graph(entity_id, depth)
        if graph.nodes:
            return graph
    return store.get_graph(entity_id=entity_id)


@app.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str):
    facts = [fact for fact in list(store.facts.values()) if fact.experiment_id == experiment_id]
    if not facts:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return {"experiment_id": experiment_id, "facts": facts, "graph": store.get_graph(facts=facts)}


@app.get("/ontology")
def get_ontology():
    return _load_ontology_payload()


@app.get("/ontology/versions")
def get_ontology_versions():
    ontology = _load_ontology_payload()
    return [{"version": ontology["version"], "status": "active", "source": "domain/default/ontology.yaml"}]


@app.get("/ontology/candidates")
def get_ontology_candidates():
    return list(store.ontology_candidates.values())


@app.post("/ontology/candidates/{candidate_id}/approve")
def approve_ontology_candidate(candidate_id: str):
    candidate = store.ontology_candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Ontology candidate not found")
    candidate.status = CandidateStatus.approved
    store.persist_ontology_candidate(candidate)
    return candidate


@app.post("/ontology/candidates/{candidate_id}/reject")
def reject_ontology_candidate(candidate_id: str):
    candidate = store.ontology_candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Ontology candidate not found")
    candidate.status = CandidateStatus.rejected
    store.persist_ontology_candidate(candidate)
    return candidate


@app.post("/ontology/candidates/{candidate_id}/merge")
def merge_ontology_candidate(candidate_id: str, target_type: str):
    candidate = store.ontology_candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Ontology candidate not found")
    candidate.status = CandidateStatus.approved
    candidate.similar_existing_types.append(target_type)
    store.persist_ontology_candidate(candidate)
    return candidate


@app.get("/review/facts")
def review_facts(status: CandidateStatus | None = None):
    candidates = list(store.candidates.values())
    if status:
        candidates = [candidate for candidate in candidates if candidate.status == status]
    return candidates


@app.post("/review/facts/{candidate_id}/approve")
def approve_fact(candidate_id: str):
    if candidate_id not in store.candidates:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        return store.approve_candidate(candidate_id)
    except SourceRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/review/facts/{candidate_id}/reject")
def reject_fact(candidate_id: str):
    if candidate_id not in store.candidates:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return store.reject_candidate(candidate_id)
