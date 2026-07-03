from __future__ import annotations

import os
from pathlib import Path

import yaml

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # pragma: no cover - helps local smoke tests without installed deps
    raise RuntimeError("Install backend dependencies from backend/requirements.txt to run the API.") from exc

from app.file_storage import MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.pipeline.sample_data import seed_sample_data
from app.pipeline.query import QueryOrchestrator
from app.schemas import CandidateStatus, QueryRequest, SearchRequest
from app.storage import ApplicationStore


ROOT_DIR = Path(__file__).resolve().parents[2]
DOMAIN_DIR = ROOT_DIR / "domain" / "default"
SAMPLE_PDF_DIR = ROOT_DIR / "sample_pdfs"

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
seed_sample_data(store, SAMPLE_PDF_DIR if SAMPLE_PDF_DIR.exists() else ROOT_DIR)
orchestrator = QueryOrchestrator(store)


def _load_ontology_payload() -> dict[str, str]:
    ontology_path = DOMAIN_DIR / "ontology.yaml"
    if not ontology_path.exists():
        return {"version": "unknown", "path": str(ontology_path), "text": ""}
    text = ontology_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {"version": str(data.get("version", "unknown")), "path": str(ontology_path), "text": text}


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
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "mode": "mock-first",
        "postgres": "enabled" if store.postgres_sink and store.postgres_sink.enabled else "memory",
        "neo4j": "enabled" if store.graph_sink and store.graph_sink.enabled else "memory",
        "minio": "enabled" if store.file_storage and store.file_storage.enabled else "disabled",
        "extraction": "remote" if os.getenv("EXTRACTION_SERVICE_URL") else "local",
    }


@app.post("/ask")
def ask(request: QueryRequest):
    return orchestrator.answer(request)


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    document_type: str | None = Form(default=None),
    source_label: str | None = Form(default=None),
    access_level: str = Form(default="uploaded"),
):
    content = await file.read()
    document = store.ingest_document(file.filename or "uploaded.txt", content, document_type, source_label, access_level)
    return {"document": document, "status": document.status, "evidence_units": document.element_count}


@app.get("/graph")
def graph():
    return store.persistent_graph()


@app.get("/facts")
def facts():
    return {"facts": store.persistent_facts()}


@app.get("/documents")
def list_documents():
    return list(store.documents.values())


@app.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    document_type: str | None = Form(default=None),
    source_label: str | None = Form(default=None),
    access_level: str = Form(default="uploaded"),
):
    content = await file.read()
    document = store.ingest_document(file.filename or "uploaded.txt", content, document_type, source_label, access_level)
    return document


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    fragments = [fragment for fragment in store.fragments.values() if fragment.document_id == document_id]
    return {"document": document, "fragments": fragments}


@app.get("/documents/{document_id}/status")
def get_document_status(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document.id, "status": document.status, "element_count": document.element_count}


@app.post("/documents/{document_id}/reprocess")
def reprocess_document(document_id: str):
    document = store.documents.get(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    fragments = [fragment for fragment in store.fragments.values() if fragment.document_id == document_id]
    candidates = store.llm.extract_entities(fragments)
    for candidate in candidates:
        store.add_candidate(candidate)
    return {"document_id": document_id, "status": "completed", "candidates": len(candidates)}


@app.post("/search")
def search(request: SearchRequest):
    return orchestrator.search(request.query, top_k=request.top_k)


@app.post("/query")
def query(request: QueryRequest):
    return orchestrator.answer(request)


@app.get("/entities/{entity_id}")
def get_entity(entity_id: str):
    facts = [
        fact
        for fact in store.facts.values()
        if entity_id in {fact.material_id, fact.experiment_id, fact.id, fact.source.fragment_id}
    ]
    if not facts:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"entity_id": entity_id, "facts": facts}


@app.get("/entities/{entity_id}/graph")
def get_entity_graph(entity_id: str, depth: int = 2):
    return store.get_graph(entity_id=entity_id)


@app.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str):
    facts = [fact for fact in store.facts.values() if fact.experiment_id == experiment_id]
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
    return candidate


@app.post("/ontology/candidates/{candidate_id}/reject")
def reject_ontology_candidate(candidate_id: str):
    candidate = store.ontology_candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Ontology candidate not found")
    candidate.status = CandidateStatus.rejected
    return candidate


@app.post("/ontology/candidates/{candidate_id}/merge")
def merge_ontology_candidate(candidate_id: str, target_type: str):
    candidate = store.ontology_candidates.get(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Ontology candidate not found")
    candidate.status = CandidateStatus.approved
    candidate.similar_existing_types.append(target_type)
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
    return store.approve_candidate(candidate_id)


@app.post("/review/facts/{candidate_id}/reject")
def reject_fact(candidate_id: str):
    if candidate_id not in store.candidates:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return store.reject_candidate(candidate_id)
