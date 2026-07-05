from __future__ import annotations

import base64
import csv
import io
import re
from collections.abc import Iterable
from typing import Protocol

from app.schemas import SourceFragment

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

try:
    import fitz  # PyMuPDF: рендер страниц без текстового слоя для мультимодального извлечения
except ImportError:  # pragma: no cover
    fitz = None

try:
    from docx import Document as DocxDocument
except ImportError:  # pragma: no cover
    DocxDocument = None

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None

try:
    from pptx import Presentation
except ImportError:  # pragma: no cover
    Presentation = None


class Parser(Protocol):
    name: str

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        ...


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def decode_text(content: bytes) -> str:
    """Легаси-экспорты из лабораторий часто в Windows-1251: сначала строгие
    кодировки, замена битых байтов — только последний фолбэк."""
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def split_text_blocks(text: str, max_chars: int = 3500) -> list[str]:
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not raw_blocks and text.strip():
        raw_blocks = [text.strip()]

    blocks: list[str] = []
    for block in raw_blocks:
        if len(block) <= max_chars:
            blocks.append(block)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", block)
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_chars:
                current = f"{current} {sentence}".strip()
            else:
                if current:
                    blocks.append(current)
                current = sentence
        if current:
            blocks.append(current)
    return blocks


class PlainTextParser:
    name = "plain-text"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        text = decode_text(content)
        blocks = split_text_blocks(text)
        return [
            SourceFragment(
                id=f"fragment-{document_id}-{index + 1}",
                document_id=document_id,
                version_id=version_id,
                page=1,
                element_type="paragraph",
                section="Uploaded text",
                text=block,
                normalized_text=normalize_text(block),
                metadata={"filename": filename, "ordinal": index + 1},
            )
            for index, block in enumerate(blocks)
        ]


class CsvParser:
    name = "csv"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        text = decode_text(content)
        reader = csv.reader(io.StringIO(text))
        header_row = next(reader, None)
        if header_row is None:
            return []
        headers = _unique_headers(header_row)
        fragments: list[SourceFragment] = []
        index = 0
        for values in reader:
            if not values:
                continue
            index += 1
            # Дубли заголовков не схлопываются (как в DictReader), а получают
            # суффикс; колонки сверх заголовка именуются по позиции
            row: dict[str, str] = {}
            for column, value in enumerate(values):
                key = headers[column] if column < len(headers) else f"column_{column + 1}"
                row[key] = value
            row_text = "; ".join(f"{key}={value}" for key, value in row.items() if value not in (None, ""))
            fragments.append(
                SourceFragment(
                    id=f"fragment-{document_id}-row-{index}",
                    document_id=document_id,
                    version_id=version_id,
                    page=1,
                    element_type="table_row",
                    section="Uploaded CSV",
                    text=row_text,
                    normalized_text=normalize_text(row_text),
                    metadata={"filename": filename, "row": index, "row_data": row},
                )
            )
        return fragments


class XlsxParser:
    name = "openpyxl"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        if load_workbook is None:
            return BinaryPlaceholderParser(reason="openpyxl is not installed").parse(
                document_id, version_id, filename, content
            )
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # pragma: no cover - depends on user files
            return BinaryPlaceholderParser(reason=f"XLSX parser failed: {exc}").parse(
                document_id, version_id, filename, content
            )

        fragments: list[SourceFragment] = []
        for sheet in workbook.worksheets:
            headers: list[str] = []
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                values = ["" if value is None else str(value).strip() for value in row]
                if not any(values):
                    continue
                if not headers:
                    headers = [value or f"column_{index + 1}" for index, value in enumerate(values)]
                    row_text = " | ".join(headers)
                    element_type = "xlsx_header"
                else:
                    pairs = [
                        f"{headers[index] if index < len(headers) else f'column_{index + 1}'}={value}"
                        for index, value in enumerate(values)
                        if value
                    ]
                    row_text = "; ".join(pairs)
                    element_type = "xlsx_row"
                if not row_text:
                    continue
                fragments.append(
                    SourceFragment(
                        id=f"fragment-{document_id}-xlsx-{_slug_id(sheet.title)}-{row_index}",
                        document_id=document_id,
                        version_id=version_id,
                        page=1,
                        element_type=element_type,
                        section=f"XLSX sheet {sheet.title}",
                        text=row_text,
                        normalized_text=normalize_text(row_text),
                        metadata={
                            "filename": filename,
                            "sheet": sheet.title,
                            "row": row_index,
                            "evidence_unit": True,
                            "parser": self.name,
                        },
                    )
                )

        if not fragments:
            return BinaryPlaceholderParser(reason="XLSX contains no readable cells").parse(
                document_id, version_id, filename, content
            )
        return fragments


