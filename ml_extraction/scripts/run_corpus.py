#!/usr/bin/env python3
"""Прогон извлечения по корпусу документов. Результат — JSON-файлы с кандидатами.

Документы разбираются парсерами backend (используются через импорт),
фрагменты отправляются в запущенный сервис извлечения по HTTP.

Запуск:
    1) запустить сервис: docker compose up ml-extraction
       (либо локально: cd ml_extraction && uvicorn app.main:app --port 8002)
    2) python scripts/run_corpus.py --src <папка с документами> --out results/ [--limit N]

Зависимости парсеров: pip install pdfplumber python-docx python-pptx openpyxl
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))

# .env корня репозитория — для локального запуска
_env_file = _ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

SUPPORTED = {".pdf", ".docx", ".docm", ".pptx", ".txt", ".md", ".csv", ".xlsx"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="папка с документами")
    ap.add_argument("--out", default="results", help="куда класть JSON")
    ap.add_argument("--limit", type=int, default=0, help="максимум документов (0 = все)")
    ap.add_argument("--max-fragments", type=int, default=30, help="максимум фрагментов на документ")
    ap.add_argument("--service", default=os.environ.get("EXTRACTION_URL", "http://localhost:8002/extract"))
    args = ap.parse_args()

    try:
        from app.pipeline.parsers import choose_parser  # парсеры backend
    except ImportError as e:
        print(f"Не хватает зависимостей парсеров backend: {e}\n"
              f"Поставь: pip install pdfplumber python-docx python-pptx openpyxl fastapi pydantic")
        return 1

    import httpx

    src, out_dir = Path(args.src), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.rglob("*")
                   if p.suffix.lower() in SUPPORTED and not p.name.startswith(("~", ".")))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"В {src} нет поддерживаемых файлов")
        return 1

    total = 0
    for n, path in enumerate(files, 1):
        print(f"[{n}/{len(files)}] {path.name} ... ", end="", flush=True)
        t0 = time.monotonic()
        try:
            parser = choose_parser(path.name)
            # document_id/version_id фиктивные: скрипт не пишет в базу, id нужны
            # лишь для обязательных полей SourceFragment и трассировки в JSON
            fragments = parser.parse(f"doc-{path.stem[:40]}", "local-v1", path.name, path.read_bytes())
            # Фрагменты короче 40 символов (колонтитулы, обрывки) фактов не несут — отсеиваются
            fragments = [f for f in fragments if len((f.text or "").strip()) >= 40][: args.max_fragments]
            if not fragments:
                print("нет текстовых фрагментов (скан? пустой?)")
                continue
            async with httpx.AsyncClient(timeout=1800) as client:
                resp = await client.post(
                    args.service,
                    json={"fragments": [f.model_dump(mode="json") for f in fragments]},
                )
                resp.raise_for_status()
                candidates = resp.json()["candidates"]
            (out_dir / f"{path.stem}.json").write_text(
                json.dumps({"file": path.name, "fragments": len(fragments), "candidates": candidates},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total += len(candidates)
            print(f"{len(fragments)} фрагм. → {len(candidates)} кандидатов за {time.monotonic() - t0:.0f}с")
        except Exception as e:
            print(f"ОШИБКА: {e}")

    print(f"\nГотово: {total} кандидатов, результаты в {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
