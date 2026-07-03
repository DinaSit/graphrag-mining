from __future__ import annotations

import csv
import difflib
from pathlib import Path


class DomainNormalizer:
    def __init__(self, domain_dir: Path):
        self.domain_dir = domain_dir
        self.aliases = self._load_aliases(domain_dir / "synonyms.csv")
        self.units = {
            "°c": "C",
            "c": "C",
            "с": "C",
            "h": "h",
            "ч": "h",
            "час": "h",
            "часов": "h",
            "%": "%",
        }

    def normalize_entity(self, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.strip().lower().replace("ё", "е").split())
        return self.aliases.get(compact, value.strip())

    def normalize_unit(self, unit: str | None) -> str | None:
        if unit is None:
            return None
        return self.units.get(unit.strip().lower(), unit.strip())

    def best_duplicate(self, value: str, existing: list[str], threshold: float = 0.86) -> str | None:
        normalized = self.normalize_entity(value) or value
        candidates = [self.normalize_entity(item) or item for item in existing]
        matches = difflib.get_close_matches(normalized.lower(), [item.lower() for item in candidates], n=1, cutoff=threshold)
        if not matches:
            return None
        for item in candidates:
            if item.lower() == matches[0]:
                return item
        return None

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

