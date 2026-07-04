from __future__ import annotations

import unittest
from pathlib import Path

from app.pipeline.query import QueryOrchestrator
from app.schemas import CandidateStatus, ExtractionCandidate, QueryCondition, SourceFragment, SourceRef
from app.storage import ApplicationStore
from app.pipeline.validation import load_validation_rules, validate_candidate_numbers


ROOT = Path(__file__).resolve().parents[2]
SOURCE = SourceRef(document_id="doc-1", version_id="doc-1-v1", fragment_id="fragment-1", quote="Температура 80 °C.")


class QualityGateTest(unittest.TestCase):
    def test_missing_material_is_not_auto_approved(self) -> None:
        store = ApplicationStore(ROOT / "domain" / "default")
        store.fragments[SOURCE.fragment_id] = SourceFragment(
            id=SOURCE.fragment_id,
            document_id=SOURCE.document_id,
            version_id=SOURCE.version_id,
            text=SOURCE.quote or "",
            normalized_text=(SOURCE.quote or "").lower(),
        )
        candidate = ExtractionCandidate(
            id="candidate-1",
            confidence=0.99,
            source=SOURCE,
            payload={
                "material": "не указано",
                "property": "сульфаты",
                "process": "очистка",
            },
        )

        result = store.add_candidate(candidate)

        self.assertEqual(result.status, CandidateStatus.pending_review)
        self.assertIn("material", result.review_note or "")
        self.assertEqual(store.facts, {})

    def test_numeric_parameter_must_be_present_in_source(self) -> None:
        result = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "температура", "value": 95, "unit": "°C"}]},
            "Температура процесса составляла 80 °C.",
            {"ranges_by_name": {"температура": (0, 1600)}, "ranges_by_quantity": {"temperature": (0, 1600)}},
        )

        self.assertFalse(result["validated"])
        self.assertTrue(result["issues"])

    def test_named_numeric_parameters_are_validated_in_rule_units(self) -> None:
        rules = load_validation_rules(ROOT / "domain" / "default")

        concentration = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "сухой остаток", "value": 10, "unit": "г/л"}]},
            "Сухой остаток составлял 10 г/л.",
            rules,
        )
        head = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "гидростатический напор", "value": 1200, "unit": "мм"}]},
            "Гидростатический напор составлял 1200 мм.",
            rules,
        )

        self.assertTrue(concentration["validated"], concentration["issues"])
        self.assertTrue(head["validated"], head["issues"])

    def test_conflict_detection_normalizes_direction_aliases(self) -> None:
        store = ApplicationStore(ROOT / "domain" / "default")
        first_source = SOURCE
        second_source = SourceRef(
            document_id="doc-1",
            version_id="doc-1-v1",
            fragment_id="fragment-2",
            quote="Сульфаты снизились после очистки.",
        )
        for source in (first_source, second_source):
            store.fragments[source.fragment_id] = SourceFragment(
                id=source.fragment_id,
                document_id=source.document_id,
                version_id=source.version_id,
                text=source.quote or "",
                normalized_text=(source.quote or "").lower(),
            )

        first = store.add_candidate(
            ExtractionCandidate(
                id="candidate-10",
                confidence=0.99,
                source=first_source,
                payload={
                    "material": "шахтные воды",
                    "property": "сульфаты",
                    "process": "очистка",
                    "effect_direction": "рост",
                },
            )
        )
        second = store.add_candidate(
            ExtractionCandidate(
                id="candidate-11",
                confidence=0.99,
                source=second_source,
                payload={
                    "material": "шахтные воды",
                    "property": "сульфаты",
                    "process": "очистка",
                    "effect_direction": "decrease",
                },
            )
        )

        self.assertEqual(first.status, CandidateStatus.approved)
        self.assertEqual(second.status, CandidateStatus.approved)
        self.assertEqual(store.facts["claim-10"].status, "conflicting")
        self.assertEqual(store.facts["claim-11"].status, "conflicting")

    def test_query_numeric_range_matches_compatible_units(self) -> None:
        condition = QueryCondition(parameter="сухой остаток", value_min=1, value_max=1, unit="г/л")

        self.assertTrue(
            QueryOrchestrator._value_in_range({"value": 1000, "unit": "мг/л"}, condition)
        )
        self.assertFalse(
            QueryOrchestrator._value_in_range({"value": 1500, "unit": "мг/л"}, condition)
        )


if __name__ == "__main__":
    unittest.main()