def _page_is_mostly_image(page) -> bool:
    """Страница считается сканом, если растровые изображения покрывают
    больше половины её площади — независимо от длины текстового слоя."""
    try:
        page_area = float(page.width) * float(page.height)
        if page_area <= 0:
            return False
        covered = sum(
            max(0.0, float(image["x1"]) - float(image["x0"]))
            * max(0.0, float(image["bottom"]) - float(image["top"]))
            for image in page.images
        )
        return covered / page_area > 0.5
    except Exception:
        return False


class PdfParser:
    name = "pdfplumber"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        if pdfplumber is None:
            return BinaryPlaceholderParser(reason="pdfplumber is not installed").parse(
                document_id, version_id, filename, content
            )
        fragments: list[SourceFragment] = []
        render_doc = None
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                image_b64 = None
                # Порог, а не проверка на пустоту: у скана текстовый слой часто
                # не пуст (колонтитул, OCR-штамп), но содержимое живёт в картинке.
                # Длинный колонтитул проходит порог по тексту, поэтому вторым
                # сигналом служит растр, покрывающий большую часть страницы
                if len(text) < 40 or _page_is_mostly_image(page):
                    if fitz is not None:
                        if render_doc is None:
                            render_doc = fitz.open(stream=content, filetype="pdf")
                        try:
                            pixmap = render_doc[page_index - 1].get_pixmap(dpi=120)
                            image_b64 = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                        except Exception:
                            image_b64 = None
                    if not text:
                        text = f"PDF page {page_index} has no text layer; visual OCR adapter is required."
                blocks = split_text_blocks(text) or [text]
                for block_index, block in enumerate(blocks, start=1):
                    metadata = {
                        "filename": filename,
                        "page": page_index,
                        "block": block_index,
                        "evidence_unit": True,
                        "parser": self.name,
                    }
                    if image_b64 and block_index == 1:
                        metadata["image_b64"] = image_b64
                    fragments.append(
                        SourceFragment(
                            id=f"fragment-{document_id}-p{page_index}-{block_index}",
                            document_id=document_id,
                            version_id=version_id,
                            page=page_index,
                            element_type="pdf_page_text",
                            section="PDF evidence unit",
                            text=block,
                            normalized_text=normalize_text(block),
                            metadata=metadata,
                        )
                    )
        if render_doc is not None:
            render_doc.close()
        return fragments


class DocxParser:
    name = "python-docx"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        if DocxDocument is None:
            return BinaryPlaceholderParser(reason="python-docx is not installed").parse(
                document_id, version_id, filename, content
            )
        try:
            document = DocxDocument(io.BytesIO(content))
        except Exception as exc:  # pragma: no cover - depends on user files
            return BinaryPlaceholderParser(reason=f"DOCX parser failed: {exc}").parse(
                document_id, version_id, filename, content
            )

        fragments: list[SourceFragment] = []
        section = "DOCX evidence unit"
        # У DOCX нет страниц: адрес фрагмента — единый сквозной номер блока,
        # общий для абзацев и табличных строк (иначе адреса конфликтуют)
        block_index = 0
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = (paragraph.style.name if paragraph.style else "") or ""
            if style_name.lower().startswith("heading"):
                section = text[:180]
            for block in split_text_blocks(text):
                block_index += 1
                fragments.append(
                    SourceFragment(
                        id=f"fragment-{document_id}-docx-p{block_index}",
                        document_id=document_id,
                        version_id=version_id,
                        page=block_index,
                        element_type="docx_paragraph",
                        section=section,
                        text=block,
                        normalized_text=normalize_text(block),
                        metadata={
                            "filename": filename,
                            "paragraph": block_index,
                            "style": style_name,
                            "evidence_unit": True,
                            "parser": self.name,
                        },
                    )
                )

        for table_index, table in enumerate(document.tables, start=1):
            for row_index, row in enumerate(table.rows, start=1):
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells if cell.text.strip()]
                row_text = " | ".join(cells)
                if not row_text:
                    continue
                block_index += 1
                fragments.append(
                    SourceFragment(
                        id=f"fragment-{document_id}-docx-t{table_index}-r{row_index}",
                        document_id=document_id,
                        version_id=version_id,
                        page=block_index,
                        element_type="docx_table_row",
                        section=f"DOCX table {table_index}",
                        text=row_text,
                        normalized_text=normalize_text(row_text),
                        metadata={
                            "filename": filename,
                            "table": table_index,
                            "row": row_index,
                            "ordinal": block_index,
                            "evidence_unit": True,
                            "parser": self.name,
                        },
                    )
                )

        if not fragments:
            return BinaryPlaceholderParser(reason="DOCX contains no extractable text").parse(
                document_id, version_id, filename, content
            )
        return fragments


