FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AQI_RUN_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir ".[api]" \
    && python -c "import pathlib, aqi_pipeline; p = pathlib.Path(aqi_pipeline.__file__).parent; assert (p / 'report_template.html').is_file(), 'missing report_template.html'; assert (p / 'static' / 'index.html').is_file(), 'missing static/index.html'" \
    && groupadd --system --gid 10001 aqi \
    && useradd --system --uid 10001 --gid aqi --create-home --home-dir /home/aqi aqi \
    && mkdir -p /data \
    && chown -R aqi:aqi /data /app

USER aqi
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["aqi-pipeline", "--run-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8000"]
