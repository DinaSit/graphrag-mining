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
        # Источник статусов — память процесса: GET /jobs работает и без
        # PostgreSQL; таблица jobs — персистентная копия на случай рестарта
        self.jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
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

    def get_job(self, job_id: str) -> dict | None:
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

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
                    self.store.reprocess_document(task["document_id"])
                self._record(job_id, task, "completed")
            except Exception as exc:
                log.exception("Фоновая задача %s (%s) упала", job_id, task["kind"])
                self._record(job_id, task, "failed", str(exc))
            finally:
                self.tasks.task_done()

    def _record(self, job_id: str, task: dict, status: str, error: str | None = None) -> None:
        with self._jobs_lock:
            self.jobs[job_id] = {"job_id": job_id, "status": status, "error": error}
        if self.store.postgres_sink:
            payload = {"filename": task.get("filename"), "document_id": task.get("document_id")}
            try:
                self.store.postgres_sink.upsert_job(job_id, task["kind"], status, payload, error)
            except Exception:
                # Статус в памяти остаётся источником: недоступность PG
                # не должна ронять воркер и терять саму задачу
                log.exception("Статус job %s не сохранён в PostgreSQL", job_id)
