from __future__ import annotations

import csv
from pathlib import Path


class DomainNormalizer:
    """Канонизация имён сущностей по domain/default/synonyms.csv
    (владелец словаря — инженер знаний)."""

    def __init__(self, domain_dir: Path):
        self.domain_dir = domain_dir
        self.aliases = self._load_aliases(domain_dir / "synonyms.csv")

    def normalize_entity(self, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.strip().lower().replace("ё", "е").split())
        return self.aliases.get(compact, value.strip())

    def _load_aliases(self, path: Path) -> dict[str, str]:
        aliases: dict[str, str] = {}
        if not path.exists():
            return aliases
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                canonical = row.get("canonical", "").strip()
                alias = row.get("alias", "").strip()
                if canonical and alias:
                    aliases[alias.lower().replace("ё", "е")] = canonical
                    aliases[canonical.lower().replace("ё", "е")] = canonical
        return aliases
