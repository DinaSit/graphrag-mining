from __future__ import annotations

import unittest

from app.pipeline.validation import (
    extract_quantity_hits,
    normalize_quantity,
    validate_candidate_numbers,
)


def _hits(text: str) -> list[tuple[float, str, str]]:
    return [(hit.value, hit.unit, hit.kind) for hit in extract_quantity_hits(text)]


class QuantityExtractionTest(unittest.TestCase):
    def test_range_yields_both_ends_not_negative(self) -> None:
        hits = extract_quantity_hits("старение при 700-750 °C")

        values = sorted(hit.value for hit in hits)
        self.assertEqual(values, [700.0, 750.0])
        self.assertTrue(all(hit.kind == "temperature" for hit in hits))
        self.assertNotIn(-750.0, values)

    def test_range_with_dots_and_dashes(self) -> None:
        for text in ("200...300 мг/л", "200–300 мг/л", "200—300 мг/л"):
            values = sorted(hit.value for hit in extract_quantity_hits(text))
            self.assertEqual(values, [200.0, 300.0], text)

    def test_thousands_separator(self) -> None:
        hits = extract_quantity_hits("сухой остаток 12 000 мг/л")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].value, 12000.0)
        self.assertEqual(hits[0].kind, "concentration")

    def test_seconds_are_not_celsius(self) -> None:
        hits = extract_quantity_hits("перемешивание 30 с при 85 °C")

        temperatures = [hit.value for hit in hits if hit.kind == "temperature"]
        durations = [hit for hit in hits if hit.kind == "duration"]
        self.assertEqual(temperatures, [85.0])
        self.assertEqual(len(durations), 1)
        self.assertAlmostEqual(durations[0].normalized_value, 30 / 3600)

    def test_uppercase_cyrillic_c_is_celsius(self) -> None:
        hits = extract_quantity_hits("температура электролита 80 С")

        self.assertEqual([(hit.value, hit.kind) for hit in hits], [(80.0, "temperature")])

    def test_single_letters_inside_words_are_not_units(self) -> None:
        self.assertEqual(_hits("на 30 в первый час"), [])
        self.assertEqual(_hits("добавить 2 части"), [])

    def test_minutes_convert_to_hours(self) -> None:
        hits = extract_quantity_hits("выдержка 10 мин")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "duration")
        self.assertAlmostEqual(hits[0].normalized_value, 10 / 60)

    def test_cyrillic_ph_is_extracted(self) -> None:
        hits = extract_quantity_hits("рН 8,5 раствора")

        self.assertEqual([(hit.value, hit.unit) for hit in hits], [(8.5, "pH")])

    def test_negative_temperature_is_preserved(self) -> None:
        for text in ("охлаждение до −40 °C", "охлаждение до -40 °C"):
            hits = extract_quantity_hits(text)
            self.assertEqual([(hit.value, hit.kind) for hit in hits], [(-40.0, "temperature")], text)

    def test_tonnes_per_day_convert_to_tonnes_per_hour(self) -> None:
        self.assertEqual(normalize_quantity(240, "т/сут"), ("mass_flow", 10.0, "t/h"))

        hits = extract_quantity_hits("подача 240 т/сут")
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].normalized_value, 10.0)
        self.assertEqual(hits[0].normalized_unit, "t/h")


class NumberValidationTest(unittest.TestCase):
    def test_incompatible_units_do_not_confirm_value(self) -> None:
        result = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "концентрация никеля", "value": 5, "unit": "мг/л"}]},
            "концентрация никеля в растворе 5 г/л",
            {"ranges_by_name": {}},
        )

        self.assertFalse(result["validated"])
        self.assertTrue(result["issues"])

    def test_same_units_confirm_value(self) -> None:
        result = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "концентрация никеля", "value": 5, "unit": "мг/л"}]},
            "концентрация никеля в растворе 5 мг/л",
            {"ranges_by_name": {}},
        )

        self.assertTrue(result["validated"], result["issues"])

    def test_range_confirms_extracted_upper_bound(self) -> None:
        result = validate_candidate_numbers({"temperature_c": 750}, "старение при 700-750 °C")

        self.assertTrue(result["validated"], result["issues"])
        self.assertIn("temperature_c", result["matched_fields"])

    def test_minutes_confirm_duration_in_hours(self) -> None:
        result = validate_candidate_numbers({"duration_h": 0.5}, "выдержка 30 мин при 85 °C")

        self.assertTrue(result["validated"], result["issues"])
        self.assertIn("duration_h", result["matched_fields"])

    def test_seconds_do_not_confirm_temperature(self) -> None:
        result = validate_candidate_numbers({"temperature_c": 30}, "перемешивание 30 с при 85 °C")

        self.assertFalse(result["validated"])

    def test_effect_value_checked_in_effect_unit(self) -> None:
        result = validate_candidate_numbers(
            {"effect_value": 0.3, "effect_unit": "м/с"},
            "скорость циркуляции католита 0.3 м/с",
        )

        self.assertTrue(result["validated"], result["issues"])
        self.assertIn("effect_value", result["matched_fields"])

    def test_negative_temperature_does_not_trip_plausibility(self) -> None:
        result = validate_candidate_numbers(
            {"numeric_parameters": [{"name": "температура шахтной воды", "value": -40, "unit": "°C"}]},
            "температура шахтной воды достигала −40 °C",
            {"ranges_by_name": {}},
        )

        self.assertTrue(result["validated"], result["issues"])


if __name__ == "__main__":
    unittest.main()
