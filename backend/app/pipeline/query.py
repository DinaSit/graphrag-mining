from __future__ import annotations

import asyncio
import json
import re
from itertools import combinations
from statistics import mean
from typing import Any

from app.pipeline.query_parsing import LLMQuestionParser
from app.pipeline.validation import normalize_for_quantity, normalize_quantity
from app.schemas import CandidateStatus, ExperimentRow, Fact, ParsedQuestion, QueryRequest, QueryResponse, SearchResponse
from app.storage import ApplicationStore

# LLM доступна из backend только по HTTP через сервис ml-extraction
from app.pipeline.llm_bridge import LLMUnavailableError, chat_json

GRAPH_LIMIT = 40

ANSWER_SYSTEM_PROMPT = """Ты формируешь ответ пользователю строго на основе предоставленного evidence pack.
Тебе ЗАПРЕЩЕНО использовать любые знания вне evidence pack. Если данных недостаточно — прямо скажи об этом.
Ответь JSON без пояснений:
{
  "summary": "2-4 предложения с ответом по существу, с указанием диапазонов значений и источников по id",
  "sufficient": true|false — хватает ли evidence pack для ПРЯМОГО ответа на вопрос (false, если данные лишь смежные или их нет),
  "confirmed": ["короткий подтверждённый вывод", ...],
  "contradictions": ["описание противоречия между источниками", ...],
  "gaps": ["чего не хватает в данных", ...],
  "hypotheses": ["гипотеза на основе косвенных данных, явно помеченная как непрямая", ...]
}
Правила цитирования (СТРОГО):
- ссылка на источник — ТОЛЬКО точный id фрагмента из evidence pack в квадратных скобках: [fragment-doc-xxxx-docx-p12];
- несколько источников — в одних скобках через запятую: [fragment-…, fragment-…];
- НИКОГДА не используй порядковые номера [1], [2], сокращённые или выдуманные id;
- каждый вывод в confirmed и каждое противоречие в contradictions заканчивай такой ссылкой."""


