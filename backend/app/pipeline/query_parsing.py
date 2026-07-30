from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas import ONTOLOGY_LABELS, ParsedQuestion, QueryCondition, QueryEntity

# LLM доступна из backend только по HTTP через сервис ml-extraction
from app.pipeline.llm_bridge import LLMUnavailableError, chat_json

SYSTEM_PROMPT = f"""Ты — Query Planner для GraphRAG-системы горно-металлургической отрасли.
Разбери вопрос пользователя строго в JSON, без пояснений и markdown-обёртки.

Схема ответа (все поля опциональны):
{{
  "process": строка или null — технологический процесс из вопроса,
  "material": строка или null — материал/вещество,
  "equipment": строка или null — оборудование,
  "property": строка или null — измеряемое свойство,
  "region": строка или null — страна/регион,
  "year_min": число или null — самый ранний допустимый год издания источника,
  "year_max": число или null — самый поздний допустимый год издания источника,
  "entities": [{{"type": один из {list(ONTOLOGY_LABELS)}, "name": строка}}],
  "conditions": [{{"parameter": строка, "value_min": число|null, "value_max": число|null, "unit": строка|null}}],
  "target": {{"parameter": строка, "value_min": число|null, "value_max": число|null, "unit": строка|null}} или null
             — целевой показатель, который нужно обеспечить (например "сухой остаток <=1000 мг/дм3")
}}

Правила:
- "не менее X" -> value_min=X; "не более X" / "<=X" -> value_max=X; "A-B" -> value_min=A, value_max=B.
- Годы: "за последние N лет" -> year_min = текущий год минус N; "с 2020" -> year_min=2020;
  "до 2015" -> year_max=2015; "в 2022 году" -> year_min=year_max=2022. Нет упоминания срока -> оба null.
- Каждое числовое условие из вопроса (кроме целевого показателя) попадает в conditions, а не только одно.
- Не придумывай сущности, которых нет в тексте вопроса.
- Ответ — только JSON-объект, ничего больше."""


class LLMQuestionParser:
    """Query Planner поверх chat_json: единственный разборщик вопросов в системе."""

    def __init__(self, normalizer=None, model: str | None = None):
        self.normalizer = normalizer
        self.model = model

    async def parse_question(self, question: str) -> ParsedQuestion:
        # Отказ LLM не маскируется: LLMUnavailableError пробрасывается
        # вызывающему, оркестратор явно сообщает о нём и собирает ответ без плана
        # Текущий год подставляется на каждый вызов, а не при импорте модуля:
        # иначе «за последние 5 лет» считались бы от года запуска процесса
        raw = await chat_json(
            messages=[
                {"role": "system",
                 "content": f"{SYSTEM_PROMPT}\nТекущий год: {datetime.now(timezone.utc).year}."},
                {"role": "user", "content": question},
            ],
            model=self.model,
        )
        try:
            return self._validate(raw)
        except Exception as error:
            raise LLMUnavailableError("bad_response", f"план вопроса не разобран: {error}") from error

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
            material=material,
            property=raw.get("property"),
            process=raw.get("process"),
            equipment=raw.get("equipment"),
            region=raw.get("region"),
            year_min=_year(raw.get("year_min")),
            year_max=_year(raw.get("year_max")),
            entities=entities,
            conditions=conditions,
            target=target,
        )


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _year(value: Any) -> int | None:
    """Год из ответа модели. Значения вне 1900..текущий отбрасываются: это не
    год издания, а число, случайно попавшее в поле."""
    number = _num(value)
    if number is None:
        return None
    year = int(number)
    return year if 1900 <= year <= datetime.now(timezone.utc).year else None