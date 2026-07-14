"""«Знаете ли вы?» — LLM-формулировка факта для карточки на главной.

Механическая сборка фразы в UI («…что кристаллы: «размеры кристаллов» —
снижение?») читается плохо, поэтому teaser и вопрос для поиска формулирует
LLM. Главная страница НЕ ждёт LLM: GET /facts/random отдаёт готовую
формулировку из кэша, а отсутствующую генерирует фоновой задачей — следующий
показ этого факта использует готовую формулировку. Факты неизменяемы, поэтому
кэш — обычный dict без TTL; при сбое LLM запись в кэш не производится (будет
новая попытка при следующем показе факта).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

from app.pipeline.llm_bridge import LLMUnavailableError, chat_json
from app.pipeline.normalization import direction_label
from app.schemas import Fact

log = logging.getLogger(__name__)

# Насыщение пула: после того как сформулировано указанное число фактов,
# дополнительный прогрев при обращениях к рубрике прекращается — далее почти
# все обращения обслуживаются из кэша
_WARM_POOL_TARGET = 20

_SYSTEM_PROMPT = """Ты редактор рубрики «Знаете ли вы?» о металлургии и переработке руд.
По полям факта верни СТРОГО JSON без пояснений: {"teaser": "...", "question": "..."}.
teaser — научно-популярная формулировка факта ОДНИМ предложением: начинается с «…что », заканчивается «?».
question — полноценный человекочитаемый вопрос для поиска по теме факта.
Пример. Факт: процесс «обеднение конвертерного шлака», свойство «извлечение никеля», эффект «рост на 12 %» →
{"teaser": "…что при обеднении конвертерного шлака извлечение никеля растёт на 12 %?",
 "question": "Как обеднение конвертерного шлака влияет на извлечение никеля?"}"""

# Кэш формулировок fact_id → {"teaser", "question"}: факты неизменяемы, TTL не нужен
_cache: dict[str, dict[str, str]] = {}
# Факты, по которым генерация уже идёт, — защита от дублирующих фоновых задач
_inflight: set[str] = set()


def cached(fact_id: str) -> dict[str, str] | None:
    """Готовая формулировка из кэша; None — ещё не сгенерирована."""
    return _cache.get(fact_id)


def schedule(fact: Fact, document_name: str) -> None:
    """Ставит фоновую генерацию формулировки, если её нет и она не в работе."""
    if cached(fact.id) is not None or fact.id in _inflight:
        return
    _inflight.add(fact.id)
    asyncio.create_task(_generate(fact, document_name))


async def _generate(fact: Fact, document_name: str) -> None:
    """Фоновая задача: сбой LLM или невалидный ответ фиксируется в логе, запись в кэш не производится."""
    try:
        phrased = await phrase_fact(fact, document_name)
        if phrased is not None:
            _cache[fact.id] = phrased
    except LLMUnavailableError as exc:
        log.warning("«Знаете ли вы?»: LLM недоступна для факта %s: %s", fact.id, exc)
    except Exception:
        log.exception("«Знаете ли вы?»: сбой формулировки факта %s", fact.id)
    finally:
        _inflight.discard(fact.id)


async def phrase_fact(fact: Fact, document_name: str) -> dict[str, str] | None:
    """Формулирует факт через LLM: {"teaser", "question"} или None, если ответ
    невалиден (пустые строки / teaser не начинается с «…что»).
    LLMUnavailableError пробрасывается вызывающему."""
    answer = await chat_json(messages=[
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(_fact_brief(fact, document_name), ensure_ascii=False)},
    ])
    if not isinstance(answer, dict):
        return None
    teaser = str(answer.get("teaser") or "").strip()
    question = str(answer.get("question") or "").strip()
    if teaser.startswith("...что"):  # модель иногда печатает три точки вместо «…»
        teaser = "…" + teaser[3:]
    if not teaser.startswith("…что") or not question:
        return None
    return {"teaser": teaser, "question": question}


def _fact_brief(fact: Fact, document_name: str) -> dict[str, object]:
    """Компактное описание факта для промпта: только заполненные поля."""
    brief: dict[str, object | None] = {
        "материал": fact.material,
        "процесс": fact.process,
        "свойство": fact.property,
        "эффект": direction_label(fact.effect_direction),
        "величина_эффекта": (
            f"{fact.effect_value} {fact.effect_unit or ''}".strip()
            if fact.effect_value is not None else None
        ),
        "результат": (
            f"{fact.result_value} {fact.result_unit or ''}".strip()
            if fact.result_value is not None else None
        ),
        "температура_C": fact.temperature_c,
        "цитата_из_документа": fact.source.quote,
        "документ": document_name,
    }
    return {key: value for key, value in brief.items() if value not in (None, "")}


def _document_name(store, fact: Fact) -> str:
    document = store.documents.get(fact.source.document_id)
    return document.filename if document else ""


def warm_more(store, count: int = 2) -> None:
    """Ставит в фон формулировку для нескольких случайных ещё не прогретых
    фактов — пул растёт с каждым обращением к рубрике и при старте. schedule
    отсеивает уже готовые и находящиеся в работе; насыщенный пул дополнительно
    не прогревается."""
    if len(_cache) >= _WARM_POOL_TARGET:
        return
    for _ in range(count):
        fact = store.random_visible_fact()
        if fact is None:
            return
        schedule(fact, _document_name(store, fact))


def pick(store) -> tuple[Fact | None, dict[str, str] | None]:
    """Факт для рубрики «Знаете ли вы?» и его формулировка (или None).

    Предпочитает УЖЕ прогретый видимый факт, чтобы главная почти всегда
    показывала готовую LLM-формулировку, а не механическую заглушку;
    параллельно пополняет прогретый пул. Холодный старт (пул пуст) — случайный
    факт с механическим показом и фоновой генерацией.
    """
    warm_more(store, 2)
    warmed = [
        fact for fid in list(_cache.keys())
        if (fact := store.facts.get(fid)) is not None and store.is_visible_fact(fact)
    ]
    if warmed:
        fact = random.choice(warmed)
        return fact, cached(fact.id)
    fact = store.random_visible_fact()
    if fact is None:
        return None, None
    phrased = cached(fact.id)
    if phrased is None:
        schedule(fact, _document_name(store, fact))
    return fact, phrased
