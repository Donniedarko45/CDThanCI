FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir ".[api]"

# Database, model registry and reports are written under /data.
ENV AQI_RUN_DIR=/data
VOLUME /data
EXPOSE 8000

CMD ["aqi-pipeline", "--run-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8000"]
