"""Локальные эмбеддинги bge-m3 (sentence-transformers), кросс-язычные RU/EN.

Модель фиксирована в коде намеренно: индекс векторов несовместим между моделями,
смена модели требует пересчёта всей базы (см. vector(1024) в схеме БД).
Веса скачиваются при первом вызове и кэшируются в volume.
"""
import asyncio
import logging
import threading

log = logging.getLogger(__name__)

# Публичная: /embed отдаёт имя модели в ответе (main.py)
MODEL_NAME = "BAAI/bge-m3"
_model = None
# _get_model работает в пуле потоков (asyncio.to_thread): без блокировки два
# конкурентных первых /embed загрузили бы модель (~2.3 ГБ) дважды
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                log.info("Загрузка модели эмбеддингов %s (первый запуск скачивает веса)", MODEL_NAME)
                _model = SentenceTransformer(MODEL_NAME)
    return _model


async def embed(texts: list[str], kind: str = "doc") -> list[list[float]]:
    """kind сохранён для совместимости контракта: bge-m3 не различает документы и запросы."""
    model = await asyncio.to_thread(_get_model)
    vectors = await asyncio.to_thread(model.encode, texts, normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]
