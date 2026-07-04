from __future__ import annotations

import json
from typing import Any

from app.pipeline.providers import MockLLMProvider
from app.schemas import ParsedQuestion, QueryCondition, QueryEntity

try:
    from ml_extraction.app.yandex_client import chat_json
except ImportError:
    chat_json = None

ONTOLOGY_LABELS = [
    "Material", "Process", "Equipment", "Property", "NumericParameter", "Condition",
    "Experiment", "Publication", "Expert", "Facility", "Result", "Recommendation", "Region",
]

SYSTEM_PROMPT = f"""Ты — Query Planner для GraphRAG-системы горно-металлургической отрасли.
Разбери вопрос пользователя строго в JSON, без пояснений и markdown-обёртки.

Схема ответа (все поля опциональны, кроме intent):
{{
  "intent": "find_technology" | "compare_experiments" | "list_experiments" | "compare_regions",
  "process": строка или null — технологический процесс из вопроса,
  "material": строка или null — материал/вещество,
  "equipment": строка или null — оборудование,
  "property": строка или null — измеряемое свойство,
  "region": строка или null — страна/регион,
  "year_from": число или null — нижняя граница года, если спрашивают "за последние N лет",
  "entities": [{{"type": один из {ONTOLOGY_LABELS}, "name": строка}}],
  "conditions": [{{"parameter": строка, "value_min": число|null, "value_max": число|null, "unit": строка|null}}],
  "target": {{"parameter": строка, "value_min": число|null, "value_max": число|null, "unit": строка|null}} или null
             — целевой показатель, который нужно обеспечить (например "сухой остаток <=1000 мг/дм3"),
  "keywords": [строка, ...] — 3-6 ключевых слов для семантического поиска
}}

Правила:
- "не менее X" -> value_min=X; "не более X" / "<=X" -> value_max=X; "A-B" -> value_min=A, value_max=B.
- Каждое числовое условие из вопроса (кроме целевого показателя) попадает в conditions, а не только одно.
- Не придумывай сущности, которых нет в тексте вопроса.
- Ответ — только JSON-объект, ничего больше."""


class LLMQuestionParser:
    """Реальный Query Planner поверх chat_json. Используется вместо store.llm.parse_question."""

    def __init__(self, normalizer=None, model: str | None = None):
        self.normalizer = normalizer
        self.model = model
        self.fallback = MockLLMProvider()

    async def parse_question(self, question: str) -> ParsedQuestion:
        if chat_json is None:
            return self.fallback.parse_question(question)
        try:
            raw = await chat_json(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ],
                model=self.model,
            )
        except Exception:
            return self.fallback.parse_question(question)

        try:
            return self._validate(raw)
        except Exception:
            return self.fallback.parse_question(question)

    def _validate(self, raw: dict[str, Any]) -> ParsedQuestion:
        entities = []
        for item in raw.get("entities") or []:
            etype = item.get("type")
            name = item.get("name")
            if not name or etype not in ONTOLOGY_LABELS:
                continue
            if self.normalizer is not None:
                name = self.normalizer.normalize_entity(name) or name
            entities.append(QueryEntity(type=etype, name=name))

        conditions = [
            QueryCondition(
                parameter=str(item["parameter"]),
                value_min=_num(item.get("value_min")),
                value_max=_num(item.get("value_max")),
                unit=item.get("unit"),
            )
            for item in (raw.get("conditions") or [])
            if item.get("parameter")
        ]

        target = None
        if raw.get("target") and raw["target"].get("parameter"):
            t = raw["target"]
            target = QueryCondition(
                parameter=str(t["parameter"]),
                value_min=_num(t.get("value_min")),
                value_max=_num(t.get("value_max")),
                unit=t.get("unit"),
            )

        material = raw.get("material")
        if material and self.normalizer is not None:
            material = self.normalizer.normalize_entity(material) or material

        return ParsedQuestion(
            intent=raw.get("intent") or "compare_experiments",
            material=material,
            property=raw.get("property"),
            process=raw.get("process"),
            equipment=raw.get("equipment"),
            region=raw.get("region"),
            year_from=_int(raw.get("year_from")),
            entities=entities,
            conditions=conditions,
            target=target,
            keywords=[str(k) for k in (raw.get("keywords") or [])][:6],
        )


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None