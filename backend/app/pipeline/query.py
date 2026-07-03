from __future__ import annotations

from statistics import mean

from app.schemas import ExperimentRow, Fact, QueryRequest, QueryResponse, SearchResponse
from app.storage import ApplicationStore


class QueryOrchestrator:
    def __init__(self, store: ApplicationStore):
        self.store = store

    def search(self, query: str, top_k: int = 8) -> SearchResponse:
        return SearchResponse(hits=self.store.search(query, top_k=top_k))

    def answer(self, request: QueryRequest) -> QueryResponse:
        parsed = self.store.llm.parse_question(request.question)
        facts = self._filter_facts(list(self.store.facts.values()), request, parsed)
        facts = sorted(facts, key=self._rank_fact(parsed), reverse=True)
        experiments = [self._row_from_fact(fact) for fact in facts[:20]]
        sources = []
        seen_sources = set()
        for fact in facts:
            key = (fact.source.document_id, fact.source.fragment_id)
            if key not in seen_sources:
                sources.append(fact.source)
                seen_sources.add(key)
        contradictions = self._find_contradictions(facts)
        gaps = self._find_gaps(parsed, facts)
        graph = self.store.get_graph(facts=facts[:20])
        confidence = round(mean([fact.confidence for fact in facts]), 3) if facts else 0.0
        summary = self._summary(parsed, facts, contradictions, gaps)
        return QueryResponse(
            summary=summary,
            experiments=experiments,
            sources=sources[:12],
            graph=graph,
            contradictions=contradictions,
            gaps=gaps,
            confidence=confidence,
        )

    def _filter_facts(self, facts: list[Fact], request: QueryRequest, parsed) -> list[Fact]:
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

    def _rank_fact(self, parsed):
        def rank(fact: Fact) -> float:
            score = fact.confidence
            if parsed.material and fact.material == self.store.normalizer.normalize_entity(parsed.material):
                score += 0.3
            if parsed.property and fact.property == self.store.normalizer.normalize_entity(parsed.property):
                score += 0.25
            if parsed.temperature_min is not None and parsed.temperature_max is not None and fact.temperature_c is not None:
                middle = (parsed.temperature_min + parsed.temperature_max) / 2
                width = max(parsed.temperature_max - parsed.temperature_min, 1)
                score += max(0.0, 0.2 - abs(fact.temperature_c - middle) / width * 0.2)
            return score

        return rank

    def _row_from_fact(self, fact: Fact) -> ExperimentRow:
        value = f"{_direction_ru(fact.effect_direction)}"
        if fact.effect_value is not None:
            value += f" на {fact.effect_value:g}{fact.effect_unit or ''}"
        return ExperimentRow(
            experiment_id=fact.experiment_id,
            material=fact.material,
            sample=fact.sample,
            process=fact.process,
            temperature_c=fact.temperature_c,
            duration_h=fact.duration_h,
            property=fact.property,
            effect=value,
            lab=fact.lab,
            confidence=fact.confidence,
            source=fact.source,
        )

    def _summary(self, parsed, facts: list[Fact], contradictions: list[str], gaps: list[str]) -> str:
        if not facts:
            return "Подтвержденных фактов по заданным условиям не найдено; проверьте фильтры или включите гипотезы."
        material = parsed.material or facts[0].material
        property_name = parsed.property or facts[0].property
        increases = sum(1 for fact in facts if fact.effect_direction == "increase")
        decreases = sum(1 for fact in facts if fact.effect_direction == "decrease")
        temps = [fact.temperature_c for fact in facts if fact.temperature_c is not None]
        temp_text = f"{min(temps):g}-{max(temps):g} °C" if temps else "без указанной температуры"
        summary = (
            f"Для {material} по свойству «{property_name}» найдено {len(facts)} эксперимента(ов) "
            f"в диапазоне {temp_text}: рост зафиксирован в {increases}, снижение в {decreases}."
        )
        if contradictions:
            summary += " Есть противоречивые результаты между лабораториями."
        if gaps:
            summary += " Также обнаружены пробелы в покрытии данных."
        return summary

    def _find_contradictions(self, facts: list[Fact]) -> list[str]:
        groups: dict[tuple[str, str], set[str]] = {}
        labs: dict[tuple[str, str], set[str]] = {}
        temps: dict[tuple[str, str], list[float]] = {}
        for fact in facts:
            key = (fact.material, fact.property)
            groups.setdefault(key, set()).add(fact.effect_direction)
            labs.setdefault(key, set()).add(fact.lab)
            if fact.temperature_c is not None:
                temps.setdefault(key, []).append(fact.temperature_c)
        contradictions = []
        for (material, property_name), directions in groups.items():
            if "increase" in directions and "decrease" in directions:
                temp_values = temps.get((material, property_name), [])
                temp_text = (
                    f"{min(temp_values):g}-{max(temp_values):g} °C"
                    if temp_values and min(temp_values) != max(temp_values)
                    else f"{temp_values[0]:g} °C"
                    if temp_values
                    else "без указанной температуры"
                )
                contradictions.append(
                    f"{material}, {property_name}, {temp_text}: разные источники показывают рост и снижение; лаборатории: {', '.join(sorted(labs[(material, property_name)]))}."
                )
        return contradictions

    def _find_gaps(self, parsed, facts: list[Fact]) -> list[str]:
        gaps: list[str] = []
        if not facts:
            return ["Нет подтвержденных фактов для выбранной комбинации материал - режим - свойство."]
        labs = {fact.lab for fact in facts}
        if len(labs) < 2:
            gaps.append("Результаты подтверждены менее чем двумя независимыми лабораториями.")
        durations = {fact.duration_h for fact in facts if fact.duration_h is not None}
        if len(durations) < 2:
            gaps.append("Недостаточно данных для сравнения влияния длительности режима.")
        temps = sorted(fact.temperature_c for fact in facts if fact.temperature_c is not None)
        if parsed.temperature_min is not None and parsed.temperature_max is not None:
            if not any(parsed.temperature_min <= temp <= parsed.temperature_max for temp in temps):
                gaps.append(f"Нет экспериментов в диапазоне {parsed.temperature_min:g}-{parsed.temperature_max:g} °C.")
            if temps and (min(temps) > parsed.temperature_min or max(temps) < parsed.temperature_max):
                gaps.append("Диапазон температур покрыт не полностью внутри заданных границ.")
        if not any(fact.equipment for fact in facts):
            gaps.append("Для найденных экспериментов не указано оборудование.")
        return gaps


def _direction_ru(value: str) -> str:
    return {"increase": "рост", "decrease": "снижение", "neutral": "без изменений"}.get(value, value)
