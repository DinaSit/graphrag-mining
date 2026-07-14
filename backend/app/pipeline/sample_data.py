from __future__ import annotations

from pathlib import Path

from app.schemas import CandidateStatus, ExtractionCandidate, SourceRef, SourceFragment
from app.storage import ApplicationStore


MATERIALS = ["Сплав X", "Сплав Y", "Сплав Z", "Композит A", "Никелевый сплав N"]
LABS = ["Лаборатория Север", "Лаборатория Центр", "Лаборатория Юг"]
PROPERTIES = ["твёрдость", "прочность", "пластичность", "вязкость", "коррозионная стойкость"]


def seed_sample_data(store: ApplicationStore, extra_pdf_dir: Path | None = None) -> None:
    if store.documents:
        return

    docs = []
    for index in range(1, 13):
        content = (
            f"Sample-отчет {index}. Серия экспериментов по материалам: {', '.join(MATERIALS)}. "
            "Фрагменты используются для проверки GraphRAG с источниками."
        ).encode("utf-8")
        docs.append(store.ingest_document(f"sample-report-{index:02d}.txt", content, "report", f"Sample report {index}", "sample"))

    experiments = _build_experiments()
    for index, payload in enumerate(experiments, start=1):
        document = docs[(index - 1) % len(docs)]
        fragment_id = f"sample-fragment-{index:03d}"
        text = (
            f"{payload['experiment_id']}: {payload['material']}, {payload['process']}, "
            f"{payload['temperature_c']} °C, {payload['duration_h']} ч. "
            f"Свойство {payload['property']}: {payload['effect_direction']} "
            f"{payload['effect_value']}{payload['effect_unit']}. Команда {payload['team']}."
        )
        fragment = SourceFragment(
            id=fragment_id,
            document_id=document.id,
            version_id=document.current_version_id,
            page=(index % 8) + 1,
            element_type="table_row",
            section="Synthetic experiments",
            text=text,
            normalized_text=text.lower().replace("ё", "е"),
            metadata={"synthetic": True, "experiment_id": payload["experiment_id"]},
        )
        store.add_source_fragment(fragment)
        source = SourceRef(
            document_id=fragment.document_id,
            version_id=fragment.version_id,
            fragment_id=fragment.id,
            page=fragment.page,
            section=fragment.section,
            table="Таблица S1",
            quote=fragment.text,
        )
        candidate = ExtractionCandidate(
            id=f"candidate-sample-{index:03d}",
            type="Claim",
            payload=payload,
            source=source,
            confidence=payload["confidence"],
            status=CandidateStatus.approved,
        )
        store.add_candidate(candidate)

    store.index_fragments(list(store.fragments.values()))
    if extra_pdf_dir and extra_pdf_dir.exists():
        for pdf_path in sorted(extra_pdf_dir.glob("*.pdf")):
            store.ingest_document(
                filename=pdf_path.name,
                content=pdf_path.read_bytes(),
                document_type="pdf",
                source_label="Teamlead PDF",
                access_level="sample",
            )


def _build_experiments() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    temperatures = [650, 680, 705, 720, 735, 760, 790]
    counter = 1
    for material_index, material in enumerate(MATERIALS):
        for temp_index, temp in enumerate(temperatures):
            # Разнообразие без random: свойства, направления и величины
            # раскладываются по остаткам от деления — демо-данные стабильны
            # между запусками
            prop = PROPERTIES[(material_index + temp_index) % len(PROPERTIES)]
            # Сценарий «пика старения» для Сплава X: твёрдость растёт при 720 °C
            # и падает при 735 °C — физически согласованная пара, которую детектор
            # противоречий обязан НЕ считать противоречием (_comparable_conditions
            # в query.py)
            if material == "Сплав X" and temp in {705, 720, 735}:
                prop = "твёрдость"
            direction = "increase" if (temp + material_index * 13) % 3 != 0 else "decrease"
            effect = 6 + ((temp_index + material_index) % 5) * 2
            if material == "Сплав X" and temp == 720:
                direction = "increase"
                effect = 12
            if material == "Сплав X" and temp == 735:
                direction = "decrease"
                effect = 4
            rows.append(
                {
                    "material": material,
                    "experiment_id": f"EXP-{counter:03d}",
                    "sample": f"S-{material_index + 1}-{temp_index + 1}",
                    "process": "старение" if temp >= 700 else "отжиг",
                    "temperature_c": float(temp),
                    "duration_h": float([2, 4, 8, 12][temp_index % 4]),
                    "property": prop,
                    "effect_direction": direction,
                    "effect_value": float(effect),
                    "effect_unit": "%",
                    "result_value": 240.0 + effect * (1 if direction == "increase" else -1),
                    "result_unit": "HV",
                    "lab": LABS[(counter - 1) % len(LABS)],
                    "team": f"Team-{(counter % 4) + 1}",
                    "equipment": f"Установка-{(counter % 5) + 1}",
                    "confidence": 0.88 + (counter % 7) / 100,
                }
            )
            counter += 1
    # Намеренно заложенное противоречие для демо детектора: повтор условий
    # «Сплав X, старение, 720 °C» с противоположным направлением эффекта
    # из другой лаборатории (team «Team-contradiction»)
    rows.append(
        {
            "material": "Сплав X",
            "experiment_id": f"EXP-{counter:03d}",
            "sample": "S-1-repeat",
            "process": "старение",
            "temperature_c": 720.0,
            "duration_h": 8.0,
            "property": "твёрдость",
            "effect_direction": "decrease",
            "effect_value": 3.0,
            "effect_unit": "%",
            "result_value": 237.0,
            "result_unit": "HV",
            "lab": "Лаборатория Юг",
            "team": "Team-contradiction",
            "equipment": "Установка-3",
            "confidence": 0.9,
        }
    )
    rows.extend(_build_nornickel_case_rows(counter + 1))
    return rows


