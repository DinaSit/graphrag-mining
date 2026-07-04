from __future__ import annotations

import unittest
from unittest.mock import patch

from app import web_search


class WebSearchTest(unittest.TestCase):
    def test_search_uses_simplified_query_first(self) -> None:
        queries: list[str] = []

        def fake_query(query: str, max_results: int):
            queries.append(query)
            return [{"href": "https://patents.google.com/patent/test", "title": "x", "body": "y"}]

        with patch("app.web_search._query_hits", side_effect=fake_query):
            hits = web_search._search("Какие способы закачки шахтных вод применялись в России?")

        self.assertTrue(hits)
        self.assertTrue(queries)
        self.assertIn("закачки", queries[0])
        self.assertLess(len(queries[0]), 350)


if __name__ == "__main__":
    unittest.main()
