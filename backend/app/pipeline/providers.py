from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from collections import OrderedDict

from app.schemas import ExtractionCandidate, SourceFragment


class DeterministicEmbeddingProvider:
    name = "deterministic-hash"
    dimensions = 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[\wА-Яа-яЁё]+", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]


class RemoteEmbeddingProvider:
    """HTTP-адаптер к внешнему сервису эмбеддингов (POST {url} → {"embeddings": [...]}).

    Резервный переход на детерминированный провайдер отсутствует намеренно: сбой
    индексации должен быть видимым, смешение векторов разных моделей делает поиск
    некорректным.
    """

    name = "remote-embeddings"

    # Кэш хранит эмбеддинги повторных вопросов и неизменённых фрагментов
    # (reprocess, переиндексация при старте) — без обращения к сервису
    _CACHE_MAX = 20000

    def __init__(self, embed_url: str):
        self.embed_url = embed_url
        self.dimensions = int(os.environ.get("EMBEDDING_DIM", "1024"))  # bge-m3
        self.timeout = float(os.environ.get("EMBEDDING_TIMEOUT", "120"))
        self._cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._call(texts, kind="doc")

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        # У модели раздельные режимы для документов и поисковых запросов
        return self._call(texts, kind="query")

    def _call(self, texts: list[str], kind: str) -> list[list[float]]:
        # Режим входит в ключ: query- и doc-векторы одной модели не взаимозаменяемы
        keys = [(kind, hashlib.sha256(text.encode("utf-8")).hexdigest()) for text in texts]
        results: dict[int, list[float]] = {}
        with self._cache_lock:
            for index, key in enumerate(keys):
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    results[index] = cached
        miss_indexes = [index for index in range(len(texts)) if index not in results]
        if miss_indexes:
            fetched = self._fetch([texts[index] for index in miss_indexes], kind)
            if len(fetched) != len(miss_indexes):
                raise ValueError(
                    f"Сервис эмбеддингов вернул {len(fetched)} векторов на {len(miss_indexes)} текстов"
                )
            with self._cache_lock:
                for index, vector in zip(miss_indexes, fetched):
                    results[index] = vector
                    self._cache[keys[index]] = vector
                    if len(self._cache) > self._CACHE_MAX:
                        self._cache.popitem(last=False)
        return [results[index] for index in range(len(texts))]

    def _fetch(self, texts: list[str], kind: str) -> list[list[float]]:
        request = urllib.request.Request(
            self.embed_url,
            data=json.dumps({"texts": texts, "kind": kind}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["embeddings"]


class RemoteExtractionProvider:
    name = "remote-extraction"

    def __init__(self, extract_url: str):
        self.extract_url = extract_url
        # Реальный LLM-сервис обрабатывает батч фрагментов дольше 8с;
        # таймаут настраивается снаружи (EXTRACTION_TIMEOUT, секунды)
        self.timeout = float(os.environ.get("EXTRACTION_TIMEOUT", "8"))

    def extract_entities(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        # Фрагменты отправляются партиями: один запрос на весь документ
        # не укладывается в таймаут (особенно сканы с vision-обработкой)
        batch_size = int(os.environ.get("EXTRACTION_BATCH_SIZE", "8"))
        candidates: list[ExtractionCandidate] = []
        for start in range(0, len(fragments), batch_size):
            chunk = fragments[start : start + batch_size]
            payload = {"fragments": [fragment.model_dump(mode="json") for fragment in chunk]}
            request = urllib.request.Request(
                self.extract_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                # Скрытый переход на mock-провайдер недопустим: пустое извлечение
                # получило бы статус completed (документ «обработан», кандидатов ноль).
                # Ошибка должна быть видимой: job помечается failed, документ
                # остаётся необработанным.
                raise RuntimeError(f"Сервис извлечения недоступен, инжест остановлен: {exc}") from exc
            candidates.extend(ExtractionCandidate.model_validate(item) for item in data.get("candidates", []))
        return candidates