def _build_nornickel_case_rows(start: int) -> list[dict[str, object]]:
    case_rows = [
        {
            "material": "шахтные воды",
            "sample": "MW-Norilsk-01",
            "process": "обессоливание",
            "temperature_c": 22.0,
            "duration_h": 2.0,
            "property": "сульфаты",
            "effect_direction": "decrease",
            "effect_value": 38.0,
            "effect_unit": "%",
            "result_value": 180.0,
            "result_unit": "мг/л",
            "lab": "Норильская лаборатория водоподготовки",
            "team": "Water-RnD",
            "equipment": "мембранная установка",
            "confidence": 0.93,
        },
        {
            "material": "шахтные воды",
            "sample": "MW-Norilsk-02",
            "process": "обессоливание",
            "temperature_c": 20.0,
            "duration_h": 2.5,
            "property": "хлориды",
            "effect_direction": "decrease",
            "effect_value": 31.0,
            "effect_unit": "%",
            "result_value": 260.0,
            "result_unit": "мг/л",
            "lab": "Норильская лаборатория водоподготовки",
            "team": "Water-RnD",
            "equipment": "мембранная установка",
            "confidence": 0.91,
        },
        {
            "material": "шахтные воды",
            "sample": "MW-Polar-03",
            "process": "закачка шахтных вод",
            "temperature_c": 18.0,
            "duration_h": 4.0,
            "property": "сухой остаток",
            "effect_direction": "decrease",
            "effect_value": 22.0,
            "effect_unit": "%",
            "result_value": 980.0,
            "result_unit": "мг/дм3",
            "lab": "Полярная лаборатория",
            "team": "GeoHydro",
            "equipment": "насосная схема",
            "confidence": 0.89,
        },
        {
            "material": "католит",
            "sample": "CAT-EL-01",
            "process": "электроэкстракция никеля",
            "temperature_c": 88.0,
            "duration_h": 12.0,
            "property": "скорость циркуляции католита",
            "effect_direction": "neutral",
            "effect_value": 0.3,
            "effect_unit": "м/с",
            "result_value": 0.3,
            "result_unit": "м/с",
            "lab": "Лаборатория электроэкстракции",
            "team": "Electro-RnD",
            "equipment": "ванна электроэкстракции",
            "confidence": 0.92,
        },
        {
            "material": "никелевые катоды",
            "sample": "NI-CATH-2024",
            "process": "электроэкстракция никеля",
            "temperature_c": 92.0,
            "duration_h": 16.0,
            "property": "выход металла",
            "effect_direction": "increase",
            "effect_value": 4.5,
            "effect_unit": "%",
            "result_value": 96.4,
            "result_unit": "%",
            "lab": "Лаборатория электроэкстракции",
            "team": "Electro-RnD",
            "equipment": "диафрагменная ячейка",
            "confidence": 0.94,
        },
    ]
    rows: list[dict[str, object]] = []
    for offset, row in enumerate(case_rows):
        payload = dict(row)
        payload["experiment_id"] = f"NN-EXP-{start + offset:03d}"
        rows.append(payload)
    return rows
