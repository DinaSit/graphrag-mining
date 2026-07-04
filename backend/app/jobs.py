"""Фоновая обработка документов: очередь в памяти + фиксированное число воркеров.

Загрузка и переобработка выполняются вне HTTP-запроса: API отвечает сразу,
статусы задач пишутся в таблицу jobs. Число воркеров ограничивает параллельную
обработку документов; суммарный лимит LLM-вызовов держит сервис извлечения.
"""
from __future__ import annotations

import logging
import queue
import threading
from uuid import uuid4

from app.storage import ApplicationStore

log = logging.getLogger(__name__)


class IngestQueue:
    def __init__(self, store: ApplicationStore, workers: int = 2):
        self.store = store
        self.tasks: queue.Queue[dict] = queue.Queue()
        for index in range(workers):
            threading.Thread(target=self._worker, name=f"ingest-worker-{index}", daemon=True).start()

    def enqueue_ingest(self, filename: str, content: bytes, document_type: str | None,
                       source_label: str | None, access_level: str) -> str:
        return self._enqueue({
            "kind": "ingest",
            "filename": filename,
            "content": content,
            "document_type": document_type,
            "source_label": source_label,
            "access_level": access_level,
        })

    def enqueue_reprocess(self, document_id: str) -> str:
        return self._enqueue({"kind": "reprocess", "document_id": document_id})

    def _enqueue(self, task: dict) -> str:
        job_id = f"job-{uuid4().hex[:10]}"
        task["job_id"] = job_id
        self._record(job_id, task, "queued")
        self.tasks.put(task)
        return job_id

    def _worker(self) -> None:
        while True:
            task = self.tasks.get()
            job_id = task["job_id"]
            try:
                self._record(job_id, task, "processing")
                if task["kind"] == "ingest":
                    self.store.ingest_document(
                        task["filename"], task["content"], task["document_type"],
                        task["source_label"], task["access_level"],
                    )
                else:
                    self._reprocess(task["document_id"])
                self._record(job_id, task, "completed")
            except Exception as exc:
                log.exception("Фоновая задача %s (%s) упала", job_id, task["kind"])
                self._record(job_id, task, "failed", str(exc))
            finally:
                self.tasks.task_done()

    def _reprocess(self, document_id: str) -> None:
        fragments = [f for f in self.store.fragments.values() if f.document_id == document_id]
        for candidate in self.store.llm.extract_entities(fragments):
            self.store.add_candidate(candidate)

    def _record(self, job_id: str, task: dict, status: str, error: str | None = None) -> None:
        if self.store.postgres_sink:
            payload = {"filename": task.get("filename"), "document_id": task.get("document_id")}
            self.store.postgres_sink.upsert_job(job_id, task["kind"], status, payload, error)
