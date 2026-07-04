from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.file_storage import MinioFileStorage
from app.persistence import Neo4jSink, PostgresSink
from app.schemas import DocumentStatus
from app.storage import ApplicationStore


ROOT_DIR = Path(__file__).resolve().parents[2]
DOMAIN_DIR = ROOT_DIR / "domain" / "default"


def build_store() -> ApplicationStore:
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
    return store


def set_status(store: ApplicationStore, document_id: str, status: DocumentStatus, element_count: int) -> None:
    document = store.documents[document_id]
    version = store.versions[document.current_version_id]
    document.status = status
    document.element_count = element_count
    version.status = status
    store._persist_document(document, version)


def reprocess_document(store: ApplicationStore, document_id: str, chunk_size: int) -> tuple[int, int]:
    if document_id not in store.documents:
        raise KeyError(f"Document not found: {document_id}")
    fragments = [fragment for fragment in store.fragments.values() if fragment.document_id == document_id]
    fragments.sort(key=lambda fragment: fragment.id)
    set_status(store, document_id, DocumentStatus.processing, len(fragments))

    accepted = 0
    try:
        for start in range(0, len(fragments), chunk_size):
            chunk = fragments[start : start + chunk_size]
            candidates = store.llm.extract_entities(chunk)
            for candidate in candidates:
                store.add_candidate(candidate)
            accepted += len(candidates)
            print(
                f"{document_id}: fragments {min(start + len(chunk), len(fragments))}/{len(fragments)}, "
                f"new_candidates={len(candidates)}, total_candidates={accepted}",
                flush=True,
            )
        set_status(store, document_id, DocumentStatus.completed if accepted else DocumentStatus.failed, len(fragments))
        return len(fragments), accepted
    except Exception:
        set_status(store, document_id, DocumentStatus.failed, len(fragments))
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document_ids", nargs="+")
    parser.add_argument("--chunk-size", type=int, default=16)
    args = parser.parse_args()

    store = build_store()
    exit_code = 0
    for document_id in args.document_ids:
        try:
            fragments, candidates = reprocess_document(store, document_id, args.chunk_size)
            print(f"{document_id}: completed fragments={fragments}, candidates={candidates}", flush=True)
        except Exception as exc:
            exit_code = 1
            print(f"{document_id}: failed: {exc}", file=sys.stderr, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
