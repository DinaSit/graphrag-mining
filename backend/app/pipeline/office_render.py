from __future__ import annotations

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def convert_office_to_pdf(content: bytes, suffix: str, timeout: float = 40) -> bytes | None:
    """Конвертирует офисный файл (DOCX/DOCM/PPTX) в PDF через headless LibreOffice.

    Механическая конвертация без LLM: входные байты записываются во временный файл
    с исходным расширением, конвертация — subprocess soffice --convert-to pdf.
    Возвращает БАЙТЫ PDF или None при любом сбое: отсутствие soffice, таймаут,
    ошибка или пустой результат конвертации. Исключения наружу не пробрасываются —
    только warning в лог. Временные файлы удаляются.
    """
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    with tempfile.TemporaryDirectory(prefix="office-render-") as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        # Отдельный профиль LibreOffice во временной папке, чтобы избежать
        # блокировки общего профиля при параллельных конвертациях
        profile_uri = f"file://{os.path.join(tmpdir, 'lo-profile')}"
        try:
            with open(input_path, "wb") as handle:
                handle.write(content)

            subprocess.run(
                [
                    "soffice",
                    "--headless",
                    f"-env:UserInstallation={profile_uri}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    input_path,
                ],
                capture_output=True,
                timeout=timeout,
                check=True,
            )
        except FileNotFoundError:
            logger.warning("Office->PDF skipped: soffice (LibreOffice) is not available")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("Office->PDF skipped: soffice conversion timed out after %ss", timeout)
            return None
        except subprocess.CalledProcessError as exc:
            logger.warning("Office->PDF skipped: soffice conversion failed: %s", exc)
            return None
        except Exception as exc:  # pragma: no cover - неожиданные ошибки ФС/подпроцесса
            logger.warning("Office->PDF skipped: unexpected error: %s", exc)
            return None

        # soffice именует результат по базовому имени входа: input<suffix> → input.pdf
        pdf_path = os.path.join(tmpdir, "input.pdf")
        try:
            with open(pdf_path, "rb") as handle:
                pdf_bytes = handle.read()
        except OSError:
            logger.warning("Office->PDF skipped: soffice produced no PDF for %s", suffix)
            return None

    return pdf_bytes or None
