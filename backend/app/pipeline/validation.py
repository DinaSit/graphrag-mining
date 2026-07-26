from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pipeline.normalization import float_or_none

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None



# Доменный каталог: тот же, что монтируется в контейнер read-only. Переопределяется
# переменной DOMAIN_DIR — так сервис извлечения уже читает онтологию и промпты
DOMAIN_DIR = Path(os.getenv("DOMAIN_DIR") or Path(__file__).resolve().parents[3] / "domain" / "default")


def _read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_validation_rules(domain_dir: Path) -> dict[str, Any]:
    """Читает domain/default/validation-rules.yaml: диапазоны правдоподобия
    числовых параметров (владелец — инженер знаний)."""
    rules: dict[str, Any] = {
        "ranges_by_name": {},
        "quantity_by_name": {},
    }
    path = Path(domain_dir) / "validation-rules.yaml"
    if yaml is None or not path.exists():
        return rules
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for name, spec in (data.get("params") or {}).items():
        if not isinstance(spec, dict) or "min" not in spec or "max" not in spec:
            continue
        rules["ranges_by_name"][name.lower()] = (float(spec["min"]), float(spec["max"]))
        quantity = spec.get("quantity")
        if quantity:
            rules["quantity_by_name"][name.lower()] = quantity
    return rules

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


def _load_units(domain_dir: Path) -> tuple[dict[str, UnitSpec], dict[str, TargetQuantity], list[str]]:
    """Читает domain/default/units.yaml: приведение единиц к базовым (владелец —
    инженер знаний). Возвращает таблицу алиасов, базовые единицы величин и
    многосимвольные алиасы для шаблона поиска."""
    specs: dict[str, UnitSpec] = {}
    quantities: dict[str, TargetQuantity] = {}
    aliases: set[str] = set()
    data = _read_yaml(domain_dir / "units.yaml")
    for kind, item in (data.get("units") or {}).items():
        spec = UnitSpec(str(item.get("kind") or kind), str(item["unit"]),
                        str(item["dimension"]), float(item["to_base"]))
        for alias in item.get("aliases") or []:
            specs[str(alias).lower()] = spec
            # Односимвольные в шаблон не идут: они зависят от регистра и
            # добавляются отдельным правилом (_SINGLE_CHAR_UNITS)
            if len(str(alias)) > 1:
                aliases.add(str(alias))
    for name, item in (data.get("quantities") or {}).items():
        quantities[name] = TargetQuantity(str(item["dimension"]), str(item["unit"]), float(item["from_base"]))
    # Длинные написания раньше коротких: иначе «м» съест начало «м/с»
    return specs, quantities, sorted(aliases, key=lambda a: (-len(a), a))





# Составные единицы идут раньше однобуквенных: иначе «м» сопоставляется с началом «м/с».
# Однобуквенные различаются регистром: «С» — Цельсий, «с» — секунды,
# «В» — вольты, строчное «в» — предлог и единицей не считается.
_UNIT_SPECS, _TARGET_QUANTITIES, _MULTI_CHAR_UNITS = _load_units(DOMAIN_DIR)

# Односимвольные единицы зависят от регистра и потому заданы шаблоном, а не
# списком: «С» — Цельсий, «с» — секунды, «В» — предлог и единицей не считается.
# Градус пишут и слитно, и через пробел
_SINGLE_CHAR_UNITS = r"%|°\s*[CcСс]|h|ч|m|м|(?-i:[CС])|(?-i:[sс])|(?-i:[VВ])"

# Составные единицы идут раньше однобуквенных: иначе «м» сопоставляется с началом «м/с»
_UNITS = "|".join([*(re.escape(a) for a in _MULTI_CHAR_UNITS), _SINGLE_CHAR_UNITS])

# Число: допускает разделители тысяч пробелом («12 000») и десятичную запятую
_NUM_CORE = r"(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d+)?"
# Минус встречается и как U+2212, и как en-dash в PDF-копиях
_SIGN = r"[+\-−–]?"
# Слева от числа не должно быть букв, цифр и дефисов: это исключает правые
# части диапазонов («700-750» не даёт −750) и части слов/кодов («EXP-001»)
_BOUND = r"(?<![\w.,+\-−–—])"
# После единицы — граница слова: «30 с при» — секунды, «2 части» — не часы
_TAIL = r"(?![0-9A-Za-zА-Яа-яЁё])"

