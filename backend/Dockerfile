FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

# LibreOffice Writer/Impress + шрифты (кириллица/латиница) — PDF-превью
# DOCX/PPTX при инжесте (headless soffice --convert-to pdf)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-impress \
        libreoffice-core \
        fonts-dejavu \
        fonts-liberation \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Просмотрщик pdf.js (подсветка цитат внутри документа) устанавливается при сборке —
# в репозитории библиотека не хранится. Слой кэшируется: повторное скачивание
# только при смене версии. cmaps (17MB, нужны только CJK-шрифтам) удаляются.
# COPY backend ниже объединяется с этой папкой, не перезаписывая её содержимое
RUN python - <<'PDFJS'
import io, os, shutil, urllib.request, zipfile
URL = "https://github.com/mozilla/pdf.js/releases/download/v3.11.174/pdfjs-3.11.174-dist.zip"
dst = "/app/backend/ui/pdfjs"
data = urllib.request.urlopen(URL, timeout=120).read()
zipfile.ZipFile(io.BytesIO(data)).extractall(dst)
shutil.rmtree(os.path.join(dst, "web", "cmaps"), ignore_errors=True)
for name in ("web/debugger.js", "web/debugger.css"):
    path = os.path.join(dst, name)
    if os.path.exists(path):
        os.remove(path)
PDFJS

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY domain /app/domain

WORKDIR /app/backend

EXPOSE 8000

# Проверка через python: в slim-образе нет curl/wget
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
