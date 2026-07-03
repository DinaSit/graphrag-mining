"""Сборка промпта извлечения из доменной конфигурации (domain/default).

Типы сущностей, связи и канонические термины подставляются из ontology.yaml
и synonyms.csv при старте сервиса; обновление доменных файлов
(зона инженера знаний) не требует правки кода.
"""
import csv
from functools import lru_cache
from pathlib import Path

import yaml

from app import config

_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "extraction.md"

# Сколько канонических терминов каждого типа включать в промпт
_TERMS_PER_TYPE = 40


@lru_cache
def _static_prompt() -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    ontology = yaml.safe_load((config.DOMAIN_DIR / "ontology.yaml").read_text(encoding="utf-8"))

    entity_lines = []
    for name, spec in ontology.get("entity_types", {}).items():
        examples = ", ".join(map(str, (spec.get("examples") or [])[:3]))
        entity_lines.append(f"- {name}: {spec.get('description', '')} Примеры: {examples}")

    relation_lines = []
    for name, spec in ontology.get("relation_types", {}).items():
        src = ", ".join(spec.get("source", []))
        dst = ", ".join(spec.get("target", []))
        relation_lines.append(f"- {name} ({src} → {dst}): {spec.get('description', '')}")

    terms_by_type: dict[str, list[str]] = {}
    with (config.DOMAIN_DIR / "synonyms.csv").open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            canonical = row.get("canonical", "").strip()
            type_ = row.get("type", "").strip() or "Other"
            if canonical and canonical not in terms_by_type.setdefault(type_, []):
                terms_by_type[type_].append(canonical)

    term_lines = [
        f"- {type_}: {', '.join(terms[:_TERMS_PER_TYPE])}"
        for type_, terms in sorted(terms_by_type.items())
        if type_ in ("Material", "Process", "Equipment", "NumericParameter", "Condition", "Property")
    ]

    return (
        template
        .replace("{entity_types}", "\n".join(entity_lines))
        .replace("{relation_types}", "\n".join(relation_lines))
        .replace("{canonical_terms}", "\n".join(term_lines))
    )


def build_prompt(fragment_text: str, element_type: str, page: int) -> str:
    return (
        _static_prompt()
        .replace("{element_type}", element_type)
        .replace("{page}", str(page))
        .replace("{fragment_text}", fragment_text)
    )
