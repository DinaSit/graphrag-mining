"""Обход графа знаний по плану вопроса и подтверждение числовых условий.

Три Cypher-шаблона (сущности, числовые параметры, регион) дают множество
id Claim-узлов. Значения параметров в графе не хранятся — только имена,
поэтому числовые диапазоны проверяются в Python по payload кандидатов.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.pipeline.normalization import canonical_text
from app.pipeline.validation import normalize_for_quantity, normalize_quantity
from app.schemas import CandidateStatus, ParsedQuestion

if TYPE_CHECKING:
    from app.storage import ApplicationStore

GRAPH_LIMIT = 40


def graph_traverse(store: ApplicationStore, parsed: ParsedQuestion) -> set[str]:
    """Обход Neo4j по плану вопроса тремя Cypher-шаблонами (сущности,
    числовые параметры, регион): объединение id найденных Claim-узлов.
    Без подключённого графового стока — пустое множество."""
    if not store.graph_sink or not store.graph_sink.enabled:
        return set()
    claim_ids: set[str] = set()

    terms = [e.name for e in parsed.entities]
    for value in (parsed.material, parsed.process, parsed.equipment):
        if value:
            terms.append(value)
    if terms:
        claim_ids |= _template_entity_neighbors(store, terms)

    for condition in parsed.conditions + ([parsed.target] if parsed.target else []):
        claim_ids |= _template_numeric_parameter(store, condition.parameter)

    if parsed.region:
        claim_ids |= _template_region(store, parsed.region)

    return claim_ids


def _template_entity_neighbors(store: ApplicationStore, terms: list[str]) -> set[str]:
    """Шаблон 1: сущность (любого типа онтологии) -> Claim'ы, которые её упоминают."""
    query = """
    UNWIND $terms AS term
    MATCH (n) WHERE toLower(n.name) CONTAINS toLower(term)
    MATCH (c:Claim)-[:MENTIONS]->(n)
    RETURN DISTINCT c.id AS claim_id
    LIMIT $limit
    """
    rows = store.graph_sink.run_read(query, {"terms": terms, "limit": GRAPH_LIMIT})
    return {row["claim_id"] for row in rows if row.get("claim_id")}


def _template_numeric_parameter(store: ApplicationStore, parameter_name: str) -> set[str]:
    """Шаблон 2: NumericParameter/Condition по имени -> связанные Experiment/Claim.

    Само значение параметра в узле графа сейчас не хранится (только name),
    поэтому фильтрация по value_min/value_max делается позже в Python
    по Fact/candidate.payload — см. numeric_condition_matches.
    """
    query = """
    MATCH (p) WHERE (p:NumericParameter OR p:Condition) AND toLower(p.name) CONTAINS toLower($parameter)
    OPTIONAL MATCH (c:Claim)-[:MENTIONS]->(p)
    OPTIONAL MATCH (p)<-[:measured_parameter|operates_at_condition]-(e:Experiment)<-[:BASED_ON]-(c2:Claim)
    RETURN DISTINCT c.id AS claim_id, c2.id AS claim_id_2
    LIMIT $limit
    """
    rows = store.graph_sink.run_read(query, {"parameter": parameter_name, "limit": GRAPH_LIMIT})
    result: set[str] = set()
    for row in rows:
        if row.get("claim_id"):
            result.add(row["claim_id"])
        if row.get("claim_id_2"):
            result.add(row["claim_id_2"])
    return result


# «Зарубежная практика» — не название страны, а всё, что не Россия: вопрос
# «в России и за рубежом» иначе не разложить на регионы
_FOREIGN_MARKERS = ("рубеж", "мировой практик", "мировая практик", "иностран", "abroad", "foreign")
_DOMESTIC_MARKERS = ("отечествен", "росси", "рф")


def _region_names(store: ApplicationStore, region_name: str) -> list[str] | None:
    """Список регионов, которые имел в виду вопрос. None — искать по вхождению
    названия (обычная страна)."""
    folded = canonical_text(region_name)
    index = store.normalizer.regions
    if any(marker in folded for marker in _FOREIGN_MARKERS):
        return index.foreign
    if any(marker in folded for marker in _DOMESTIC_MARKERS):
        return index.domestic
    return None


def _template_region(store: ApplicationStore, region_name: str) -> set[str]:
    """Шаблон 3: Region -> Claim'ы, упоминающие решения/публикации в этом регионе."""
    names = _region_names(store, region_name)
    if names is not None:
        query = """
        MATCH (c:Claim)-[:MENTIONS]->(r:Region) WHERE r.name IN $names
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        params: dict[str, Any] = {"names": names, "limit": GRAPH_LIMIT}
    else:
        query = """
        MATCH (r:Region) WHERE toLower(r.name) CONTAINS toLower($region)
        MATCH (c:Claim)-[:MENTIONS]->(r)
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        params = {"region": region_name, "limit": GRAPH_LIMIT}
    rows = store.graph_sink.run_read(query, params)
    return {row["claim_id"] for row in rows if row.get("claim_id")}


def numeric_condition_matches(
    store: ApplicationStore, parsed: ParsedQuestion, claim_ids: set[str]
) -> list[dict[str, Any]]:
    if not parsed.conditions and not parsed.target:
        return []
    wanted = list(parsed.conditions) + ([parsed.target] if parsed.target else [])
    matches: list[dict[str, Any]] = []
    mapped = [
        store.candidates[f"candidate-{cid.replace('claim-', '')}"] for cid in claim_ids
        if f"candidate-{cid.replace('claim-', '')}" in store.candidates
    ]
    # Числовые условия подтверждаются только утверждёнными кандидатами:
    # rejected/pending не могут становиться доказательствами;
    # кандидаты скрытых документов не участвуют
    hidden = store.hidden_document_ids()
    candidate_pool = [
        candidate for candidate in (mapped or list(store.candidates.values()))
        if candidate.status == CandidateStatus.approved
        and (candidate.source is None or candidate.source.document_id not in hidden)
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
                if value_in_range(item, condition):
                    matches.append({"candidate_id": candidate.id, "source": candidate.source, "parameter": item})
    return matches


def value_in_range(item: dict[str, Any], condition) -> bool:
    """Пересекается ли значение/диапазон параметра кандидата с условием вопроса.

    Fail-open по замыслу: без чисел или с неконвертируемыми значениями
    возвращается True — что нельзя проверить, то не отбраковывается.
    Обе стороны сравнения приводятся к базовой единице величины условия,
    если единица распознана (normalize_for_quantity).
    """
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
