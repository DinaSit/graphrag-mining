"""Локальные эмбеддинги bge-m3 (sentence-transformers), кросс-язычные RU/EN.

Модель зашита намеренно: индекс векторов несовместим между моделями,
смена модели требует пересчёта всей базы (см. vector(1024) в схеме БД).
Веса скачиваются при первом вызове и кэшируются в volume.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

_MODEL_NAME = "BAAI/bge-m3"
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        log.info("Загрузка модели эмбеддингов %s (первый запуск скачивает веса)", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


async def embed(texts: list[str], kind: str = "doc") -> list[list[float]]:
    """kind сохранён для совместимости контракта: bge-m3 не различает документы и запросы."""
    model = await asyncio.to_thread(_get_model)
    vectors = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]


MODEL_NAME = _MODEL_NAME