class PptxParser:
    name = "python-pptx"

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        if Presentation is None:
            return BinaryPlaceholderParser(reason="python-pptx is not installed").parse(
                document_id, version_id, filename, content
            )
        try:
            presentation = Presentation(io.BytesIO(content))
        except Exception as exc:  # pragma: no cover - depends on user files
            return BinaryPlaceholderParser(reason=f"PPTX parser failed: {exc}").parse(
                document_id, version_id, filename, content
            )

        fragments: list[SourceFragment] = []
        for slide_index, slide in enumerate(presentation.slides, start=1):
            shape_texts = list(_slide_texts(slide.shapes))
            text = "\n\n".join(shape_texts).strip()
            if not text:
                continue
            for block_index, block in enumerate(split_text_blocks(text), start=1):
                fragments.append(
                    SourceFragment(
                        id=f"fragment-{document_id}-pptx-s{slide_index}-{block_index}",
                        document_id=document_id,
                        version_id=version_id,
                        page=slide_index,
                        element_type="pptx_slide_text",
                        section=f"PPTX slide {slide_index}",
                        text=block,
                        normalized_text=normalize_text(block),
                        metadata={
                            "filename": filename,
                            "slide": slide_index,
                            "block": block_index,
                            "evidence_unit": True,
                            "parser": self.name,
                        },
                    )
                )

        if not fragments:
            return BinaryPlaceholderParser(reason="PPTX contains no extractable text").parse(
                document_id, version_id, filename, content
            )
        return fragments


class BinaryPlaceholderParser:
    name = "binary-placeholder"

    def __init__(self, reason: str | None = None):
        self.reason = reason

    def parse(self, document_id: str, version_id: str, filename: str, content: bytes) -> list[SourceFragment]:
        text = (
            f"Файл {filename} зарегистрирован, но текст не был извлечен. "
            "Для этого формата нужен отдельный адаптер, OCR или конвертация в поддерживаемый формат."
        )
        if self.reason:
            text = f"{text} Причина: {self.reason}."
        return [
            SourceFragment(
                id=f"fragment-{document_id}-binary-1",
                document_id=document_id,
                version_id=version_id,
                page=1,
                element_type="document_placeholder",
                section="Parser adapter boundary",
                text=text,
                normalized_text=normalize_text(text),
                metadata={"filename": filename, "bytes": len(content), "parser": self.name, "reason": self.reason},
            )
        ]


# Единственный источник правды по поддерживаемым форматам: и выбор парсера,
# и проверка расширения идут через этот реестр
_PARSER_BY_EXTENSION: dict[str, type] = {
    ".csv": CsvParser,
    ".xlsx": XlsxParser,
    ".xlsm": XlsxParser,
    ".pdf": PdfParser,
    ".docx": DocxParser,
    ".docm": DocxParser,
    ".pptx": PptxParser,
    ".txt": PlainTextParser,
    ".md": PlainTextParser,
    ".json": PlainTextParser,
}

SUPPORTED_EXTENSIONS = set(_PARSER_BY_EXTENSION)


def choose_parser(filename: str) -> Parser:
    parser_class = _PARSER_BY_EXTENSION.get(extension_of(filename))
    if parser_class is not None:
        return parser_class()
    # Неизвестный формат намеренно регистрируется placeholder-фрагментом
    return BinaryPlaceholderParser(reason=f"unsupported extension {extension_of(filename) or '<none>'}")


def extension_of(filename: str) -> str:
    match = re.search(r"(\.[^.\\/]+)$", filename.lower())
    return match.group(1) if match else ""


def is_supported_file(filename: str) -> bool:
    return extension_of(filename) in SUPPORTED_EXTENSIONS


def _unique_headers(raw: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(raw):
        name = (value or "").strip() or f"column_{index + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        headers.append(name if count == 1 else f"{name}_{count}")
    return headers


def _slug_id(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part) or "sheet"


def _slide_texts(shapes: Iterable) -> Iterable[str]:
    for shape in shapes:
        if getattr(shape, "has_text_frame", False):
            text = "\n".join(paragraph.text for paragraph in shape.text_frame.paragraphs).strip()
            if text:
                yield text
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                row_text = " | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells if cell.text.strip())
                if row_text:
                    yield row_text
        if hasattr(shape, "shapes"):
            yield from _slide_texts(shape.shapes)
