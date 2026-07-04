from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pint import UnitRegistry
except ImportError:  # pragma: no cover - bundled smoke tests can run without optional deps
    UnitRegistry = None

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def load_validation_rules(domain_dir: Path) -> dict[str, Any]:
    """Читает domain/default/validation-rules.yaml: пороги кандидатов и
    диапазоны правдоподобия числовых параметров (владелец — инженер знаний).
    """
    rules: dict[str, Any] = {
        "thresholds": {},
        "ranges_by_quantity": {},
        "ranges_by_name": {},
        "quantity_by_name": {},
    }
    path = Path(domain_dir) / "validation-rules.yaml"
    if yaml is None or not path.exists():
        return rules
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules["thresholds"] = data.get("candidate_thresholds", {})
    for name, spec in (data.get("params") or {}).items():
        if not isinstance(spec, dict) or "min" not in spec or "max" not in spec:
            continue
        low, high = float(spec["min"]), float(spec["max"])
        rules["ranges_by_name"][name.lower()] = (low, high)
        # По виду величины диапазоны разных параметров объединяются:
        # это грубая проверка «температура 99999 °C нереальна», точная — по имени
        quantity = spec.get("quantity")
        if quantity:
            rules["quantity_by_name"][name.lower()] = quantity
            merged = rules["ranges_by_quantity"].get(quantity)
            rules["ranges_by_quantity"][quantity] = (
                min(low, merged[0]) if merged else low,
                max(high, merged[1]) if merged else high,
            )
    return rules


NUMBER_UNIT_RE = re.compile(
    r"(?P<value>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>кВт·ч/т|kWh/t|мг/дм3|мг/дм³|mg/dm3|mg/dm³|г/дм3|г/дм³|g/dm3|g/dm³|м3/ч|м³/ч|m3/h|A/m2|А/м²|А/м2|мСм/см|mS/cm|мг/л|mg/l|г/л|g/l|г/т|g/t|м/с|m/s|MPa|МПа|bar|бар|atm|атм|ppm|kgf|кгс|час(?:а|ов)?|мкм|μm|um|mm|мм|mV|мВ|NTU|h|ч|%|°\s*C|°\s*С|C|С|т/ч|t/h|V|В|m|м)",
    re.IGNORECASE,
)
PH_RE = re.compile(r"pH\s*[=:]?\s*(?P<value>[+-]?\d+(?:[.,]\d+)?)", re.IGNORECASE)


@dataclass(frozen=True)
class QuantityHit:
    value: float
    unit: str
    kind: str
    normalized_value: float
    normalized_unit: str


@dataclass(frozen=True)
class UnitSpec:
    kind: str
    unit: str
    dimension: str
    to_base: float


@dataclass(frozen=True)
class TargetQuantity:
    dimension: str
    unit: str
    from_base: float


