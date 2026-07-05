from __future__ import annotations

import io
import unittest

from app.pipeline.parsers import CsvParser, DocxDocument, DocxParser, PlainTextParser, decode_text


class DecodeTextTest(unittest.TestCase):
    def test_utf8_is_decoded_strictly(self) -> None:
        self.assertEqual(decode_text("Сплав X".encode("utf-8")), "Сплав X")

    def test_cp1251_legacy_export_is_readable(self) -> None:
        raw = "материал;значение\nСплав X;12".encode("cp1251")

        text = decode_text(raw)

        self.assertIn("Сплав X", text)
        self.assertNotIn("�", text)

    def test_plain_text_parser_keeps_cp1251_content(self) -> None:
        fragments = PlainTextParser().parse("d1", "v1", "отчет.txt", "Твёрдость выросла.".encode("cp1251"))

        self.assertEqual(len(fragments), 1)
        self.assertIn("Твёрдость", fragments[0].text)


class CsvParserTest(unittest.TestCase):
    def test_duplicate_headers_keep_all_columns(self) -> None:
        raw = "эксперимент,значение,единица,значение,единица\nEXP-1,700,C,750,C\n".encode("utf-8")

        fragments = CsvParser().parse("d1", "v1", "data.csv", raw)

        self.assertEqual(len(fragments), 1)
        row = fragments[0].metadata["row_data"]
        self.assertEqual(row["значение"], "700")
        self.assertEqual(row["значение_2"], "750")
        self.assertIn("значение=700", fragments[0].text)
        self.assertIn("значение_2=750", fragments[0].text)

    def test_extra_cells_are_named_by_position(self) -> None:
        raw = "a,b\n1,2,3\n".encode("utf-8")

        fragments = CsvParser().parse("d1", "v1", "data.csv", raw)

        self.assertEqual(fragments[0].metadata["row_data"], {"a": "1", "b": "2", "column_3": "3"})


@unittest.skipIf(DocxDocument is None, "python-docx не установлен")
class DocxNumberingTest(unittest.TestCase):
    def test_paragraphs_and_table_rows_share_block_counter(self) -> None:
        document = DocxDocument()
        document.add_paragraph("Первый абзац с результатами эксперимента.")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "температура"
        table.rows[0].cells[1].text = "750"
        buffer = io.BytesIO()
        document.save(buffer)

        fragments = DocxParser().parse("d1", "v1", "отчет.docx", buffer.getvalue())

        paragraphs = [f for f in fragments if f.element_type == "docx_paragraph"]
        rows = [f for f in fragments if f.element_type == "docx_table_row"]
        self.assertEqual([f.page for f in paragraphs], [1])
        # Табличная строка продолжает сквозную нумерацию, а не начинает свою
        self.assertEqual([f.page for f in rows], [2])


if __name__ == "__main__":
    unittest.main()
