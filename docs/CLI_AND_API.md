# CLI and HTTP API reference

Everything the pipeline does is available from one command, `aqi-pipeline` (entry point
`aqi_pipeline.cli:main`, installed by `pip install -e ".[api,dev]"`). The HTTP API reads the
same SQLite store and model registry, so the scheduler and the API can run as separate
processes over one run directory. For what happens inside a cycle, see
[MONITORING.md](MONITORING.md) and [RETRAINING.md](RETRAINING.md).

## Global options

| Option | Meaning |
| --- | --- |
| `--config PATH` | YAML config. Every key is optional; the defaults are in `src/aqi_pipeline/config.py` and listed with comments in `config/default.yaml`. Unknown keys are rejected. |
| `--run-dir DIR` | Where the database (`pipeline.db`), the model registry (`registry/`) and the reports (`reports/`) are written. Overrides `run_dir` from the config (default `runs/default`). |

Both go before the command, for example `aqi-pipeline --run-dir runs/demo status`.

## Commands

| Command | What it does | Options |
| --- | --- | --- |
| `simulate` | The full demo: generates a synthetic station, trains the first model on six months of history, replays a production year one cycle per day, scores the pipeline against three baselines, and writes `summary.json` and the HTML report. **Deletes the run directory first.** | `--days N` shortens production to N days; `--quiet` |
| `bootstrap` | Loads history from the configured `csv` or `open_meteo` source, backtests every candidate model, trains the best one and makes it champion `v001`. | `--until TIME` (default: now) |
| `run-cycle` | One cycle up to `TIME`: ingest, score matured forecasts, forecast, monitor, decide, maybe retrain, check probation. Prints the decision and its reason. Running it twice for the same hour changes nothing. | `--now TIME` (default: the current hour, UTC) |
| `schedule` | Runs a cycle for the current UTC hour in a loop. A failed cycle is printed and the next one retries. | `--every SECONDS` (default 3600) |
| `status` | Prints the champion, the shadow, the latest decision and its reason, safe mode, accuracy and drift as JSON. | |
| `report` | Rebuilds `reports/monitoring_report.html`. | `--out PATH` |
| `rollback` | Restores the shadow as champion and resets the CUSUM, the accuracy streak and safe mode. Only possible while a shadow exists (during probation). | `--reason TEXT`, `--now TIME` |
| `serve` | Starts the HTTP API and the dashboard with uvicorn. | `--host` (default `127.0.0.1`), `--port` (default `8000`) |

### Typical sessions

```bash
# Demo on synthetic data (the sample run took under 3 minutes)
aqi-pipeline --run-dir runs/demo simulate
aqi-pipeline --run-dir runs/demo status
aqi-pipeline --run-dir runs/demo serve        # then open http://127.0.0.1:8000/

# A quicker demo: 30 days of production instead of a year
aqi-pipeline --run-dir runs/quick simulate --days 30

# A real station from a CSV export (copy and edit config/csv_station.example.yaml)
aqi-pipeline --config my_station.yaml bootstrap --until 2025-12-31
aqi-pipeline --config my_station.yaml run-cycle --now "2026-01-01 23:00"
aqi-pipeline --config my_station.yaml report

# Live data from Open-Meteo (no API key), one cycle per hour
aqi-pipeline --config config/open_meteo.example.yaml bootstrap
aqi-pipeline --config config/open_meteo.example.yaml schedule --every 3600
```

## HTTP API

`aqi-pipeline serve` hands `--run-dir` and `--config` to the API through the environment
variables `AQI_RUN_DIR` and `AQI_CONFIG`; set them yourself when starting the app another way
(for example `uvicorn aqi_pipeline.api:app`). Interactive OpenAPI documentation is served at
`/docs`.

### Pages

| Endpoint | Returns |
| --- | --- |
| `GET /` | The live dashboard: one self-contained HTML file (`dashboard.html`) with no external assets |
| `GET /report` | The monitoring report, rebuilt automatically when the database is newer than the saved file; a 404 page until something has run |
| `GET /docs` | The OpenAPI (Swagger) UI |