class QueryOrchestrator:
    # Стемы базовой R&D-лексики: словарь синонимов покрывает термины домена,
    # стемы добавляют морфологию и общеисследовательские слова, которых в нём нет
    _DOMAIN_STEMS = frozenset({
        "метод", "экспер", "исслед", "публика", "отчет", "лаборат", "температур",
        "концентрац", "скорост", "давлен", "расход", "материал", "процесс",
        "оборудован", "установк", "технолог", "параметр", "режим", "источник",
        "статья", "патент", "вывод", "эффект", "практик", "раствор", "очистк",
        "вода", "воды", "руда", "руды", "металл", "сплав", "шлак", "штейн",
    })

    def __init__(self, store: ApplicationStore, question_parser: LLMQuestionParser | None = None):
        self.store = store
        self.question_parser = question_parser or LLMQuestionParser(normalizer=store.normalizer)
        self._domain_terms: tuple[set[str], set[str]] | None = None

    def search(self, query: str, top_k: int = 8) -> SearchResponse:
        return SearchResponse(hits=self.store.search(query, top_k=top_k))

    def _domain_vocabulary(self) -> tuple[set[str], set[str]]:
        """Термины домена из словаря синонимов: (короткие — точное совпадение,
        5-буквенные префиксы длинных — матчат морфологические формы)."""
        if self._domain_terms is None:
            exact: set[str] = set()
            prefixes: set[str] = set()
            for alias, canonical in getattr(self.store.normalizer, "aliases", {}).items():
                for term in (alias, canonical):
                    for word in re.findall(r"[a-zа-я0-9]+", term.lower().replace("ё", "е")):
                        if len(word) <= 4:
                            exact.add(word)
                        else:
                            prefixes.add(word[:5])
            self._domain_terms = (exact, prefixes)
        return self._domain_terms

    def is_offtopic(self, question: str) -> bool:
        """Смолток и оффтоп («как дела?») не гоняются по полному пайплайну.

        Проверяется на границе API (/ask), а не внутри answer(): пайплайн
        остаётся полным для прямых вызовов. Эвристика ошибается только в
        безопасную сторону: цифры, единицы, длинный текст или любой доменный
        термин отправляют вопрос в пайплайн.
        """
        text = question.strip().lower().replace("ё", "е")
        if not text:
            return True
        if any(ch.isdigit() for ch in text):
            return False
        if len(text) > 80:
            return False
        exact, prefixes = self._domain_vocabulary()
        for token in re.findall(r"[a-zа-я0-9]+", text):
            if len(token) <= 4:
                if token in exact:
                    return False
            elif token[:5] in prefixes or any(token.startswith(stem) for stem in self._DOMAIN_STEMS):
                return False
        return True

    def offtopic_response(self) -> QueryResponse:
        empty_graph = self.store.get_graph(facts=[])
        return QueryResponse(
            summary=(
                "Вопрос не похож на запрос к базе знаний, поэтому полный поиск не запускался. "
                "Я отвечаю на вопросы по горно-металлургическим R&D-материалам: методы, материалы, "
                "процессы, параметры, эксперименты, источники. Например: «Какие методы обессоливания "
                "воды подходят, если сульфаты 200–300 мг/л, а требуемый сухой остаток ≤1000 мг/дм³?»"
            ),
            experiments=[],
            sources=[],
            graph=empty_graph,
            contradictions=[],
            gaps=[],
            confidence=0.0,
            evidence_status="none",
            offtopic=True,
        )

    async def answer(self, request: QueryRequest) -> QueryResponse:
        llm_errors: list[str] = []

        # Семантический поиск не зависит от LLM и стартует параллельно с разбором
        # вопроса: обе операции сетевые, последовательность здесь — чистая потеря
        # времени. store.search ходит за эмбеддингом блокирующим urllib-запросом,
        # поэтому выносится в поток — иначе зависший сервис эмбеддингов
        # останавливает весь event loop вместе с /health.
        search_task = asyncio.create_task(
            asyncio.to_thread(self.store.search, request.question, top_k=10)
        )

        # Отказ LLM на любой ступени не прячется: причина копится в llm_errors
        # и показывается пользователю, а ответ собирается из того, что доступно
        # без модели (граф, факты, семантический поиск).
        parsed: ParsedQuestion | None = None
        try:
            parsed = await self.question_parser.parse_question(request.question)
        except LLMUnavailableError as error:
            llm_errors.append(error.human())
        except Exception:
            search_task.cancel()
            raise

        facts: list[Fact] = []
        numeric_evidence: list[dict[str, Any]] = []
        claim_ids: set[str] = set()
        if parsed is not None:
            claim_ids = self._graph_traverse(parsed)
            graph_facts = [self.store.facts[cid] for cid in claim_ids if cid in self.store.facts]
            legacy_facts = self._filter_facts_legacy(list(self.store.facts.values()), request, parsed)
            facts = self._merge_unique(graph_facts, legacy_facts)
            facts = sorted(facts, key=self._rank_fact(parsed), reverse=True)
            numeric_evidence = self._numeric_condition_matches(parsed, claim_ids)

        try:
            search_hits = await search_task
        except Exception:
            search_hits = []
        for hit in search_hits:
            document = self.store.documents.get(hit.source.document_id)
            if document:
                hit.metadata = {**hit.metadata, "filename": document.filename}

        has_direct_facts = bool(facts or numeric_evidence)
        related_facts: list[Fact] = []
        hypotheses: list[str] = []
        if not has_direct_facts and parsed is not None:
            related_facts, hypotheses = self._indirect_search(parsed)

        experiments = [self._row_from_fact(fact) for fact in facts[:20]]
        sources = self._collect_sources(facts)
        contradictions = self._find_contradictions(facts)
        gaps = self._find_gaps(parsed, facts, numeric_evidence)
        graph = self.store.get_graph(facts=facts[:20])
        confidence = round(mean([fact.confidence for fact in facts]), 3) if facts else 0.0

        evidence_pack = self._build_evidence_pack(parsed, facts, numeric_evidence, search_hits, contradictions, gaps)
        llm_answer: dict[str, Any] = {}
        try:
            llm_answer = await self._generate_answer(request.question, evidence_pack)
        except LLMUnavailableError as error:
            llm_errors.append(error.human())

        summary = llm_answer.get("summary") or self._degraded_summary(
            facts, search_hits, has_direct_facts, bool(llm_errors)
        )
        if llm_answer.get("contradictions"):
            contradictions = list(dict.fromkeys(contradictions + llm_answer["contradictions"]))
        if llm_answer.get("gaps"):
            gaps = list(dict.fromkeys(gaps + llm_answer["gaps"]))
        if llm_answer.get("hypotheses"):
            hypotheses = list(dict.fromkeys(hypotheses + llm_answer["hypotheses"]))

        # Модель сама оценила, что прямого ответа в данных нет (факты смежные) —
        # включаем ступень веб-поиска, даже если формально факты нашлись
        if llm_answer.get("sufficient") is False:
            related_facts = self._merge_unique(related_facts, facts)
            facts = []
            experiments = []
            sources = []
            graph = self.store.get_graph(facts=[])
            confidence = 0.0
            has_direct_facts = False

        related_experiments = [self._row_from_fact(fact) for fact in related_facts[:20]]
        related_sources = self._collect_sources(related_facts)
        related_graph = self.store.get_graph(facts=related_facts[:20])
        evidence_status = "direct" if has_direct_facts else "partial" if related_facts or search_hits else "none"

        return QueryResponse(
            summary=summary,
            experiments=experiments,
            sources=sources[:12],
            graph=graph,
            contradictions=contradictions,
            gaps=gaps,
            confidence=confidence,
            hypotheses=hypotheses,
            llm_error="; ".join(dict.fromkeys(llm_errors)) or None,
            search_hits=search_hits[:8],
            has_direct_facts=has_direct_facts,
            related_experiments=related_experiments,
            related_sources=related_sources[:12],
            related_graph=related_graph,
            evidence_status=evidence_status,
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
        mapped = [
            self.store.candidates[f"candidate-{cid.replace('claim-', '')}"] for cid in claim_ids
            if f"candidate-{cid.replace('claim-', '')}" in self.store.candidates
        ]
        # Числовые условия подтверждаются только утверждёнными кандидатами:
        # rejected/pending не могут становиться доказательствами
        candidate_pool = [
            candidate for candidate in (mapped or list(self.store.candidates.values()))
            if candidate.status == CandidateStatus.approved
        ]
        for candidate in candidate_pool:
            payload = candidate.payload
            numeric_params = payload.get("numeric_parameters") or payload.get("parameters") or []
            for condition in wanted:
                for item in numeric_params:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("type") or item.get("parameter") or item.get("name") or "").lower()
                    # Безымянный параметр не считается совпадением:
                    # пустая строка — подстрока любого условия
                    if not name.strip():
                        continue
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
        quantity = normalize_quantity(1.0, condition.unit)[0] if condition.unit else None
        if quantity == "unknown":
            quantity = None
        item_unit = item.get("unit")
        if quantity is not None:
            if value_min is not None:
                converted = normalize_for_quantity(value_min, item_unit, quantity)
                value_min = converted[0] if converted is not None else value_min
            if value_max is not None:
                converted = normalize_for_quantity(value_max, item_unit, quantity)
                value_max = converted[0] if converted is not None else value_max
            if condition.value_min is not None:
                converted = normalize_for_quantity(condition.value_min, condition.unit, quantity)
                condition_min = converted[0] if converted is not None else condition.value_min
            else:
                condition_min = None
            if condition.value_max is not None:
                converted = normalize_for_quantity(condition.value_max, condition.unit, quantity)
                condition_max = converted[0] if converted is not None else condition.value_max
            else:
                condition_max = None
        else:
            condition_min = condition.value_min
            condition_max = condition.value_max
        if condition_min is not None and value_max is not None and value_max < condition_min:
            return False
        if condition_max is not None and value_min is not None and value_min > condition_max:
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
            # Снапшот: ingest-воркеры мутируют facts во время запроса
            partial = [f for f in list(self.store.facts.values()) if normalized.lower() in f.material.lower() or normalized.lower() in f.process.lower()]
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
        # Косвенные находки помечаются гипотезами (копии — базу не трогаем)
        unique = [f.model_copy(update={"is_hypothesis": True}) for f in unique]
        return unique, hypotheses

    def _build_evidence_pack(self, parsed, facts, numeric_evidence, search_hits, contradictions, gaps) -> dict[str, Any]:
        return {
            "question_plan": parsed.model_dump(mode="json") if parsed is not None else None,
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
        # Отказ LLM пробрасывается наверх (LLMUnavailableError) и показывается
        # пользователю; прочие сбои приводятся к тому же типу
        try:
            return await chat_json(
                messages=[
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(
                        {"question": question, "evidence_pack": evidence_pack}, ensure_ascii=False
                    )},
                ]
            )
        except LLMUnavailableError:
            raise
        except Exception as error:
            raise LLMUnavailableError("bad_response", str(error)) from error

    def _entity_key(self, value: str | None) -> str | None:
        """Ключ сравнения сущностей: канонизация нормалайзером + casefold + ё→е.

        Применяется к ОБЕИМ сторонам сравнения: «медный концентрат» из вопроса
        должен совпасть с «Медный концентрат» из документа и без synonyms.csv.
        """
        if not value:
            return None
        return _canonical_text(self.store.normalizer.normalize_entity(value) or value)

    def _filter_facts_legacy(self, facts: list[Fact], request: QueryRequest, parsed: ParsedQuestion) -> list[Fact]:
        confidence_min = max(request.confidence_min, request.filters.confidence_min)
        result: list[Fact] = []
        material_key = self._entity_key(parsed.material)
        property_key = self._entity_key(parsed.property)
        filter_materials = {self._entity_key(item) for item in request.filters.materials if item}
        filter_properties = {self._entity_key(item) for item in request.filters.properties if item}
        filter_labs = set(request.filters.laboratories)
        for fact in facts:
            if fact.confidence < confidence_min:
                continue
            if fact.is_hypothesis and not request.include_hypotheses:
                continue
            if material_key and self._entity_key(fact.material) != material_key:
                continue
            if property_key and self._entity_key(fact.property) != property_key:
                continue
            if parsed.temperature_min is not None and fact.temperature_c is not None and fact.temperature_c < parsed.temperature_min:
                continue
            if parsed.temperature_max is not None and fact.temperature_c is not None and fact.temperature_c > parsed.temperature_max:
                continue
            if filter_materials and self._entity_key(fact.material) not in filter_materials:
                continue
            if filter_properties and self._entity_key(fact.property) not in filter_properties:
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
        material_key = self._entity_key(parsed.material)
        property_key = self._entity_key(parsed.property)

        def rank(fact: Fact) -> float:
            score = fact.confidence
            if material_key and self._entity_key(fact.material) == material_key:
                score += 0.3
            if property_key and self._entity_key(fact.property) == property_key:
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

    def _degraded_summary(self, facts: list[Fact], search_hits, has_direct_facts: bool, llm_failed: bool) -> str:
        """Человекочитаемое summary без LLM: что реально нашлось в базе.

        Используется и когда LLM недоступна (llm_failed), и когда модель
        не вернула summary.
        """
        if has_direct_facts:
            body = f"В базе знаний найдено {len(facts)} факт(ов) по запросу — см. таблицу фактов и источники."
        elif facts:
            body = (f"Прямых фактов не найдено; есть {len(facts)} косвенных кейс(ов) по смежным понятиям — "
                    "см. гипотезы.")
        elif search_hits:
            top = [
                f"«{hit.metadata.get('filename', hit.source.document_id)}» — {hit.text[:160].strip()}…"
                for hit in search_hits[:3]
            ]
            body = (f"Фактов в графе не найдено, но семантический поиск дал {len(search_hits)} "
                    "релевантных фрагментов:\n- " + "\n- ".join(top))
        else:
            body = "В базе знаний ничего не найдено по этому запросу."
        if llm_failed:
            return "Ответ собран без языковой модели (см. причину выше). " + body
        return body

    def _find_contradictions(self, facts: list[Fact]) -> list[str]:
        groups: dict[tuple[str, str], list[Fact]] = {}
        labels: dict[tuple[str, str], tuple[str, str]] = {}
        for fact in facts:
            material = self.store.normalizer.normalize_entity(fact.material) or fact.material
            property_name = self.store.normalizer.normalize_entity(fact.property) or fact.property
            key = (_canonical_text(material), _canonical_text(property_name))
            labels.setdefault(key, (material, property_name))
            groups.setdefault(key, []).append(fact)
        contradictions: list[str] = []
        seen_messages: set[str] = set()
        for key, group in groups.items():
            for first, second in combinations(group, 2):
                directions = {_direction_key(first.effect_direction), _direction_key(second.effect_direction)}
                if directions != {"increase", "decrease"}:
                    continue
                if not _comparable_conditions(first, second):
                    continue
                material, property_name = labels[key]
                message = (
                    f"{material}, {property_name}: разные источники показывают противоположный эффект "
                    f"при сопоставимых условиях; лаборатории: {', '.join(sorted({first.lab, second.lab}))}."
                )
                if message not in seen_messages:
                    seen_messages.add(message)
                    contradictions.append(message)
        return contradictions

    def _find_gaps(self, parsed, facts: list[Fact], numeric_evidence: list[dict[str, Any]]) -> list[str]:
        gaps: list[str] = []
        if not facts and not numeric_evidence:
            return ["Нет подтверждённых фактов для заданной комбинации условий."]
        if facts:
            labs = {fact.lab for fact in facts}
            if len(labs) < 2:
                gaps.append("Результаты подтверждены менее чем двумя независимыми источниками.")
        # Факты по теме могут найтись, а численное подтверждение целевого
        # показателя — нет: это и есть пробел
        if parsed is not None and parsed.target and not numeric_evidence:
            gaps.append(f"Нет данных, напрямую подтверждающих целевой показатель «{parsed.target.parameter}».")
        return gaps


