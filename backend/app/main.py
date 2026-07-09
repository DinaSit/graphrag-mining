from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

try:
    from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import RedirectResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.concurrency import run_in_threadpool
except ImportError as exc:  # pragma: no cover - helps local smoke tests without installed deps
    raise RuntimeError("Install backend dependencies from backend/requirements.txt to run the API.") from exc

from app.file_storage import MinioFileStorage
from app.jobs import IngestQueue
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.query import QueryOrchestrator
from app.pipeline.llm_bridge import LLM_CHAT_URL, LLMUnavailableError, SummaryStreamExtractor, chat_stream
from app.schemas import (
    CandidateStatus,
    DocumentVisibilityRequest,
    GraphPayload,
    QueryRequest,
    QueryResponse,
    RejectFactRequest,
)
from app.storage import ApplicationStore, SourceRequiredError

log = logging.getLogger(__name__)


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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# UI — один статический файл backend/ui/index.html (контракт К1); каталог
# создаётся параллельно другим агентом, его отсутствие не роняет API
UI_DIR = ROOT_DIR / "backend" / "ui"
if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
else:
    log.warning("UI не смонтирован: каталог %s не найден", UI_DIR)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


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


class WebAnswerProxyRequest(BaseModel):
    question: str


def _web_answer_failure(reason: str) -> dict:
    """Единый формат отказа /web/answer: та же схема, что успешный ответ."""
    return {"found": False, "answer": None, "url": None, "snippets": [], "llm_error": reason}


@app.post("/web/answer")
async def web_answer(request: WebAnswerProxyRequest) -> dict:
    """Независимый контур веб-поиска (контракт К1): выполняется ВСЕГДА по
    явному запросу UI (решение пользователя), а не только когда база не
    ответила. Прокси в ml-extraction /web_answer; схема ответа 1:1 повторяет
    WebAnswerResponse сервиса: {"found", "answer", "url", "snippets", "llm_error"}."""
    web_answer_url = os.getenv("WEB_ANSWER_URL")
    if not web_answer_url:
        return _web_answer_failure("веб-поиск не настроен (WEB_ANSWER_URL не задан)")
    # Запас поверх WEB_ANSWER_TIMEOUT: ml-extraction должен успеть сам оборвать
    # поиск/LLM по своему таймауту и вернуть структурированную ошибку
    timeout = float(os.getenv("WEB_ANSWER_TIMEOUT", "20")) + 5.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            raw = await client.post(web_answer_url, json={"question": request.question})
        raw.raise_for_status()
        payload = raw.json()
        if not isinstance(payload, dict):
            raise ValueError("тело ответа не JSON-объект")
    # Сетевые ошибки и не-JSON тело не превращаются в 500: UI получает
    # человекочитаемую причину в llm_error
    except httpx.TimeoutException:
        return _web_answer_failure(f"веб-поиск не уложился в таймаут {timeout:.0f} с")
    except (httpx.HTTPError, ValueError) as exc:
        return _web_answer_failure(f"веб-поиск недоступен: {exc.__class__.__name__}")
    return {
        "found": bool(payload.get("found")),
        "answer": payload.get("answer"),
        "url": payload.get("url"),
        "snippets": payload.get("snippets") or [],
        "llm_error": payload.get("llm_error"),
    }


@app.post("/ask")
async def ask(request: QueryRequest):
    # Смолток и оффтоп («как дела?») отвечаются мгновенно, без LLM,
    # графа и эмбеддингов
    if orchestrator.is_offtopic(request.question):
        return orchestrator.offtopic_response()
    return await orchestrator.answer(request)


def _sse_event(name: str, payload: dict) -> str:
    """Формат события контракта К1: "event: <имя>\\ndata: <json одной строкой>\\n\\n"."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _parse_llm_json(text: str) -> dict:
    """Полный текст стрима -> dict как из /chat_json: модель может обернуть
    JSON в код-блок или добавить хвост вокруг объекта."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("в ответе модели нет JSON-объекта")
        parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("ответ модели не JSON-объект")
    return parsed


async def _ask_stream_events(request: QueryRequest):
    """SSE-поток контракта К1: "evidence" -> "delta"... -> "final" (всегда последним)."""
    # Оффтоп: сразу единственное событие final
    if orchestrator.is_offtopic(request.question):
        yield _sse_event("final", orchestrator.offtopic_response().model_dump(mode="json"))
        return

    try:
        evidence = await orchestrator._collect_evidence(request)
        yield _sse_event("evidence", orchestrator.evidence_preview(evidence))

        # Дельты наружу — только текст summary: автомат вытаскивает значение
        # первого ключа из сырого JSON-потока модели
        extractor = SummaryStreamExtractor()
        full_text = ""
        stream_error: LLMUnavailableError | None = None
        async for event in chat_stream(orchestrator._answer_messages(request.question, evidence.evidence_pack)):
            if event.get("done"):
                full_text = event.get("text") or ""
                stream_error = event.get("error")
                break
            piece = extractor.feed(event.get("delta") or "")
            if piece:
                yield _sse_event("delta", {"text": piece})

        llm_answer: dict = {}
        if stream_error is not None:
            # Сбой стрима (в т.ч. после первого токена): существующая
            # деградация — summary без LLM + llm_error в финальном ответе
            evidence.llm_errors.append(stream_error.human())
        else:
            try:
                llm_answer = _parse_llm_json(full_text)
            except ValueError as exc:
                evidence.llm_errors.append(LLMUnavailableError("bad_response", str(exc)).human())

        response = orchestrator._finalize(evidence, llm_answer)
        yield _sse_event("final", response.model_dump(mode="json"))
    except Exception as exc:  # финальное событие обязано прийти даже при сбое пайплайна
        fallback = QueryResponse(
            summary=f"Ответ не собран: внутренняя ошибка стриминга ({exc.__class__.__name__}).",
            experiments=[],
            sources=[],
            graph=GraphPayload(),
            contradictions=[],
            gaps=[],
            confidence=0.0,
            llm_error=str(exc) or exc.__class__.__name__,
            evidence_status="none",
        )
        yield _sse_event("final", fallback.model_dump(mode="json"))


