"""Оркестрация извлечения: фрагменты → LLM → кандидаты фактов. Зона ML-A."""
import asyncio
import logging
import re

from app import config, prompt, yandex_client
from app.schemas import ExtractionCandidate, SourceFragment, SourceRef
from app.windows import Window, attribute_quote, build_windows

log = logging.getLogger(__name__)

_NUMERIC_FIELDS = ("temperature_c", "duration_h", "effect_value", "result_value")
_REQUIRED_STR_FIELDS = ("material", "experiment_id", "sample", "process", "property", "lab", "team")
_EFFECT_DIRECTIONS = {"increase", "decrease", "neutral", "unknown"}


# Отказы провайдера, при которых продолжать батч бессмысленно: остальные
# фрагменты завершатся той же ошибкой, а backend должен пометить документ failed
# (HTTP 502), а не completed с фактами, потерянными без уведомления
_FATAL_KINDS = {"auth", "quota", "unavailable"}


async def extract_fragments(fragments: list[SourceFragment]) -> list[ExtractionCandidate]:
    # Единица извлечения — окно, а не фрагмент: модель должна видеть абзац
    # в его разделе, а строку таблицы — вместе с шапкой (см. windows.py)
    batch = build_windows(fragments)
    log.info("Извлечение: %d фрагментов сгруппированы в %d окон", len(fragments), len(batch))
    # Параллельность ограничивает глобальный семафор в yandex_client:
    # суммарный лимит держится и при нескольких одновременных батчах
    results = await asyncio.gather(*(_extract_one(w) for w in batch), return_exceptions=True)
    candidates: list[ExtractionCandidate] = []
    for window, result in zip(batch, results):
        if isinstance(result, yandex_client.YandexClientError) and result.kind in _FATAL_KINDS:
            raise result
        if isinstance(result, BaseException):
            # локальная проблема окна (невалидный JSON, некорректный ответ
            # модели) изолируется и логируется, батч продолжается
            log.warning("Окно с фрагмента %s пропущено: %s", window.lead.id, result)
            continue
        candidates.extend(result)
    return candidates


_DIGIT_RE = re.compile(r"\d")


def _route(window: Window, text: str, image_b64: str | None) -> str:
    """Предварительный маршрутизатор: определяет, каким разбором обрабатывать окно.

    vision — сканы и схемы; full — таблицы и текст с числами (нужны числовые правила
    и словарь терминов); light — простой текст, промпт короче в ~4 раза.
    """
    if image_b64:
        return "vision"
    if window.kind == "table":
        return "full"
    if _DIGIT_RE.search(text):
        return "full"
    return "light"


async def _extract_one(window: Window) -> list[ExtractionCandidate]:
    text = window.text
    lead = window.lead
    image_b64 = lead.metadata.get("image_b64")
    route = _route(window, text, image_b64)

    if route == "vision":
        # Страница без текстового слоя: извлечение по изображению
        prompt_text = prompt.build_prompt(
            "(текстовый слой отсутствует — извлекай данные с приложенного изображения страницы)",
            window.element_type,
            lead.page,
            section=window.section,
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        }]
    else:
        if len(text) < config.MIN_FRAGMENT_CHARS:
            return []  # окна короче порога в модель не отправляются
        messages = [{
            "role": "user",
            "content": prompt.build_prompt(
                text[:8000], window.element_type, lead.page, mode=route, section=window.section
            ),
        }]
    data = await yandex_client.chat_json(messages)

    candidates = []
    for i, claim in enumerate(data.get("claims", [])):
        if not isinstance(claim, dict):
            continue
        payload = _normalize_claim(claim)
        if payload is None:
            log.warning("Отброшен невалидный claim из окна %s: %s", lead.id, str(claim)[:150])
            continue
        confidence = payload.pop("confidence")
        quote = payload.pop("quote", None)
        # Утверждение возвращается тому фрагменту окна, из которого взята цитата:
        # ссылка на источник остаётся точной, окно на неё не влияет
        fragment = attribute_quote(window, quote)
        candidates.append(ExtractionCandidate(
            id=f"candidate-{fragment.id}-{i}",
            type="Claim",
            payload=payload,
            source=SourceRef(
                document_id=fragment.document_id,
                version_id=fragment.version_id,
                fragment_id=fragment.id,
                page=fragment.page,
                section=fragment.section,
                quote=(quote or fragment.text)[:220],
            ),
            confidence=confidence,
        ))
    return candidates


def _normalize_claim(claim: dict) -> dict | None:
    """Приводит claim к контракту extraction-schema.json.

    Возвращает None, если claim не содержит фактического утверждения.
    """
    out = dict(claim)

    for field in _REQUIRED_STR_FIELDS:
        value = out.get(field)
        if not isinstance(value, str) or not value.strip():
            out[field] = "не указано"

    if out["material"] == "не указано" and out["property"] == "не указано":
        return None

    for field in _NUMERIC_FIELDS:
        out[field] = _float_or_none(out.get(field))

    if out.get("effect_direction") not in _EFFECT_DIRECTIONS:
        out["effect_direction"] = "unknown"

    # Нестроковый quote (модель вернула число/объект) заменяется на None:
    # источник подставит текст фрагмента
    if not isinstance(out.get("quote"), str):
        out["quote"] = None

    confidence = _float_or_none(out.get("confidence"))
    out["confidence"] = min(max(confidence, 0.0), 1.0) if confidence is not None else 0.5

    # Поля entities/relations/numeric_parameters сохраняются в payload (JSONB)
    # как задел под расширение схемы графа до полной онтологии
    for key in ("entities", "relations", "numeric_parameters"):
        if not isinstance(out.get(key), list):
            out[key] = []
    return out


def _float_or_none(value) -> float | None:
    if value in (None, "", "null", "не указано"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None