def _direction_ru(value: str) -> str:
    return {"increase": "рост", "decrease": "снижение", "neutral": "без изменений"}.get(value, value)


def _direction_key(value: str) -> str:
    text = str(value or "unknown").strip().lower().replace("ё", "е")
    return {
        "рост": "increase",
        "увеличение": "increase",
        "повышение": "increase",
        "снижение": "decrease",
        "уменьшение": "decrease",
        "падение": "decrease",
        "без изменений": "neutral",
        "нет изменений": "neutral",
    }.get(text, text)


def _canonical_text(value: str) -> str:
    return " ".join(value.strip().casefold().replace("ё", "е").split())


def _comparable_conditions(first: Fact, second: Fact, temperature_tolerance_c: float = 5.0) -> bool:
    """Противоположные эффекты при разных условиях — не противоречие:
    рост твёрдости при 705 °C и падение при 790 °C физически согласованы
    (пик старения). Неуказанная температура считается «любой» и пересекается
    со всем; разные процессы делают пару несопоставимой.
    """
    if first.process and second.process and _canonical_text(first.process) != _canonical_text(second.process):
        return False
    if (
        first.temperature_c is not None
        and second.temperature_c is not None
        and abs(first.temperature_c - second.temperature_c) > temperature_tolerance_c
    ):
        return False
    return True
