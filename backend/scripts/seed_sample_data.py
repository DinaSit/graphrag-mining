from __future__ import annotations

from pathlib import Path

from app.pipeline.sample_data import seed_sample_data
from app.storage import ApplicationStore


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    store = ApplicationStore(root / "domain" / "default")
    seed_sample_data(store)
    print(f"Seeded {len(store.documents)} documents, {len(store.facts)} facts, {len(store.fragments)} fragments.")
