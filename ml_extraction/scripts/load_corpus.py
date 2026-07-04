#!/usr/bin/env python3
"""Загрузка корпуса документов в базу знаний через POST /ingest.

Файлы .doc конвертируются в .docx автоматически (textutil, только macOS).
Загрузка последовательная: /ingest синхронный, параллельные запросы
перегружают backend.

Запуск:
    python scripts/load_corpus.py --src <папка> [--limit N] [--backend URL]
"""
import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

SUPPORTED = {".pdf", ".docx", ".docm", ".pptx", ".txt", ".md", ".csv", ".xlsx", ".xlsm"}
CONVERTIBLE = {".doc"}


def convert_doc(path: Path, tmp_dir: Path) -> Path | None:
    """Конвертирует .doc в .docx через textutil (входит в macOS)."""
    target = tmp_dir / (path.stem + ".docx")
    result = subprocess.run(
        ["textutil", "-convert", "docx", "-output", str(target), str(path)],
        capture_output=True,
    )
    return target if result.returncode == 0 and target.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="папка с документами")
    ap.add_argument("--limit", type=int, default=0, help="максимум файлов (0 = все)")
    ap.add_argument("--files", nargs="*", help="явный список файлов вместо обхода папки")
    ap.add_argument("--backend", default="http://localhost:8000")
    args = ap.parse_args()

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        src = Path(args.src).expanduser()
        files = sorted(
            p for p in src.rglob("*")
            if p.suffix.lower() in SUPPORTED | CONVERTIBLE and not p.name.startswith((".", "~"))
        )
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("Нет файлов для загрузки")
        return 1

    loaded, failed, skipped = [], [], []
    t_start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for n, path in enumerate(files, 1):
            upload_path = path
            if path.suffix.lower() in CONVERTIBLE:
                converted = convert_doc(path, tmp_dir)
                if converted is None:
                    skipped.append((path.name, "конвертация .doc не удалась"))
                    print(f"[{n}/{len(files)}] {path.name} — ПРОПУЩЕН (конвертация не удалась)")
                    continue
                upload_path = converted

            print(f"[{n}/{len(files)}] {path.name} ... ", end="", flush=True)
            t0 = time.monotonic()
            try:
                with open(upload_path, "rb") as fh:
                    response = httpx.post(
                        f"{args.backend}/ingest",
                        files={"file": (upload_path.name, fh)},
                        data={
                            "document_type": upload_path.suffix.lstrip("."),
                            "source_label": path.name,
                            "access_level": "uploaded",
                        },
                        timeout=3600,
                    )
                response.raise_for_status()
                payload = response.json()
                units = payload.get("evidence_units", "?")
                loaded.append(path.name)
                print(f"OK: {units} evidence units за {time.monotonic() - t0:.0f}с")
            except Exception as exc:
                failed.append((path.name, str(exc)[:120]))
                print(f"ОШИБКА: {exc}")

    total_min = (time.monotonic() - t_start) / 60
    print(f"\n=== Итог за {total_min:.1f} мин ===")
    print(f"Загружено: {len(loaded)}")
    if failed:
        print(f"Ошибки ({len(failed)}):")
        for name, reason in failed:
            print(f"  - {name}: {reason}")
    if skipped:
        print(f"Пропущено ({len(skipped)}):")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