### JSON endpoints

| Endpoint | Parameters | Returns |
| --- | --- | --- |
| `GET /health` | | `status`, the champion version and the last observation time |
| `GET /forecast` | `hours` (1–168, default 24) | The upcoming served forecasts: target time, AQI, category, `source` (`model`, or `persistence` in safe mode) and whether the inputs were clean |
| `GET /model` | | The champion's metadata (training data, validation scores or gate result, input importance), the shadow version and the last 20 registry actions |
| `GET /monitoring` | | The latest cycle record: data quality, drift, accuracy, CUSUM, safe mode, the decision, and any retraining or probation result. 404 before the first cycle |
| `GET /events` | `limit` (1–1000, default 50) | The newest audit-log entries, oldest first |
| `GET /dashboard/data` | `hours` (1–168, default 72) | Everything the dashboard draws: status, the current AQI and readings, upcoming forecasts, an hourly actual-vs-forecast series and the report data |
| `GET /observations/recent` | `hours` (1–720, default 216), `sample` (default `false`) | Recent raw hourly observations; with `sample=true` and an empty store, a synthetic sample instead |
| `POST /predict` | JSON body (below) | A 24-hour-ahead forecast from the champion for the observations you send. Stateless: nothing is stored |

Errors: `503` when no model has been trained (and, for `/forecast`, when there are no
observations yet); `422` for invalid input to `/predict`.

### `POST /predict`

Send at least 24 hourly rows, oldest first, as `{"observations": [...]}`; nine days (216 rows)
gives every feature its full history. Each row has a `timestamp` and any of the 14 observation
columns; missing values may be `null`.

| Columns | Unit |
| --- | --- |
| `pm25`, `pm10`, `no2`, `so2`, `o3` | µg/m³ |
| `co` | mg/m³ |
| `temperature` | °C |
| `humidity` | % |
| `wind_speed` | m/s |
| `precipitation` | mm per hour |
| `fc_temperature`, `fc_humidity`, `fc_wind_speed`, `fc_precipitation` | Weather expected over the next 24 hours, as forecast at the row's time (mean; total for precipitation) |

`GET /observations/recent` returns rows in exactly this shape, so the stored history can be
scored directly:

```bash
curl -s "http://127.0.0.1:8000/observations/recent?hours=216" \
  | python3 -c 'import json, sys; print(json.dumps({"observations": json.load(sys.stdin)["observations"]}))' \
  | curl -s -X POST http://127.0.0.1:8000/predict -H "Content-Type: application/json" -d @-
```

The response (values illustrative):

```json
{
  "champion": "v006",
  "made_at": "2026-08-31 23:00:00",
  "target_time": "2026-09-01 23:00:00",
  "aqi": 74.3,
  "category": "Satisfactory",
  "current_aqi": 68.0,
  "inputs_clean": true,
  "data_quality": "pass",
  "issues": []
}
```

The rows pass through the same quality gate as stored data, so the response reports the data
quality status and any issues (gaps, stuck sensors, out-of-range readings). The dashboard's
*What-If* playground is built on this endpoint: it loads 216 recent hours (or a synthetic sample
when the store is empty), lets you adjust the inputs and posts them to `/predict`.

## Running it as a service

**cron**, one cycle per hour:

```
0 * * * *  aqi-pipeline --config /etc/aqi/station.yaml run-cycle
```

**Docker Compose** (`docker-compose.yml`): two containers share one volume, `aqi-data`, mounted
at `/data`. `scheduler` runs `schedule --every 3600` with the Open-Meteo example config, and
`api` serves on port 8000. Bootstrap once, then start both:

```bash
docker compose run --rm scheduler aqi-pipeline --config /app/config/open_meteo.example.yaml --run-dir /data bootstrap
docker compose up -d
```

The image (`Dockerfile`) is `python:3.12-slim` with the `api` extra installed; by default it
serves the API on `0.0.0.0:8000` with the run directory at `/data`. The README notes that the
Dockerfile and compose file were not built or run in the environment the project was written in.
