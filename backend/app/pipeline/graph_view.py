"""Проекция фактов в граф для ответа (контракт узлов КГ).

Строит GraphPayload, который уходит в интерфейс: узлы Claim/Material/Process/
Equipment/Document и рёбра между ними. Это представление для чтения — граф
в Neo4j пишет persistence, здесь он не изменяется.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.pipeline.normalization import (
    canonical_text,
    clean_extracted,
    direction_label,
    is_junk_label,
    normalize_effect_direction,
    slug,
)
from app.schemas import Fact, GraphEdge, GraphNode, GraphPayload, SourceRef

if TYPE_CHECKING:
    from app.storage import ApplicationStore

# Семантические сущности, попадающие в ответный граф КГ: только эти типы
# (Material/Property/Effect/Laboratory/SourceFragment отдельными узлами нет)
GRAPH_SEMANTIC_TYPES = frozenset({"Process", "Equipment", "Condition"})


def build_graph(store: ApplicationStore, facts: list[Fact] | None = None) -> GraphPayload:
    # Без явного списка граф строится только по видимым фактам:
    # скрытые документы не попадают в визуализацию
    selected = facts if facts is not None else store.visible_facts()
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    # Дедуп узлов по (тип, каноническая подпись): «медь» из пяти фактов —
    # один узел; ключ → стабильный id
    node_ids: dict[tuple[str, str], str] = {}

    def node(node_type: str, label: str, **data: Any) -> str | None:
        """Создаёт (или переиспользует) узел; для подписи-заглушки узел
        не создаётся. Возвращает id узла или None, если подпись — заглушка."""
        if is_junk_label(label):
            return None
        key = (node_type, canonical_text(label))
        existing = node_ids.get(key)
        if existing is not None:
            return existing
        base_id = f"{node_type.lower()}-{slug(label) or len(node_ids)}"
        # Разные подписи с одинаковым slug не должны получать общий id узла
        node_id = base_id
        suffix = 2
        while node_id in nodes:
            node_id = f"{base_id}-{suffix}"
            suffix += 1
        node_ids[key] = node_id
        nodes[node_id] = GraphNode(id=node_id, label=label, type=node_type, data=data)
        return node_id

    def edge(source: str, target: str, label: str) -> None:
        edge_id = f"{source}-{label}-{target}"
        edges.setdefault(edge_id, GraphEdge(id=edge_id, source=source, target=target, label=label))

    for fact in selected:
        # Claim: id факта как узел-id (дедуп по id, а не по подписи —
        # разные факты с одинаковой подписью остаются разными утверждениями)
        claim_id = fact.id
        if claim_id not in nodes:
            nodes[claim_id] = GraphNode(
                id=claim_id, label=claim_label(fact), type="Claim",
                data={"confidence": fact.confidence, "title": claim_title(fact)},
            )
        # Material → Claim
        material_id = node("Material", fact.material)
        if material_id is not None:
            edge(material_id, claim_id, "ABOUT")
        # Process → Claim
        process_id = node("Process", fact.process)
        if process_id is not None:
            edge(process_id, claim_id, "USED_IN")
        # Equipment → Claim
        equipment_id = node("Equipment", fact.equipment or "")
        if equipment_id is not None:
            edge(equipment_id, claim_id, "USED_IN")
        # Семантические сущности из payload (Process/Equipment/Condition) → Claim
        for entity_type, entity_name in _semantic_entities(store, fact):
            entity_id = node(entity_type, entity_name)
            if entity_id is not None:
                edge(entity_id, claim_id, "MENTIONS")
        # Claim → Document (один узел на документ, а не на фрагмент;
        # дедуп по document_id — одинаковые короткие имена не сливаются)
        document_id, document_label = _document_node(store, fact.source)
        doc_node_id = f"document-{document_id}"
        if doc_node_id not in nodes:
            nodes[doc_node_id] = GraphNode(
                id=doc_node_id, label=document_label, type="Document",
                data={"document_id": document_id},
            )
        edge(claim_id, doc_node_id, "CITES")
    return GraphPayload(nodes=list(nodes.values()), edges=list(edges.values()))


def _semantic_entities(store: ApplicationStore, fact: Fact) -> list[tuple[str, str]]:
    """Process/Equipment/Condition с чистыми именами из payload кандидата факта.
    Прочие типы (Material/Property/…) в ответный граф отдельными узлами не идут."""
    if not fact.candidate_id:
        return []
    candidate = store.candidates.get(fact.candidate_id)
    if candidate is None:
        return []
    result: list[tuple[str, str]] = []
    for item in candidate.payload.get("entities", []):
        if not isinstance(item, dict):
            continue
        entity_type = item.get("type")
        if entity_type not in GRAPH_SEMANTIC_TYPES:
            continue
        name = clean_extracted(store.normalizer.normalize_entity(clean_extracted(item.get("name"))))
        if name:
            result.append((entity_type, name))
    return result


def _document_node(store: ApplicationStore, source: SourceRef) -> tuple[str, str]:
    """Один узел Document на документ: короткое имя файла без расширения (~20 симв.)."""
    document = store.documents.get(source.document_id)
    if document is None or not document.filename:
        return source.document_id, source.document_id
    name = document.filename.rsplit(".", 1)[0].strip()
    if len(name) > 20:
        name = name[:20].rstrip() + "…"
    return source.document_id, name


def _effect_label(fact: Fact) -> str:
    value = f" {fact.effect_value:g}{fact.effect_unit or ''}" if fact.effect_value is not None else ""
    return f"{direction_label(fact.effect_direction)}{value}"


def claim_label(fact: Fact) -> str:
    """Короткая подпись узла Claim — суть утверждения: "<property>: <направление>";
    без извлечённого направления — только property (без завершающего двоеточия);
    если property не извлечён — начало цитаты источника."""
    if not is_junk_label(fact.property):
        direction = direction_label(normalize_effect_direction(fact.effect_direction))
        if direction and direction != "unknown":
            return f"{fact.property}: {direction}"
        return fact.property
    quote = (fact.source.quote or "").strip()
    if quote:
        return quote[:40] + ("…" if len(quote) > 40 else "")
    return fact.id


def claim_title(fact: Fact) -> str:
    """Полное описание утверждения для data.title узла Claim (тултип UI)."""
    context = ", ".join(part for part in (fact.material, fact.process) if not is_junk_label(part))
    if not is_junk_label(fact.property):
        effect = _effect_label(fact).strip()
        body = f"{fact.property}: {effect}" if effect else fact.property
    else:
        body = (fact.source.quote or "").strip() or fact.id
    return f"{context} — {body}" if context else body
