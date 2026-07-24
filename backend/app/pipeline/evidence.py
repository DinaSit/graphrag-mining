"""Доказательная база ответа: собранные материалы, evidence pack и вызов LLM.

CollectedEvidence — всё, что готово ДО генерации: общая ступень для /ask и
/ask/stream. Модель видит только evidence pack и цитирует его сквозными
номерами (перевод номеров в id — pipeline/citations).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.llm_bridge import LLMUnavailableError, chat_json
from app.schemas import (
    ExperimentRow,
    Fact,
    GraphPayload,
    ParsedQuestion,
    QueryRequest,
    SearchHit,
    SourceRef,
)

ANSWER_SYSTEM_PROMPT = """Ты формируешь ответ пользователю строго на основе предоставленного evidence pack.
Тебе ЗАПРЕЩЕНО использовать любые знания вне evidence pack. Если данных недостаточно — прямо скажи об этом.
Ответь JSON без пояснений. Ключ "summary" ОБЯЗАН идти ПЕРВЫМ ключом объекта:
{
  "summary": "2-4 предложения с ответом по существу, с указанием диапазонов значений и источников по id",
  "sufficient": true|false — хватает ли evidence pack для ПРЯМОГО ответа на вопрос (false, если данные лишь смежные или их нет),
  "confirmed": ["короткий подтверждённый вывод", ...],
  "contradictions": ["описание противоречия между источниками", ...],
  "gaps": ["чего не хватает в данных", ...],
  "hypotheses": ["текст гипотезы на основе косвенных данных БЕЗ пометок вроде «непрямая/косвенная гипотеза:» — пометку ставит интерфейс", ...]
}
Правила цитирования (СТРОГО):
- у каждого элемента evidence pack (facts и search_hits) есть числовой номер в поле "n";
- ссылка на источник — ТОЛЬКО этот номер в квадратных скобках: [3];
- несколько источников — в одних скобках через запятую: [3, 7]; подряд идущие тоже ТОЛЬКО через запятую [5, 6, 7] — диапазоны вида [5–10] ЗАПРЕЩЕНЫ;
- используй ТОЛЬКО номера, реально присутствующие в evidence pack; НЕ придумывай номера;
- НЕ пиши id фрагментов, названия файлов или текстовые ссылки — только номер;
- каждый вывод в confirmed и каждое противоречие в contradictions заканчивай такой ссылкой."""


@dataclass
class CollectedEvidence:
    """Всё, что собрано ДО генерации LLM: общая ступень для /ask и /ask/stream.

    llm_errors мутируется дальше по конвейеру: сюда дописывается отказ
    генерации, чтобы finalize показал причину пользователю.
    """

    request: QueryRequest
    pipeline_mode: str  # "fast" — классический RAG, "full" — граф + планировщик
    llm_errors: list[str]
    parsed: ParsedQuestion | None
    facts: list[Fact]
    numeric_evidence: list[dict[str, Any]]
    search_hits: list[SearchHit]
    has_direct_facts: bool
    related_facts: list[Fact]
    hypotheses: list[str]
    experiments: list[ExperimentRow]
    sources: list[SourceRef]
    contradictions: list[str]
    gaps: list[str]
    graph: GraphPayload
    confidence: float
    evidence_pack: dict[str, Any]
    # Карта номерных цитат, показанных модели, → id фрагмента: номер "3" в
    # ответе LLM (см. ANSWER_SYSTEM_PROMPT) переводится в канонический
    # [fragment-…] на этапе finalize. Номера надёжно воспроизводятся моделью,
    # тогда как длинные id она искажает (дописывает несуществующие суффиксы -N).
    citation_index: dict[str, str] = field(default_factory=dict)


def build_evidence_pack(
    parsed, facts, numeric_evidence, search_hits, contradictions, gaps
) -> tuple[dict[str, Any], dict[str, str]]:
    """Возвращает (pack, citation_index). Каждому цитируемому элементу
    (facts, затем search_hits) присваивается сквозной номер "n" — модель
    цитирует ИМ, а finalize переводит номер в канонический id фрагмента.
    citation_index: "номер" → fragment_id."""
    citation_index: dict[str, str] = {}
    marker = 0
    fact_items: list[dict[str, Any]] = []
    for f in facts[:15]:
        marker += 1
        citation_index[str(marker)] = f.source.fragment_id
        fact_items.append({
            "n": marker,
            "material": f.material,
            "process": f.process,
            "property": f.property,
            "effect": f.effect_direction,
            "value": f.effect_value,
            "unit": f.effect_unit,
            "status": f.status,
            "confidence": f.confidence,
            "source": f.source.model_dump(mode="json"),
        })
    hit_items: list[dict[str, Any]] = []
    for h in search_hits[:10]:
        marker += 1
        citation_index[str(marker)] = h.fragment_id
        hit_items.append({
            "n": marker,
            "text": h.text[:400],
            "score": h.score,
            "source": h.source.model_dump(mode="json"),
        })
    pack = {
        "question_plan": parsed.model_dump(mode="json") if parsed is not None else None,
        "facts": fact_items,
        "numeric_matches": [
            {"candidate_id": m["candidate_id"], "parameter": m["parameter"],
             "source": m["source"].model_dump(mode="json") if m["source"] else None}
            for m in numeric_evidence[:15]
        ],
        "search_hits": hit_items,
        "known_contradictions": contradictions,
        "known_gaps": gaps,
    }
    return pack, citation_index


def answer_messages(question: str, evidence_pack: dict[str, Any]) -> list[dict[str, str]]:
    """Сообщения генерации ответа: одни и те же для /ask (chat_json)
    и /ask/stream (chat_stream)."""
    return [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(
            {"question": question, "evidence_pack": evidence_pack}, ensure_ascii=False
        )},
    ]


async def generate_answer(question: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    # Отказ LLM пробрасывается наверх (LLMUnavailableError) и показывается
    # пользователю; прочие сбои приводятся к тому же типу
    try:
        return await chat_json(messages=answer_messages(question, evidence_pack))
    except LLMUnavailableError:
        raise
    except Exception as error:
        raise LLMUnavailableError("bad_response", str(error)) from error
