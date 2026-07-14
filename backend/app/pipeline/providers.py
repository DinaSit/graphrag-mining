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
from typing import Any

from app.pipeline.normalization import float_or_none
from app.schemas import ExtractionCandidate, SourceRef, SourceFragment


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


class MockLLMProvider:
    name = "mock-llm"

    def extract_entities(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        for fragment in fragments:
            row = fragment.metadata.get("row_data", {})
            payload = self._payload_from_row(row) if row else self._payload_from_text(fragment.text)
            if not payload:
                continue
            confidence = float(payload.pop("confidence", 0.82))
            source = SourceRef(
                document_id=fragment.document_id,
                version_id=fragment.version_id,
                fragment_id=fragment.id,
                page=fragment.page,
                section=fragment.section,
                quote=fragment.text[:220],
            )
            candidates.append(
                ExtractionCandidate(
                    id=f"candidate-{fragment.id}",
                    type="Claim",
                    payload=payload,
                    source=source,
                    confidence=confidence,
                )
            )
        return candidates

    def _payload_from_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        material = row.get("material") or row.get("материал")
        property_name = row.get("property") or row.get("свойство")
        if not material or not property_name:
            return None
        return {
            "material": material,
            "experiment_id": row.get("experiment_id") or row.get("эксперимент") or "uploaded-exp",
            "sample": row.get("sample") or row.get("образец") or "uploaded-sample",
            "process": row.get("process") or row.get("режим") or "unknown process",
            "temperature_c": float_or_none(row.get("temperature_c") or row.get("temperature")),
            "duration_h": float_or_none(row.get("duration_h") or row.get("duration")),
            "property": property_name,
            "effect_direction": row.get("effect_direction") or row.get("effect") or "unknown",
            "effect_value": float_or_none(row.get("effect_value")),
            "effect_unit": row.get("effect_unit") or "%",
            "result_value": float_or_none(row.get("result_value")),
            "result_unit": row.get("result_unit") or row.get("unit"),
            "lab": row.get("lab") or row.get("laboratory") or "Unknown Lab",
            "team": row.get("team") or "Unknown Team",
            "equipment": row.get("equipment") or "unknown equipment",
            "confidence": float_or_none(row.get("confidence")) or 0.86,
        }

    def _payload_from_text(self, text: str) -> dict[str, Any] | None:
        lower = text.lower().replace("ё", "е")
        if "сплав x" not in lower and "alloy x" not in lower:
            return None
        temp = re.search(r"(\d{3})\s*°?\s*[cс]", lower)
        duration = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ч|h|час)", lower)
        effect_value = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*%", lower)
        direction = "increase" if "повыш" in lower or "увелич" in lower else "decrease" if "сниж" in lower or "уменьш" in lower else "unknown"
        return {
            "material": "Сплав X",
            "experiment_id": "uploaded-text-exp",
            "sample": "uploaded-sample",
            "process": "термообработка",
            "temperature_c": float(temp.group(1)) if temp else None,
            "duration_h": float(duration.group(1).replace(",", ".")) if duration else None,
            "property": "твёрдость",
            "effect_direction": direction,
            "effect_value": float(effect_value.group(1).replace(",", ".")) if effect_value else None,
            "effect_unit": "%",
            "result_value": None,
            "result_unit": None,
            "lab": "Uploaded Lab",
            "team": "Uploaded Team",
            "equipment": "unknown equipment",
            "confidence": 0.84,
        }


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
