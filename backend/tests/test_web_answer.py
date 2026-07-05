from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.main import ask
from app.schemas import GraphPayload, QueryRequest, QueryResponse


class WebAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_ask_marks_web_timeout_without_failing_answer(self) -> None:
        response = QueryResponse(
            summary="нет прямых фактов",
            experiments=[],
            sources=[],
            graph=GraphPayload(),
            contradictions=[],
            gaps=[],
            confidence=0,
            has_direct_facts=False,
            evidence_status="none",
        )

        old_url = os.environ.get("WEB_ANSWER_URL")
        old_timeout = os.environ.get("WEB_ANSWER_TIMEOUT")
        os.environ["WEB_ANSWER_URL"] = "http://ml/web_answer"
        os.environ["WEB_ANSWER_TIMEOUT"] = "0.1"
        try:
            with patch("app.main.orchestrator.answer", AsyncMock(return_value=response)):
                with patch("app.main.httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
                    # Вопрос доменный: оффтоп-роутер /ask не должен срезать веб-ступень
                    result = await ask(QueryRequest(question="какие методы очистки шахтных вод существуют?"))
        finally:
            if old_url is None:
                os.environ.pop("WEB_ANSWER_URL", None)
            else:
                os.environ["WEB_ANSWER_URL"] = old_url
            if old_timeout is None:
                os.environ.pop("WEB_ANSWER_TIMEOUT", None)
            else:
                os.environ["WEB_ANSWER_TIMEOUT"] = old_timeout

        self.assertIsNotNone(result.web_answer)
        self.assertIn("таймаут", result.web_answer["llm_error"])


if __name__ == "__main__":
    unittest.main()
