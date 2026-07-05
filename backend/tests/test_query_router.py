from __future__ import annotations

import unittest

from app.pipeline.providers import RemoteEmbeddingProvider
from app.pipeline.query import QueryOrchestrator
from app.schemas import GraphPayload


class Normalizer:
    aliases = {
        "электроэкстракция": "электроэкстракция",
        "electrowinning": "электроэкстракция",
        "пвп": "взвешенная плавка",
        "католит": "католит",
    }

    def normalize_entity(self, value):
        return value


class Store:
    graph_sink = None

    def __init__(self):
        self.facts = {}
        self.candidates = {}
        self.documents = {}
        self.normalizer = Normalizer()

    def search(self, query, top_k=10):
        return []

    def get_graph(self, facts=None, entity_id=None):
        return GraphPayload()


class OfftopicRouterTest(unittest.TestCase):
    def setUp(self):
        # Парсер-бомба: роутер не должен доходить до LLM на оффтопе
        class Parser:
            async def parse_question(self, question):
                raise AssertionError("LLM не должна вызываться для оффтопа")

        self.orchestrator = QueryOrchestrator(Store(), Parser())

    def test_smalltalk_is_offtopic(self):
        for question in ["как дела?", "привет", "ты кто?", "", "спасибо!"]:
            self.assertTrue(self.orchestrator.is_offtopic(question), question)

    def test_domain_terms_pass(self):
        for question in [
            "что известно про электроэкстракцию?",  # словарь синонимов
            "как менялась твёрдость сплава?",  # стем «сплав»
            "оптимальная скорость циркуляции католита",  # словарь + стем
            "режимы ПВП",  # короткая аббревиатура из словаря
        ]:
            self.assertFalse(self.orchestrator.is_offtopic(question), question)

    def test_digits_and_long_questions_pass(self):
        self.assertFalse(self.orchestrator.is_offtopic("что было при 700 градусах?"))
        self.assertFalse(
            self.orchestrator.is_offtopic(
                "подскажи пожалуйста, есть ли у нас хоть что-нибудь про то, "
                "чем занимались соседние команды в прошлом году"
            )
        )

    def test_offtopic_response_is_instant_and_marked(self):
        response = self.orchestrator.offtopic_response()
        self.assertTrue(response.offtopic)
        self.assertEqual(response.evidence_status, "none")
        self.assertEqual(response.experiments, [])
        self.assertIn("базе знаний", response.summary)


class EmbeddingCacheTest(unittest.TestCase):
    def test_repeated_texts_hit_cache(self):
        provider = RemoteEmbeddingProvider("http://embeddings.invalid/embed")
        calls: list[list[str]] = []

        def fake_fetch(texts, kind):
            calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

        provider._fetch = fake_fetch
        first = provider.embed(["текст один", "текст два"])
        second = provider.embed(["текст один", "текст два"])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_query_and_doc_modes_cached_separately(self):
        provider = RemoteEmbeddingProvider("http://embeddings.invalid/embed")
        calls: list[str] = []

        def fake_fetch(texts, kind):
            calls.append(kind)
            return [[0.5] for _ in texts]

        provider._fetch = fake_fetch
        provider.embed(["вопрос"])
        provider.embed_query(["вопрос"])
        self.assertEqual(calls, ["doc", "query"])

    def test_partial_cache_fetches_only_misses(self):
        provider = RemoteEmbeddingProvider("http://embeddings.invalid/embed")
        calls: list[list[str]] = []

        def fake_fetch(texts, kind):
            calls.append(list(texts))
            return [[1.0] for _ in texts]

        provider._fetch = fake_fetch
        provider.embed(["a"])
        provider.embed(["a", "b"])
        self.assertEqual(calls, [["a"], ["b"]])

    def test_length_mismatch_raises(self):
        provider = RemoteEmbeddingProvider("http://embeddings.invalid/embed")
        provider._fetch = lambda texts, kind: [[1.0]]
        with self.assertRaises(ValueError):
            provider.embed(["a", "b"])


if __name__ == "__main__":
    unittest.main()
