from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any

from app.schemas import ExtractionCandidate, ParsedQuestion, QueryRequest, SearchHit, SourceRef, SourceFragment


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

    def extract_relations(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        return []

    def parse_question(self, question: str) -> ParsedQuestion:
        lower = question.lower().replace("ё", "е")
        material = None
        if re.search(r"сплав\w*\s+x", lower) or "alloy x" in lower:
            material = "Сплав X"
        else:
            match = re.search(r"(сплав\s+[a-zа-я0-9-]+|alloy\s+[a-z0-9-]+)", lower, re.IGNORECASE)
            if match:
                material = match.group(1).strip().title()

        property_name = None
        if "тверд" in lower or "hardness" in lower:
            property_name = "твёрдость"
        elif "сульфат" in lower or "sulfate" in lower:
            property_name = "сульфаты"
        elif "хлорид" in lower or "chloride" in lower:
            property_name = "хлориды"
        elif "сух" in lower and "остат" in lower:
            property_name = "сухой остаток"
        elif "циркуляц" in lower and "католит" in lower:
            property_name = "скорость циркуляции католита"
        elif "выход" in lower and ("металл" in lower or "никел" in lower):
            property_name = "выход металла"

        if material is None:
            if "шахт" in lower and "вод" in lower:
                material = "шахтные воды"
            elif "католит" in lower or "catholyte" in lower:
                material = "католит"
            elif "катод" in lower or "nickel cathode" in lower:
                material = "никелевые катоды"
        temp_min = temp_max = None
        range_match = re.search(r"(\d{2,4})\s*[–-]\s*(\d{2,4})\s*(?:°?\s*[cс])?", lower)
        if range_match:
            temp_min = float(range_match.group(1))
            temp_max = float(range_match.group(2))
        else:
            one_temp = re.search(r"(\d{2,4})\s*(?:°?\s*[cс])", lower)
            if one_temp:
                temp_min = temp_max = float(one_temp.group(1))

        return ParsedQuestion(
            intent="compare_experiments",
            material=material,
            property=property_name,
            temperature_min=temp_min,
            temperature_max=temp_max,
        )

    def summarize_results(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        if not hits:
            return "По доступным фрагментам не найдено подтвержденных результатов."
        return f"Найдено {len(hits)} релевантных фрагментов с привязкой к первоисточникам."

    def generate_answer(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        return self.summarize_results(request, hits)

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
            "temperature_c": _float_or_none(row.get("temperature_c") or row.get("temperature")),
            "duration_h": _float_or_none(row.get("duration_h") or row.get("duration")),
            "property": property_name,
            "effect_direction": row.get("effect_direction") or row.get("effect") or "unknown",
            "effect_value": _float_or_none(row.get("effect_value")),
            "effect_unit": row.get("effect_unit") or "%",
            "result_value": _float_or_none(row.get("result_value")),
            "result_unit": row.get("result_unit") or row.get("unit"),
            "lab": row.get("lab") or row.get("laboratory") or "Unknown Lab",
            "team": row.get("team") or "Unknown Team",
            "equipment": row.get("equipment") or "unknown equipment",
            "confidence": _float_or_none(row.get("confidence")) or 0.86,
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


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


class RemoteExtractionProvider:
    name = "remote-extraction"

    def __init__(self, extract_url: str, fallback: MockLLMProvider | None = None):
        self.extract_url = extract_url
        self.fallback = fallback or MockLLMProvider()
        # Реальный LLM-сервис обрабатывает батч фрагментов дольше 8с;
        # таймаут настраивается снаружи (EXTRACTION_TIMEOUT, секунды)
        self.timeout = float(os.environ.get("EXTRACTION_TIMEOUT", "8"))

    def extract_entities(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        payload = {"fragments": [fragment.model_dump(mode="json") for fragment in fragments]}
        request = urllib.request.Request(
            self.extract_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return self.fallback.extract_entities(fragments)
        return [ExtractionCandidate.model_validate(item) for item in data.get("candidates", [])]

    def extract_relations(self, fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
        return []

    def parse_question(self, question: str) -> ParsedQuestion:
        return self.fallback.parse_question(question)

    def summarize_results(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        return self.fallback.summarize_results(request, hits)

    def generate_answer(self, request: QueryRequest, hits: list[SearchHit]) -> str:
        return self.fallback.generate_answer(request, hits)