NUMBER_UNIT_RE = re.compile(
    _BOUND + r"(?P<value>" + _SIGN + _NUM_CORE + r")\s*(?P<unit>" + _UNITS + r")" + _TAIL,
    re.IGNORECASE,
)
# Диапазоны «700-750 °C», «200...300 мг/л» разбираются отдельным проходом
# до одиночных чисел: единица приписывается обоим концам
RANGE_RE = re.compile(
    _BOUND
    + r"(?P<value1>" + _SIGN + _NUM_CORE + r")"
    + r"\s*(?:\.{2,3}|…|[\-−–—])\s*"
    + r"(?P<value2>" + _SIGN + _NUM_CORE + r")"
    + r"\s*(?P<unit>" + _UNITS + r")" + _TAIL,
    re.IGNORECASE,
)
# «рН» в русских отчётах пишется кириллицей, встречаются и смешанные написания;
# диапазон «pH 4-6» даёт оба конца
PH_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])[pр][hн]\s*[=:]?\s*(?P<value>[+\-−]?\d+(?:[.,]\d+)?)"
    r"(?:\s*(?:\.{2,3}|…|[\-−–—])\s*(?P<value2>\d+(?:[.,]\d+)?))?",
    re.IGNORECASE,
)


def extract_quantity_hits(text: str) -> list[QuantityHit]:
    hits: list[QuantityHit] = []
    consumed: list[tuple[int, int]] = []
    for match in RANGE_RE.finditer(text):
        consumed.append(match.span())
        raw_unit = match.group("unit").replace(" ", "")
        hits.append(_make_hit(match.group("value1"), raw_unit))
        hits.append(_make_hit(match.group("value2"), raw_unit))
    for match in NUMBER_UNIT_RE.finditer(text):
        start, end = match.span()
        # Совпадения внутри уже разобранных диапазонов повторно не обрабатываются
        if any(start < c_end and c_start < end for c_start, c_end in consumed):
            continue
        hits.append(_make_hit(match.group("value"), match.group("unit").replace(" ", "")))
    for match in PH_RE.finditer(text):
        raw_value = _parse_number(match.group("value"))
        hits.append(QuantityHit(raw_value, "pH", "dimensionless", raw_value, "pH"))
        if match.group("value2"):
            second = _parse_number(match.group("value2"))
            hits.append(QuantityHit(second, "pH", "dimensionless", second, "pH"))
    return hits


def _make_hit(raw_value: str, raw_unit: str) -> QuantityHit:
    value = _parse_number(raw_value)
    unit = _resolve_unit_alias(raw_unit)
    kind, normalized_value, normalized_unit = normalize_quantity(value, unit)
    return QuantityHit(
        value=value,
        unit=unit,
        kind=kind,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
    )


def _parse_number(raw: str) -> float:
    cleaned = raw.replace("−", "-").replace("–", "-")
    cleaned = cleaned.replace(" ", "").replace(" ", "").replace(" ", "")
    return float(cleaned.replace(",", "."))