_UNIT_SPECS: dict[str, UnitSpec] = {
    "c": UnitSpec("temperature", "C", "temperature", 1.0),
    "°c": UnitSpec("temperature", "C", "temperature", 1.0),
    "с": UnitSpec("temperature", "C", "temperature", 1.0),
    "°с": UnitSpec("temperature", "C", "temperature", 1.0),
    "h": UnitSpec("duration", "h", "duration", 1.0),
    "ч": UnitSpec("duration", "h", "duration", 1.0),
    "час": UnitSpec("duration", "h", "duration", 1.0),
    "часа": UnitSpec("duration", "h", "duration", 1.0),
    "часов": UnitSpec("duration", "h", "duration", 1.0),
    "%": UnitSpec("relative_effect", "%", "relative_effect", 1.0),
    "м3/ч": UnitSpec("flow_rate", "m3/h", "flow_rate", 1.0),
    "м³/ч": UnitSpec("flow_rate", "m3/h", "flow_rate", 1.0),
    "m3/h": UnitSpec("flow_rate", "m3/h", "flow_rate", 1.0),
    "kgf": UnitSpec("force", "kgf", "force", 1.0),
    "кгс": UnitSpec("force", "kgf", "force", 1.0),
    "мг/л": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "мг/дм3": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "мг/дм³": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "mg/l": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "mg/dm3": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "mg/dm³": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "г/л": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "г/дм3": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "г/дм³": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "g/l": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "g/dm3": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "g/dm³": UnitSpec("concentration_high", "g/L", "concentration", 1000.0),
    "г/т": UnitSpec("concentration_trace", "g/t", "concentration_trace", 1.0),
    "g/t": UnitSpec("concentration_trace", "g/t", "concentration_trace", 1.0),
    "ppm": UnitSpec("concentration", "mg/L", "concentration", 1.0),
    "м/с": UnitSpec("velocity", "m/s", "velocity", 1.0),
    "m/s": UnitSpec("velocity", "m/s", "velocity", 1.0),
    "mpa": UnitSpec("pressure", "MPa", "pressure", 1.0),
    "мпа": UnitSpec("pressure", "MPa", "pressure", 1.0),
    "bar": UnitSpec("pressure", "MPa", "pressure", 0.1),
    "бар": UnitSpec("pressure", "MPa", "pressure", 0.1),
    "atm": UnitSpec("pressure", "MPa", "pressure", 0.101325),
    "атм": UnitSpec("pressure", "MPa", "pressure", 0.101325),
    "mm": UnitSpec("length", "mm", "length", 1.0),
    "мм": UnitSpec("length", "mm", "length", 1.0),
    "m": UnitSpec("length_m", "m", "length", 1000.0),
    "м": UnitSpec("length_m", "m", "length", 1000.0),
    "мкм": UnitSpec("length_um", "um", "length", 0.001),
    "μm": UnitSpec("length_um", "um", "length", 0.001),
    "um": UnitSpec("length_um", "um", "length", 0.001),
    "v": UnitSpec("voltage", "V", "voltage", 1.0),
    "в": UnitSpec("voltage", "V", "voltage", 1.0),
    "mv": UnitSpec("voltage_mv", "mV", "voltage", 0.001),
    "мв": UnitSpec("voltage_mv", "mV", "voltage", 0.001),
    "a/m2": UnitSpec("current_density", "A/m2", "current_density", 1.0),
    "а/м2": UnitSpec("current_density", "A/m2", "current_density", 1.0),
    "а/м²": UnitSpec("current_density", "A/m2", "current_density", 1.0),
    "квт·ч/т": UnitSpec("specific_energy", "kWh/t", "specific_energy", 1.0),
    "kwh/t": UnitSpec("specific_energy", "kWh/t", "specific_energy", 1.0),
    "т/ч": UnitSpec("mass_flow", "t/h", "mass_flow", 1.0),
    "t/h": UnitSpec("mass_flow", "t/h", "mass_flow", 1.0),
    "ntu": UnitSpec("turbidity", "NTU", "turbidity", 1.0),
    "ms/cm": UnitSpec("conductivity", "mS/cm", "conductivity", 1.0),
    "мсм/см": UnitSpec("conductivity", "mS/cm", "conductivity", 1.0),
}


_TARGET_QUANTITIES: dict[str, TargetQuantity] = {
    "temperature": TargetQuantity("temperature", "C", 1.0),
    "duration": TargetQuantity("duration", "h", 1.0),
    "relative_effect": TargetQuantity("relative_effect", "%", 1.0),
    "flow_rate": TargetQuantity("flow_rate", "m3/h", 1.0),
    "force": TargetQuantity("force", "kgf", 1.0),
    "pressure": TargetQuantity("pressure", "MPa", 1.0),
    "pressure_bar": TargetQuantity("pressure", "bar", 10.0),
    "pressure_atm": TargetQuantity("pressure", "atm", 9.869232667),
    "length": TargetQuantity("length", "mm", 1.0),
    "length_m": TargetQuantity("length", "m", 0.001),
    "length_um": TargetQuantity("length", "um", 1000.0),
    "voltage": TargetQuantity("voltage", "V", 1.0),
    "voltage_mv": TargetQuantity("voltage", "mV", 1000.0),
    "current_density": TargetQuantity("current_density", "A/m2", 1.0),
    "specific_energy": TargetQuantity("specific_energy", "kWh/t", 1.0),
    "mass_flow": TargetQuantity("mass_flow", "t/h", 1.0),
    "concentration": TargetQuantity("concentration", "mg/L", 1.0),
    "concentration_high": TargetQuantity("concentration", "g/L", 0.001),
    "concentration_trace": TargetQuantity("concentration_trace", "g/t", 1.0),
    "dimensionless": TargetQuantity("dimensionless", "pH", 1.0),
    "turbidity": TargetQuantity("turbidity", "NTU", 1.0),
    "conductivity": TargetQuantity("conductivity", "mS/cm", 1.0),
}


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
    for match in PH_RE.finditer(text):
        raw_value = float(match.group("value").replace(",", "."))
        hits.append(QuantityHit(raw_value, "pH", "dimensionless", raw_value, "pH"))
    return hits


def normalize_quantity(value: float, unit: str) -> tuple[str, float, str]:
    spec = _unit_spec(unit)
    if spec is not None:
        converted = normalize_for_quantity(value, unit, spec.kind)
        if converted is not None:
            normalized_value, normalized_unit = converted
            return spec.kind, normalized_value, normalized_unit
    return "unknown", value, unit


