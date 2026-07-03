FROM python:3.12-slim

WORKDIR /app

COPY streamlit_app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY streamlit_app/app.py /app/app.py

ENV BACKEND_URL=http://backend:8000

EXPOSE 8501

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2).read()" || exit 1

CMD ["streamlit", "run", "/app/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]

