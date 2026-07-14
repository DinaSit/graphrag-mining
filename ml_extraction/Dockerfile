FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Языковая модель перевода RU→EN скачивается в образ на этапе сборки
# (нужна сеть при build); во время выполнения перевод выполняется офлайн из этого кэша
RUN python -c "import argostranslate.package as p; p.update_package_index(); \
    pkg = next(x for x in p.get_available_packages() if x.from_code == 'ru' and x.to_code == 'en'); \
    p.install_from_path(pkg.download())"

COPY app ./app

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