def _resolve_unit_alias(raw_unit: str) -> str:
    # После lowercase «С» и «с» неразличимы, поэтому регистр
    # разрешается здесь, пока он ещё известен
    if raw_unit in ("С", "C"):
        return "°C"
    if raw_unit in ("с", "s"):
        return "сек"
    return raw_unit


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
    """Сверяет извлечённые числа с числами источника в нормализованных единицах.

    При переданных rules именованные numeric_parameters дополнительно
    проверяются на правдоподобие по диапазонам из validation-rules.yaml.
    """

    hits = extract_quantity_hits(source_text or "")
    issues: list[str] = []
    matched_fields: list[str] = []

    field_rules = [
        ("temperature_c", "temperature", 1.0),
        ("duration_h", "duration", 0.1),
    ]
    for field, kind, tolerance in field_rules:
        value = float_or_none(payload.get(field))
        if value is None:
            continue
        if any(hit.kind == kind and abs(hit.normalized_value - value) <= tolerance for hit in hits):
            matched_fields.append(field)
        elif source_text:
            issues.append(f"{field}={value:g} не найдено в source evidence regex+unit validation")

    # Эффект сверяется в его собственной единице (м/с, мг/л, HV),
    # а не как безусловные проценты; без единицы считаем эффект процентами
    effect_value = float_or_none(payload.get("effect_value"))
    if effect_value is not None:
        effect_unit = payload.get("effect_unit")
        if _unit_spec(effect_unit) is None:
            effect_unit = "%" if not effect_unit else None
        if _value_seen_in_source(effect_value, hits, unit=effect_unit, source_text=source_text or "", tolerance=0.1):
            matched_fields.append("effect_value")
        elif source_text:
            issues.append(f"effect_value={effect_value:g} не найдено в source evidence regex+unit validation")

    if rules:
        # Правдоподобие проверяется по имени параметра: объединённые
        # по виду величины диапазоны дают ложные отказы («рост на 150 %»)
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
                    float_or_none(param.get("value")),
                    float_or_none(param.get("value_min")),
                    float_or_none(param.get("value_max")),
                )
                if value is not None
            ]
            if not values:
                continue
            if source_text and not all(
                _value_seen_in_source(value, hits, unit=unit, quantity=quantity, source_text=source_text)
                for value in values
            ):
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


def _value_seen_in_source(
    value: float,
    hits: list[QuantityHit],
    unit: str | None = None,
    quantity: str | None = None,
    source_text: str = "",
    tolerance: float = 0.01,
) -> bool:
    spec = _unit_spec(unit)
    if spec is not None:
        # Сравнение только в совместимой размерности: «5 г/л» в тексте
        # не подтверждает извлечённые «5 мг/л»
        expected_base = value * spec.to_base
        for hit in hits:
            hit_spec = _unit_spec(hit.unit)
            if hit_spec is None or hit_spec.dimension != spec.dimension:
                continue
            if abs(hit.value * hit_spec.to_base - expected_base) <= tolerance:
                return True
        return False
    if quantity is not None:
        # Единица параметра не указана: считаем значение заданным
        # в единице правила и сверяем с конвертированными хитами
        expected = normalize_for_quantity(value, unit, quantity)
        if expected is not None:
            for hit in hits:
                actual = normalize_for_quantity(hit.value, hit.unit, quantity)
                if actual is not None and abs(actual[0] - expected[0]) <= tolerance:
                    return True
            return False
    # Единица неизвестна валидатору: размерность проверить нельзя,
    # подтверждаем хотя бы присутствие самого числа в тексте
    if any(abs(hit.value - value) <= tolerance for hit in hits):
        return True
    return _plain_number_in_text(value, source_text)


def _plain_number_in_text(value: float, text: str) -> bool:
    if not text:
        return False
    rendered = f"{value:g}"
    for variant in {rendered, rendered.replace(".", ",")}:
        if re.search(r"(?<![\w.,\-])" + re.escape(variant) + r"(?!\d|[.,]\d)", text):
            return True
    return False


# Символы, которые разнятся между цитатой модели и текстом документа, не меняя
# смысла: кавычки всех начертаний, тире всех длин, мягкий перенос. Приводятся
# к пробелу, пробелы затем схлопываются. Без этой нормализации расхождений
# втрое больше, и почти все они — форматирование, а не подмена текста
_QUOTE_NOISE = str.maketrans({char: " " for char in "«»\"'“”„‘’—–-­‑"})


def _normalize_quote(text: str | None) -> str:
    lowered = str(text or "").lower().replace("ё", "е").translate(_QUOTE_NOISE)
    return " ".join(lowered.split())


def quote_in_source(quote: str | None, source_text: str | None) -> bool:
    """Встречается ли цитата в тексте фрагмента дословно (с точностью до
    регистра, ё/е, кавычек, тире и пробелов).

    Единственная проверка в конвейере, которая сверяет утверждение модели
    с документом напрямую, а не спрашивает модель о ней самой. Пустая цитата
    проверке не подлежит — считается пройденной, противоречить нечему.
    """
    normalized = _normalize_quote(quote)
    if not normalized:
        return True
    return normalized in _normalize_quote(source_text)