@app.post("/ask/stream")
async def ask_stream(request: QueryRequest):
    """Стриминговый вариант /ask (контракт К1): тот же оффтоп-роутер,
    но summary отдаётся кусками по мере генерации."""
    return StreamingResponse(_ask_stream_events(request), media_type="text/event-stream")


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


@app.get("/facts/random")
def random_fact():
    """Один случайный approved-факт из нескрытого документа (фича UI)."""
    fact = store.random_visible_fact()
    if fact is None:
        raise HTTPException(status_code=404, detail="Нет ни одного подходящего факта")
    return {"fact": fact.model_dump(mode="json")}


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


@app.get("/documents/{document_id}/original")
def get_document_original(document_id: str):
    """Оригинальный файл из MinIO: PDF отдаётся inline (браузер рендерит),
    остальные форматы — attachment с исходным именем файла."""
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if not store.file_storage or not store.file_storage.enabled:
        raise HTTPException(status_code=404, detail="Файловое хранилище недоступно, оригинал не получить")
    try:
        stored = store.file_storage.get_document(document_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Оригинал не получен из MinIO: {exc}")
    if stored is None:
        raise HTTPException(status_code=404, detail="Файл документа не найден в хранилище")
    content, _ = stored
    filename = document.filename or "document.bin"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    is_pdf = filename.lower().endswith(".pdf")
    if is_pdf:
        media_type = "application/pdf"
    disposition = "inline" if is_pdf else "attachment"
    # RFC 5987: кириллическое имя — в filename*, ASCII-фолбэк — в filename.
    # Имя файла не должно ломать заголовок: управляющие символы и '/'
    # выбрасываются, кавычки и бэкслеши в quoted-string экранируются,
    # в filename* quote(..., safe="") кодирует всё вне attr-char
    clean_name = "".join(ch for ch in filename if ch.isprintable() and ch != "/") or "document"
    ascii_name = clean_name.encode("ascii", "ignore").decode() or "document"
    ascii_name = ascii_name.replace("\\", "\\\\").replace('"', '\\"')
    header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean_name, safe='')}"
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": header})


@app.get("/documents/{document_id}/preview")
def get_document_preview(document_id: str):
    """PDF-превью DOCX/PPTX из MinIO (строится LibreOffice при инжесте/бэкфиле):
    отдаётся inline, браузер рендерит как обычный PDF. 404 — превью нет/недоступно."""
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if not document.preview_object or not store.file_storage or not store.file_storage.enabled:
        raise HTTPException(status_code=404, detail="PDF-превью для документа нет")
    content = store.file_storage.get_object(document.preview_object)
    if content is None:
        raise HTTPException(status_code=404, detail="PDF-превью не найдено в хранилище")
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="preview.pdf"'},
    )


@app.post("/documents/{document_id}/visibility")
def set_document_visibility(document_id: str, request: DocumentVisibilityRequest):
    """Скрывает/показывает документ во всех ответах. Данные не удаляются,
    Neo4j не перестраивается — фильтрация выполняется на стороне backend."""
    try:
        return store.set_document_visibility(document_id, request.hidden)
    except KeyError:
        raise HTTPException(status_code=404, detail="Document not found")


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


@app.get("/review/count")
def review_count():
    """Число кандидатов, ожидающих проверки эксперта (бейдж в UI)."""
    # list(): кандидаты добавляются из фоновых воркеров параллельно
    pending = sum(1 for candidate in list(store.candidates.values()) if candidate.status == CandidateStatus.pending_review)
    return {"pending": pending}


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
def reject_fact(candidate_id: str, body: RejectFactRequest | None = Body(default=None)):
    if candidate_id not in store.candidates:
        raise HTTPException(status_code=404, detail="Candidate not found")
    # Тело необязательно: старые клиенты шлют reject без note
    return store.reject_candidate(candidate_id, note=body.note if body else None)


class BulkReviewRequest(BaseModel):
    # Лимит 1000: пачка обрабатывается синхронно в одном запросе
    candidate_ids: list[str] = Field(max_length=1000)
    action: Literal["approve", "reject"]
    note: str | None = None


@app.post("/review/facts/bulk")
def bulk_review_facts(body: BulkReviewRequest):
    """Пакетная проверка кандидатов: ошибка по одному id не прерывает пачку."""
    processed = 0
    failed: list[dict[str, str]] = []
    for candidate_id in body.candidate_ids:
        candidate = store.candidates.get(candidate_id)
        if candidate is None:
            failed.append({"id": candidate_id, "error": "Кандидат не найден"})
            continue
        if candidate.status != CandidateStatus.pending_review:
            failed.append({
                "id": candidate_id,
                "error": f"Кандидат уже обработан (статус {candidate.status.value})",
            })
            continue
        try:
            if body.action == "approve":
                store.approve_candidate(candidate_id)
            else:
                store.reject_candidate(candidate_id, note=body.note)
        except SourceRequiredError as exc:
            failed.append({"id": candidate_id, "error": str(exc)})
        except KeyError:
            failed.append({"id": candidate_id, "error": "Кандидат не найден"})
        else:
            processed += 1
    return {"processed": processed, "failed": failed}
