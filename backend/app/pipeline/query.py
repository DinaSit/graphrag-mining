from __future__ import annotations

import json
from statistics import mean
from typing import Any

from app.pipeline.query_parsing import LLMQuestionParser
from app.schemas import ExperimentRow, Fact, ParsedQuestion, QueryRequest, QueryResponse, SearchResponse
from app.storage import ApplicationStore

try:
    from ml_extraction.app.yandex_client import chat_json
except ImportError:
    chat_json = None

GRAPH_LIMIT = 40

ANSWER_SYSTEM_PROMPT = """Ты формируешь ответ пользователю строго на основе предоставленного evidence pack.
Тебе ЗАПРЕЩЕНО использовать любые знания вне evidence pack. Если данных недостаточно — прямо скажи об этом.
Ответь JSON без пояснений:
{
  "summary": "2-4 предложения с ответом по существу, с указанием диапазонов значений и источников по id",
  "confirmed": ["короткий подтверждённый вывод", ...],
  "contradictions": ["описание противоречия между источниками", ...],
  "gaps": ["чего не хватает в данных", ...],
  "hypotheses": ["гипотеза на основе косвенных данных, явно помеченная как непрямая", ...]
}
Каждый пункт в confirmed должен ссылаться на конкретный факт/источник из evidence pack (по id)."""