def normalize_for_quantity(value: float, unit: str | None, quantity: str | None) -> tuple[float, str] | None:
    if quantity is None:
        return value, unit or ""
    target = _TARGET_QUANTITIES.get(quantity)
    if target is None:
        return value, unit or ""
    if unit is None or str(unit).strip() == "":
        return value, target.unit
    if quantity == "dimensionless" and _unit_key(unit) == "ph":
        return value, target.unit
    spec = _unit_spec(unit)
    if spec is None:
        return None
    if _unit_key(unit) == "ppm" and quantity == "concentration_trace":
        return value, target.unit
    if spec.dimension != target.dimension:
        return None
    return value * spec.to_base * target.from_base, target.unit


def validate_candidate_numbers(
    payload: dict[str, Any], source_text: str | None, rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate extracted numeric values against source text using regex + Pint-compatible units.

    При переданных rules числа дополнительно проверяются на правдоподобие
    по диапазонам из validation-rules.yaml.
    """

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

    if rules:
        ranges_by_quantity = rules.get("ranges_by_quantity", {})
        has_named_numeric_parameters = bool(payload.get("numeric_parameters"))
        if not has_named_numeric_parameters:
            for hit in hits:
                bounds = ranges_by_quantity.get(hit.kind)
                if bounds and not (bounds[0] <= hit.normalized_value <= bounds[1]):
                    issues.append(
                        f"{hit.value:g} {hit.unit} вне диапазона правдоподобия {bounds[0]:g}-{bounds[1]:g} ({hit.kind})"
                    )
        # Точная проверка по имени параметра: извлечённые numeric_parameters несут название
        ranges_by_name = rules.get("ranges_by_name", {})
        quantity_by_name = rules.get("quantity_by_name", {})
        for param in payload.get("numeric_parameters") or []:
            if not isinstance(param, dict):
                continue
            name = str(param.get("name", "")).lower()
            rule_name, bounds = _matching_rule(name, ranges_by_name)
            quantity = quantity_by_name.get(rule_name or "")
            unit = param.get("unit")
            values = [
                value for value in (
                    _float_or_none(param.get("value")),
                    _float_or_none(param.get("value_min")),
                    _float_or_none(param.get("value_max")),
                )
                if value is not None
            ]
            if not values:
                continue
            if source_text and not all(_value_seen_in_source(value, hits, unit=unit, quantity=quantity) for value in values):
                issues.append(f"«{param.get('name') or param.get('type') or 'numeric_parameter'}»={values} не найдено в source evidence")
            if bounds is None:
                continue
            low, high = bounds
            normalized_values: list[float] = []
            failed_conversion = False
            for value in values:
                converted = normalize_for_quantity(value, unit, quantity)
                if converted is None:
                    failed_conversion = True
                    normalized_values.append(value)
                else:
                    normalized_values.append(converted[0])
            if failed_conversion:
                issues.append(f"«{param.get('name')}»={values} не удалось привести к единице правила")
                continue
            if any(not (low <= value <= high) for value in normalized_values):
                unit_label = normalize_for_quantity(values[0], unit, quantity)[1] if values and quantity else str(unit or "")
                issues.append(f"«{param.get('name')}»={normalized_values} {unit_label} вне диапазона {low:g}-{high:g}")

    return {
        "validated": not issues,
        "issues": issues,
        "matched_fields": matched_fields,
        "quantities": [hit.__dict__ for hit in hits],
    }


def _unit_spec(unit: str | None) -> UnitSpec | None:
    return _UNIT_SPECS.get(_unit_key(unit))


def _unit_key(unit: str | None) -> str:
    if unit is None:
        return ""
    return (
        str(unit)
        .strip()
        .replace(" ", "")
        .replace("³", "3")
        .replace("²", "2")
        .replace("μ", "u")
        .lower()
    )


def _matching_rule(name: str, ranges_by_name: dict[str, tuple[float, float]]) -> tuple[str | None, tuple[float, float] | None]:
    for rule_name, bounds in ranges_by_name.items():
        if rule_name in name:
            return rule_name, bounds
    return None, None


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


def _value_seen_in_source(
    value: float,
    hits: list[QuantityHit],
    unit: str | None = None,
    quantity: str | None = None,
) -> bool:
    expected = normalize_for_quantity(value, unit, quantity)
    for hit in hits:
        if abs(hit.value - value) <= 0.01 or abs(hit.normalized_value - value) <= 0.01:
            return True
        if expected is None:
            continue
        actual = normalize_for_quantity(hit.value, hit.unit, quantity)
        if actual is not None and abs(actual[0] - expected[0]) <= 0.01:
            return True
    return False
