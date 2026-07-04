from __future__ import annotations

import unittest

from app.pipeline.query import QueryOrchestrator
from app.schemas import Fact, GraphPayload, ParsedQuestion, QueryRequest, SourceRef


SOURCE = SourceRef(document_id="doc-1", version_id="doc-1-v1", fragment_id="fragment-1", quote="source")


def fact(fact_id: str, material: str, property_name: str = "твёрдость") -> Fact:
    return Fact(
        id=fact_id,
        candidate_id=f"candidate-{fact_id}",
        material=material,
        material_id=f"material-{material}",
        experiment_id=f"exp-{fact_id}",
        sample="sample",
        process="термообработка",
        property=property_name,
        effect_direction="increase",
        effect_value=5,
        effect_unit="%",
        lab="lab",
        team="team",
        confidence=0.9,
        source=SOURCE,
    )


class Normalizer:
    def normalize_entity(self, value: str | None) -> str | None:
        return value


class Parser:
    def __init__(self, parsed: ParsedQuestion):
        self.parsed = parsed

    async def parse_question(self, question: str) -> ParsedQuestion:
        return self.parsed


class Store:
    graph_sink = None

    def __init__(self, facts: list[Fact]):
        self.facts = {item.id: item for item in facts}
        self.candidates = {}
        self.documents = {}
        self.normalizer = Normalizer()

    def search(self, query: str, top_k: int = 10):
        return []

    def get_graph(self, facts=None, entity_id=None):
        return GraphPayload()


class Orchestrator(QueryOrchestrator):
    def __init__(self, store: Store, parsed: ParsedQuestion, answer: dict):
        super().__init__(store, Parser(parsed))
        self.llm_answer = answer

    async def _generate_answer(self, question: str, evidence_pack: dict):
        return self.llm_answer


class QueryContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_facts_stay_in_direct_block(self) -> None:
        parsed = ParsedQuestion(material="Сплав X", property="твёрдость")
        orchestrator = Orchestrator(
            Store([fact("claim-1", "Сплав X")]),
            parsed,
            {"summary": "ok", "sufficient": True},
        )

        response = await orchestrator.answer(QueryRequest(question="q"))

        self.assertTrue(response.has_direct_facts)
        self.assertEqual(response.evidence_status, "direct")
        self.assertEqual(len(response.experiments), 1)
        self.assertEqual(response.related_experiments, [])

    async def test_insufficient_answer_moves_facts_to_related_block(self) -> None:
        parsed = ParsedQuestion(material="Сплав X", property="твёрдость")
        orchestrator = Orchestrator(
            Store([fact("claim-1", "Сплав X")]),
            parsed,
            {"summary": "not enough", "sufficient": False},
        )

        response = await orchestrator.answer(QueryRequest(question="q"))

        self.assertFalse(response.has_direct_facts)
        self.assertEqual(response.evidence_status, "partial")
        self.assertEqual(response.experiments, [])
        self.assertEqual(len(response.related_experiments), 1)

    async def test_indirect_search_uses_related_block_only(self) -> None:
        parsed = ParsedQuestion(material="никель", property="твёрдость")
        orchestrator = Orchestrator(
            Store([fact("claim-1", "никель-содержащий сплав")]),
            parsed,
            {"summary": "related only", "sufficient": False},
        )

        response = await orchestrator.answer(QueryRequest(question="q"))

        self.assertFalse(response.has_direct_facts)
        self.assertEqual(response.experiments, [])
        self.assertEqual(len(response.related_experiments), 1)


if __name__ == "__main__":
    unittest.main()