class QueryOrchestrator:
    def __init__(self, store: ApplicationStore, question_parser: LLMQuestionParser | None = None):
        self.store = store
        self.question_parser = question_parser or LLMQuestionParser(normalizer=store.normalizer)

    def search(self, query: str, top_k: int = 8) -> SearchResponse:
        return SearchResponse(hits=self.store.search(query, top_k=top_k))

    async def answer(self, request: QueryRequest) -> QueryResponse:
        parsed = await self.question_parser.parse_question(request.question)

        claim_ids = self._graph_traverse(parsed)

        graph_facts = [self.store.facts[cid] for cid in claim_ids if cid in self.store.facts]

        legacy_facts = self._filter_facts_legacy(list(self.store.facts.values()), request, parsed)

        facts = self._merge_unique(graph_facts, legacy_facts)
        facts = sorted(facts, key=self._rank_fact(parsed), reverse=True)

        numeric_evidence = self._numeric_condition_matches(parsed, claim_ids)

        search_hits = self.store.search(request.question, top_k=10) if not facts and not numeric_evidence else []

        is_hypothesis_mode = not facts and not numeric_evidence
        hypotheses: list[str] = []
        if is_hypothesis_mode:
            facts, hypotheses = self._indirect_search(parsed)

        experiments = [self._row_from_fact(fact) for fact in facts[:20]]
        sources = self._collect_sources(facts)
        contradictions = self._find_contradictions(facts)
        gaps = self._find_gaps(parsed, facts, numeric_evidence)
        graph = self.store.get_graph(facts=facts[:20])
        confidence = round(mean([fact.confidence for fact in facts]), 3) if facts else 0.0

        evidence_pack = self._build_evidence_pack(parsed, facts, numeric_evidence, search_hits, contradictions, gaps)
        llm_answer = await self._generate_answer(request.question, evidence_pack)

        summary = llm_answer.get("summary") or self._fallback_summary(parsed, facts, contradictions, gaps)
        if llm_answer.get("contradictions"):
            contradictions = list(dict.fromkeys(contradictions + llm_answer["contradictions"]))
        if llm_answer.get("gaps"):
            gaps = list(dict.fromkeys(gaps + llm_answer["gaps"]))
        if llm_answer.get("hypotheses"):
            hypotheses = list(dict.fromkeys(hypotheses + llm_answer["hypotheses"]))

        return QueryResponse(
            summary=summary,
            experiments=experiments,
            sources=sources[:12],
            graph=graph,
            contradictions=contradictions,
            gaps=gaps,
            confidence=confidence,
            hypotheses=hypotheses,  
        )

    def _graph_traverse(self, parsed: ParsedQuestion) -> set[str]:
        if not self.store.graph_sink or not self.store.graph_sink.enabled:
            return set()
        claim_ids: set[str] = set()

        terms = [e.name for e in parsed.entities]
        for value in (parsed.material, parsed.process, parsed.equipment):
            if value:
                terms.append(value)
        if terms:
            claim_ids |= self._template_entity_neighbors(terms)

        for condition in parsed.conditions + ([parsed.target] if parsed.target else []):
            claim_ids |= self._template_numeric_parameter(condition.parameter)

        if parsed.region:
            claim_ids |= self._template_region(parsed.region)

        return claim_ids

    def _template_entity_neighbors(self, terms: list[str]) -> set[str]:
        """Шаблон 1: сущность (любого типа онтологии) -> Claim'ы, которые её упоминают."""
        query = """
        UNWIND $terms AS term
        MATCH (n) WHERE toLower(n.name) CONTAINS toLower(term)
        MATCH (c:Claim)-[:MENTIONS]->(n)
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"terms": terms, "limit": GRAPH_LIMIT})
        return {row["claim_id"] for row in rows if row.get("claim_id")}

    def _template_numeric_parameter(self, parameter_name: str) -> set[str]:
        """Шаблон 2: NumericParameter/Condition по имени -> связанные Experiment/Claim.

        Само значение параметра в узле графа сейчас не хранится (только name),
        поэтому фильтрация по value_min/value_max делается позже в Python
        по Fact/candidate.payload — см. _numeric_condition_matches.
        """
        query = """
        MATCH (p) WHERE (p:NumericParameter OR p:Condition) AND toLower(p.name) CONTAINS toLower($parameter)
        OPTIONAL MATCH (c:Claim)-[:MENTIONS]->(p)
        OPTIONAL MATCH (p)<-[:measured_parameter|operates_at_condition]-(e:Experiment)<-[:BASED_ON]-(c2:Claim)
        RETURN DISTINCT c.id AS claim_id, c2.id AS claim_id_2
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"parameter": parameter_name, "limit": GRAPH_LIMIT})
        result: set[str] = set()
        for row in rows:
            if row.get("claim_id"):
                result.add(row["claim_id"])
            if row.get("claim_id_2"):
                result.add(row["claim_id_2"])
        return result

    def _template_region(self, region_name: str) -> set[str]:
        """Шаблон 3: Region -> Claim'ы, упоминающие решения/публикации в этом регионе."""
        query = """
        MATCH (r:Region) WHERE toLower(r.name) CONTAINS toLower($region)
        MATCH (c:Claim)-[:MENTIONS]->(r)
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"region": region_name, "limit": GRAPH_LIMIT})
        return {row["claim_id"] for row in rows if row.get("claim_id")}

    def _numeric_condition_matches(self, parsed: ParsedQuestion, claim_ids: set[str]) -> list[dict[str, Any]]:
        if not parsed.conditions and not parsed.target:
            return []
        wanted = list(parsed.conditions) + ([parsed.target] if parsed.target else [])
        matches: list[dict[str, Any]] = []
        candidate_pool = (
            [self.store.candidates[f"candidate-{cid.replace('claim-', '')}"] for cid in claim_ids
             if f"candidate-{cid.replace('claim-', '')}" in self.store.candidates]
            or list(self.store.candidates.values())
        )
        for candidate in candidate_pool:
            payload = candidate.payload
            numeric_params = payload.get("numeric_parameters") or payload.get("parameters") or []
            for condition in wanted:
                for item in numeric_params:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("type") or item.get("parameter") or item.get("name") or "").lower()
                    if condition.parameter.lower() not in name and name not in condition.parameter.lower():
                        continue
                    if self._value_in_range(item, condition):
                        matches.append({"candidate_id": candidate.id, "source": candidate.source, "parameter": item})
        return matches

    @staticmethod
    def _value_in_range(item: dict[str, Any], condition) -> bool:
        value = item.get("value")
        value_min = item.get("value_min", value)
        value_max = item.get("value_max", value)
        if value_min is None and value_max is None:
            return True 
        try:
            value_min = float(value_min) if value_min is not None else None
            value_max = float(value_max) if value_max is not None else None
        except (TypeError, ValueError):
            return True
        if condition.value_min is not None and value_max is not None and value_max < condition.value_min:
            return False
        if condition.value_max is not None and value_min is not None and value_min > condition.value_max:
            return False
        return True

    def _indirect_search(self, parsed: ParsedQuestion) -> tuple[list[Fact], list[str]]:
        """Прямых данных нет: ищем по одному ослабленному признаку за раз (материал ИЛИ процесс)."""
        hypotheses: list[str] = []
        loose_terms = [parsed.material, parsed.process, parsed.equipment] + [e.name for e in parsed.entities]
        loose_terms = [t for t in loose_terms if t]
        found: list[Fact] = []
        for term in loose_terms:
            normalized = self.store.normalizer.normalize_entity(term) or term
            partial = [f for f in self.store.facts.values() if normalized.lower() in f.material.lower() or normalized.lower() in f.process.lower()]
            if partial:
                found.extend(partial)
                hypotheses.append(
                    f"Прямых данных по полной комбинации не найдено. Найдены косвенные кейсы по «{term}» "
                    f"({len(partial)} факт(ов)) — не подтверждённый вывод, гипотеза для проверки."
                )
        seen = set()
        unique = []
        for f in found:
            if f.id not in seen:
                unique.append(f)
                seen.add(f.id)
        for f in unique:
            f = f.model_copy(update={"is_hypothesis": True})
        return unique, hypotheses

    def _build_evidence_pack(self, parsed, facts, numeric_evidence, search_hits, contradictions, gaps) -> dict[str, Any]:
        return {
            "question_plan": parsed.model_dump(mode="json"),
            "facts": [
                {
                    "id": f.id,
                    "material": f.material,
                    "process": f.process,
                    "property": f.property,
                    "effect": f.effect_direction,
                    "value": f.effect_value,
                    "unit": f.effect_unit,
                    "status": f.status,
                    "confidence": f.confidence,
                    "source": f.source.model_dump(mode="json"),
                }
                for f in facts[:15]
            ],
            "numeric_matches": [
                {"candidate_id": m["candidate_id"], "parameter": m["parameter"],
                 "source": m["source"].model_dump(mode="json") if m["source"] else None}
                for m in numeric_evidence[:15]
            ],
            "search_hits": [
                {"fragment_id": h.fragment_id, "text": h.text[:400], "score": h.score,
                 "source": h.source.model_dump(mode="json")}
                for h in search_hits[:10]
            ],
            "known_contradictions": contradictions,
            "known_gaps": gaps,
        }

    async def _generate_answer(self, question: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
        if chat_json is None:
            return {}
        try:
            return await chat_json(
                messages=[
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(
                        {"question": question, "evidence_pack": evidence_pack}, ensure_ascii=False
                    )},
                ]
            )
        except Exception:
            return {}

    def _filter_facts_legacy(self, facts: list[Fact], request: QueryRequest, parsed: ParsedQuestion) -> list[Fact]:
        confidence_min = max(request.confidence_min, request.filters.confidence_min)
        result: list[Fact] = []
        normalized_material = self.store.normalizer.normalize_entity(parsed.material) if parsed.material else None
        normalized_property = self.store.normalizer.normalize_entity(parsed.property) if parsed.property else None
        filter_materials = {self.store.normalizer.normalize_entity(item) or item for item in request.filters.materials}
        filter_properties = {self.store.normalizer.normalize_entity(item) or item for item in request.filters.properties}
        filter_labs = set(request.filters.laboratories)
        for fact in facts:
            if fact.confidence < confidence_min:
                continue
            if fact.is_hypothesis and not request.include_hypotheses:
                continue
            if normalized_material and fact.material != normalized_material:
                continue
            if normalized_property and fact.property != normalized_property:
                continue
            if parsed.temperature_min is not None and fact.temperature_c is not None and fact.temperature_c < parsed.temperature_min:
                continue
            if parsed.temperature_max is not None and fact.temperature_c is not None and fact.temperature_c > parsed.temperature_max:
                continue
            if filter_materials and fact.material not in filter_materials:
                continue
            if filter_properties and fact.property not in filter_properties:
                continue
            if filter_labs and fact.lab not in filter_labs:
                continue
            result.append(fact)
        return result

    @staticmethod
    def _merge_unique(*fact_lists: list[Fact]) -> list[Fact]:
        seen: set[str] = set()
        merged: list[Fact] = []
        for facts in fact_lists:
            for fact in facts:
                if fact.id not in seen:
                    merged.append(fact)
                    seen.add(fact.id)
        return merged

    def _rank_fact(self, parsed: ParsedQuestion):
        def rank(fact: Fact) -> float:
            score = fact.confidence
            if parsed.material and fact.material == (self.store.normalizer.normalize_entity(parsed.material) or parsed.material):
                score += 0.3
            if parsed.property and fact.property == (self.store.normalizer.normalize_entity(parsed.property) or parsed.property):
                score += 0.25
            if parsed.process and parsed.process.lower() in fact.process.lower():
                score += 0.2
            if parsed.temperature_min is not None and parsed.temperature_max is not None and fact.temperature_c is not None:
                middle = (parsed.temperature_min + parsed.temperature_max) / 2
                width = max(parsed.temperature_max - parsed.temperature_min, 1)
                score += max(0.0, 0.2 - abs(fact.temperature_c - middle) / width * 0.2)
            return score
        return rank

    def _row_from_fact(self, fact: Fact) -> ExperimentRow:
        value = _direction_ru(fact.effect_direction)
        if fact.effect_value is not None:
            value += f" на {fact.effect_value:g}{fact.effect_unit or ''}"
        return ExperimentRow(
            experiment_id=fact.experiment_id, material=fact.material, sample=fact.sample,
            process=fact.process, temperature_c=fact.temperature_c, duration_h=fact.duration_h,
            property=fact.property, effect=value, lab=fact.lab, confidence=fact.confidence, source=fact.source,
        )

    def _collect_sources(self, facts: list[Fact]) -> list:
        sources = []
        seen = set()
        for fact in facts:
            key = (fact.source.document_id, fact.source.fragment_id)
            if key not in seen:
                sources.append(fact.source)
                seen.add(key)
        return sources

    def _fallback_summary(self, parsed, facts, contradictions, gaps) -> str:
        if not facts:
            return "Подтвержденных фактов по заданным условиям не найдено; проверьте фильтры или включите гипотезы."
        material = parsed.material or facts[0].material
        property_name = parsed.property or facts[0].property
        summary = f"Для {material} по «{property_name}» найдено {len(facts)} факт(ов) в базе знаний."
        if contradictions:
            summary += " Есть противоречивые результаты между источниками."
        if gaps:
            summary += " Обнаружены пробелы в покрытии данных."
        return summary

    def _find_contradictions(self, facts: list[Fact]) -> list[str]:
        groups: dict[tuple[str, str], set[str]] = {}
        labs: dict[tuple[str, str], set[str]] = {}
        for fact in facts:
            key = (fact.material, fact.property)
            groups.setdefault(key, set()).add(fact.effect_direction)
            labs.setdefault(key, set()).add(fact.lab)
        contradictions = []
        for (material, property_name), directions in groups.items():
            if "increase" in directions and "decrease" in directions:
                contradictions.append(
                    f"{material}, {property_name}: разные источники показывают противоположный эффект; "
                    f"лаборатории: {', '.join(sorted(labs[(material, property_name)]))}."
                )
        return contradictions

    def _find_gaps(self, parsed, facts: list[Fact], numeric_evidence: list[dict[str, Any]]) -> list[str]:
        gaps: list[str] = []
        if not facts and not numeric_evidence:
            return ["Нет подтверждённых фактов для заданной комбинации условий."]
        if facts:
            labs = {fact.lab for fact in facts}
            if len(labs) < 2:
                gaps.append("Результаты подтверждены менее чем двумя независимыми источниками.")
        if parsed.target and not numeric_evidence and not facts:
            gaps.append(f"Нет данных, напрямую подтверждающих целевой показатель «{parsed.target.parameter}».")
        return gaps


def _direction_ru(value: str) -> str:
    return {"increase": "рост", "decrease": "снижение", "neutral": "без изменений"}.get(value, value)