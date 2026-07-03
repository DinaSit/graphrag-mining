from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:
    from pint import UnitRegistry
except ImportError:  # pragma: no cover - bundled smoke tests can run without optional deps
    UnitRegistry = None


NUMBER_UNIT_RE = re.compile(
    r"(?P<value>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>°\s*C|°\s*С|C|С|h|ч|час(?:а|ов)?|%|мг/л|мг/дм3|мг/дм³|mg/l|ppm|м/с|m/s)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuantityHit:
    value: float
    unit: str
    kind: str
    normalized_value: float
    normalized_unit: str


def extract_quantity_hits(text: str) -> list[QuantityHit]:
    hits: list[QuantityHit] = []
    for match in NUMBER_UNIT_RE.finditer(text):
        raw_value = float(match.group("value").replace(",", "."))
        raw_unit = match.group("unit").replace(" ", "")
        kind, normalized_value, normalized_unit = normalize_quantity(raw_value, raw_unit)
        hits.append(
            QuantityHit(
                value=raw_value,
                unit=raw_unit,
                kind=kind,
                normalized_value=normalized_value,
                normalized_unit=normalized_unit,
            )
        )
    return hits


def normalize_quantity(value: float, unit: str) -> tuple[str, float, str]:
    normalized = unit.lower().replace("с", "c")
    if normalized in {"°c", "c"}:
        return "temperature", value, "C"
    if normalized in {"h", "ч", "час", "часа", "часов"}:
        return "duration", _convert(value, "hour", "hour"), "h"
    if normalized == "%":
        return "relative_effect", value, "%"
    if normalized in {"мг/л", "мг/дм3", "мг/дм³", "mg/l", "ppm"}:
        return "concentration", value, "mg/L"
    if normalized in {"м/c", "м/с", "m/s"}:
        return "velocity", value, "m/s"
    return "unknown", value, unit


def validate_candidate_numbers(payload: dict[str, Any], source_text: str | None) -> dict[str, Any]:
    """Validate extracted numeric values against source text using regex + Pint-compatible units."""

    hits = extract_quantity_hits(source_text or "")
    issues: list[str] = []
    matched_fields: list[str] = []

    field_rules = [
        ("temperature_c", "temperature", 1.0),
        ("duration_h", "duration", 0.1),
        ("effect_value", "relative_effect", 0.1),
    ]
    for field, kind, tolerance in field_rules:
        value = _float_or_none(payload.get(field))
        if value is None:
            continue
        if any(hit.kind == kind and abs(hit.normalized_value - value) <= tolerance for hit in hits):
            matched_fields.append(field)
        elif source_text:
            issues.append(f"{field}={value:g} не найдено в source evidence regex+unit validation")

    return {
        "validated": not issues,
        "issues": issues,
        "matched_fields": matched_fields,
        "quantities": [hit.__dict__ for hit in hits],
    }


def _convert(value: float, source_unit: str, target_unit: str) -> float:
    if UnitRegistry is None:
        return value
    registry = UnitRegistry()
    return float((value * registry(source_unit)).to(target_unit).magnitude)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None
