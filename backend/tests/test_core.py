from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.file_storage import StoredObject
from app.pipeline.parsers import choose_parser
from app.pipeline.providers import MockLLMProvider
from app.pipeline.query import QueryOrchestrator
from app.pipeline.sample_data import seed_sample_data
from app.schemas import ExtractionCandidate, QueryRequest
from app.storage import ApplicationStore, SourceRequiredError

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    Workbook = None


class FakeFileStorage:
    enabled = True

    def put_document(
        self,
        document_id: str,
        version_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        return StoredObject(
            bucket="test-bucket",
            object_name=f"documents/{document_id}/{version_id}/{filename}",
            uri=f"s3://test-bucket/documents/{document_id}/{version_id}/{filename}",
        )


class CorePipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ApplicationStore(ROOT / "domain" / "default")
        seed_sample_data(self.store)
        self.orchestrator = QueryOrchestrator(self.store)

    def test_seed_creates_vertical_slice(self) -> None:
        self.assertEqual(len(self.store.documents), 12)
        self.assertGreaterEqual(len(self.store.facts), 30)
        self.assertGreaterEqual(len(self.store.fragment_vectors), len(self.store.fragments))

    def test_control_question_returns_sources_and_graph(self) -> None:
        response = self.orchestrator.answer(
            QueryRequest(question="Что делали со сплавом X при температуре 700-750 °C и как изменялась твёрдость?")
        )
        self.assertGreaterEqual(len(response.experiments), 2)
        self.assertTrue(response.sources)
        self.assertTrue(response.graph.nodes)
        self.assertIn("Сплав X", response.summary)

    def test_question_parser_extracts_temperature_range(self) -> None:
        parsed = MockLLMProvider().parse_question("Что делали со сплавом X при температуре 700-750 C?")
        self.assertEqual(parsed.material, "Сплав X")
        self.assertEqual(parsed.temperature_min, 700)
        self.assertEqual(parsed.temperature_max, 750)

    def test_fact_requires_source(self) -> None:
        candidate = ExtractionCandidate(id="candidate-no-source", payload={"material": "Сплав X"}, confidence=0.91)
        self.store.candidates[candidate.id] = candidate
        with self.assertRaises(SourceRequiredError):
            self.store.approve_candidate(candidate.id)

    def test_ingest_records_storage_uri(self) -> None:
        store = ApplicationStore(ROOT / "domain" / "default", file_storage=FakeFileStorage())
        document = store.ingest_document("storage-check.txt", b"Storage smoke test", "txt", "storage-check")
        self.assertEqual(document.storage_bucket, "test-bucket")
        self.assertTrue(document.storage_uri.startswith("s3://test-bucket/documents/"))

    def test_xlsx_parser_extracts_rows(self) -> None:
        if Workbook is None:
            self.skipTest("openpyxl is not installed")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Experiments"
        sheet.append(["material", "temperature_c", "property"])
        sheet.append(["Сплав X", 720, "твёрдость"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        parser = choose_parser("experiments.xlsx")
        fragments = parser.parse("doc-xlsx", "doc-xlsx-v1", "experiments.xlsx", buffer.getvalue())

        self.assertEqual(parser.name, "openpyxl")
        self.assertGreaterEqual(len(fragments), 2)
        self.assertIn("Сплав X", fragments[-1].text)


if __name__ == "__main__":
    unittest.main()
