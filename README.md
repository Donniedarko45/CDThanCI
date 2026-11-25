# AQI pipeline

[![CI](https://github.com/Donniedarko45/CDThanCI/actions/workflows/ci.yml/badge.svg)](https://github.com/Donniedarko45/CDThanCI/actions/workflows/ci.yml)
[![CD](https://github.com/Donniedarko45/CDThanCI/actions/workflows/cd.yml/badge.svg)](https://github.com/Donniedarko45/CDThanCI/actions/workflows/cd.yml)

An automated pipeline that forecasts India's CPCB Air Quality Index 24 hours ahead
from hourly station data, monitors its own inputs and accuracy, and decides for
itself when to retrain, which data to retrain on, and whether the new model is
good enough to go live.

```
ingest -> quality gate -> forecast -> monitor -> decide
                             ^                      | retrain
                          registry <- gate <- challengers
```

## Quick start

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows pwsh: .venv\Scripts\Activate.ps1
pip install -e ".[api,dev]"

# Full demo: a simulated station's first year in production.
aqi-pipeline --run-dir runs/demo simulate

# Open runs/demo/reports/monitoring_report.html, then:
aqi-pipeline --run-dir runs/demo status
aqi-pipeline --run-dir runs/demo serve        # http://127.0.0.1:8000/ (Operations Console), /report, /docs

# Quality, lint, coverage, and package checks:
ruff check src tests
ruff format --check src tests
pytest -q --cov=aqi_pipeline --cov-report=term-missing
python -m build
```

A copy of the demo's output is in `sample_run/` (report and summary) if you only
want to look.

## Docker & Compose demo

Run the complete one-command live demo (seeds a 45-day synthetic station into `/data`
and starts the Operations Console on port `8000`):

```bash
docker compose --profile demo up --build
```

Open `http://127.0.0.1:8000/` for the Operations Console (`#/overview`, `#/diagnostics`,
`#/simulator`, `#/registry`, `#/pipeline`), `http://127.0.0.1:8000/report` for the
standalone monitoring report, and `http://127.0.0.1:8000/docs` for Swagger UI.

For continuous live station operation (Open-Meteo hourly scheduler + API):

```bash
docker compose --profile live run --rm scheduler \
  aqi-pipeline --config /app/config/open_meteo.example.yaml --run-dir /data bootstrap
docker compose --profile live up -d
```

The container image (`Dockerfile`) runs on `python:3.12-slim` as non-root user `aqi`,
stores persistent state in `/data`, verifies packaged static assets at build time,
and exposes a built-in Docker `HEALTHCHECK` against `GET /health`.

```bash
docker build -t aqi-pipeline:local .
docker run --rm -v aqi-local:/data aqi-pipeline:local aqi-pipeline --run-dir /data simulate --days 15 --quiet
docker run -d --name aqi-api -p 8000:8000 -v aqi-local:/data aqi-pipeline:local
```

## Multi-View Operations Console & HTTP API

`GET /` serves the offline modular Operations Console (`src/aqi_pipeline/static/`) with
light/dark themes, live auto-refresh, and hash routing across five views:

| View | Route | Focus |
| --- | --- | --- |
| Overview & Forecast | `#/overview` | CPCB AQI gauge, pollutant cards, interactive `24h`–`168h` forecast chart, horizon export |
| Drift & Diagnostics | `#/diagnostics` | PSI/novelty drift breakdown, feature importance, monthly MAE comparison, CUSUM & cycle signals |
| What-If & Batch Studio | `#/simulator` | 9-day (`216h`) What-If scenarios (`POST /predict`) and CSV/JSON batch scoring (`POST /predict/batch`) |
| Registry & Audit Log | `#/registry` | Model version lineage (`champion`, `shadow`, `archived`, `rolled_back`), gate outcomes, event log |
| Pipeline & Config | `#/pipeline` | 7-stage pipeline diagram, sanitized runtime configuration (`GET /api/config`), store telemetry |

API endpoints:
- **UI & static assets**: `GET /`, `GET /static/*`, `GET /report`, `GET /favicon.ico`, `GET /docs`
- **Dashboard & analytics**: `GET /dashboard/data?hours=72`, `GET /observations/recent?hours=216`, `GET /api/config`, `GET /api/analytics`
- **Core & inference**: `GET /health`, `GET /forecast?hours=24`, `GET /model`, `GET /monitoring`, `GET /events?limit=50`, `POST /predict`, `POST /predict/batch`

## CI/CD & dependency automation

- **CI (`.github/workflows/ci.yml`)**: triggers on `push`, `pull_request`, and `workflow_dispatch` with concurrency cancellation across four jobs:
  - `lint-and-package`: `ruff check src tests`, `ruff format --check src tests`, `node --check` on `src/aqi_pipeline/static/*.js`, `python -m build`, wheel asset verification, and `dist/` upload.
  - `test-matrix`: Python `3.10` and `3.12` test matrix with `pytest --cov=aqi_pipeline`, coverage XML, JUnit XML, and `$GITHUB_STEP_SUMMARY` reporting.
  - `e2e-and-ui`: 15-day simulation, live API endpoint smoke checks, headless Playwright Chromium UI verification across views/horizons/themes, and screenshot + HTML report artifacts.
  - `docker-smoke`: `docker compose config -q`, Buildx build, non-root UID check, seeded container healthcheck, and live HTTP verification.
- **CD (`.github/workflows/cd.yml`)**: triggers on pushes to `main`, `v*` tags, and `workflow_dispatch`; builds Python distributions and a demo monitoring report, publishes multi-tagged images to `ghcr.io/donniedarko45/cdthanci` with OCI labels and build provenance attestation (`actions/attest-build-provenance`), and creates GitHub Releases on `v*` tags.
- **Dependabot (`.github/dependabot.yml`)**: weekly update checks for `pip`, `github-actions`, and `docker`.

## What it does each cycle

| Stage | Module | What happens |
| --- | --- | --- |
| Ingest | `sources.py` | New hourly rows from a CSV file, the Open-Meteo API or the simulator |
| Quality gate | `validation.py` | Gaps, duplicates, impossible values and stuck sensors are flagged; raw values are kept |
| Forecast | `features.py`, `modeling.py` | 54 features, AQI 24 hours ahead, from the champion (and the shadow during probation) |
| Monitor | `drift.py`, `performance.py` | Input drift now; accuracy when the true values arrive 24 hours later |
| Decide | `policy.py` | `none`, `watch`, `hold` or `retrain`, with the reason recorded |
| Retrain and gate | `retrain.py` | Challengers on three data strategies; promoted only if clearly better on held-out recent days |
| Registry | `registry.py` | Versioned models with champion / shadow / archived / rolled-back stages |

Design choices worth knowing:

- **Sensor faults are not drift.** Forecasts made from flagged inputs, or scored
  against flagged labels, are excluded from accuracy monitoring and from training.
  While recent PM data is unreliable the decision is `hold`.
- **Drift respects seasons.** The recent window is compared with training data from
  the same time of year, with alarm thresholds calibrated on the training history;
  readings outside the whole training range count as drift within days.
- **Accuracy is judged against persistence** ("tomorrow = today"), so a hard week
  for every method does not look like model decay.
- **Retraining is gated.** A challenger must cut MAE by 5% with 95% bootstrap
  probability on the most recent days, then survives a 7-day probation against the
  previous model or is rolled back automatically.
- **Safe mode.** While the model does worse than persistence, persistence is served.

The design document has the full reasoning.

## Results on the simulated year

The demo simulates a station that goes live on 1 September 2025 with six months
of history, so its first smoke season and first winter are conditions the model
has never seen. Three events are injected: a five-day sensor fault, a PM10
analyser recalibration (+30%), and a rise in ozone production from May 2026.
The pipeline is compared with a model that is never retrained, with fixed monthly
retraining (model re-selected and always deployed), and with persistence. All
are scored on the same 8,585 hours.

| Method | MAE (AQI points) | RMSE | Category correct | Retrainings |
| --- | ---: | ---: | ---: | ---: |
| This pipeline | 23.8 | 37.2 | 73.3% | 12 attempts, 6 deployed |
| Fixed monthly retraining | 29.2 | 54.7 | 70.9% | 12, all deployed |
| Persistence | 28.6 | 42.9 | 67.3% | none |
| Never retrained | 69.7 | 108.0 | 49.3% | none |

What the numbers do and do not show:

- The gain over monthly retraining (19% lower MAE) comes from **one month**. In
  October 2025 the smoke season began; the pipeline noticed within days, fell
  back to persistence, and deployed a new model on 15 October. The monthly
  schedule kept serving a model that had never seen smoke until 1 November
  (MAE about 40 against 116 for that month).
- **Outside October the monthly schedule was slightly better** (MAE 21.0 against
  22.3). With a short history, simply refitting on more data every month is hard
  to beat; the pipeline's gate rejected half its challengers.
- One of six promotions was rolled back after probation. The sensor fault led to
  seven `hold` decisions and no retraining. The recalibration triggered nothing.
- The thresholds were tuned while developing on this run (seed 7). On two other
  seeds, run once with the same settings, the pipeline was again ahead: MAE 20.9
  against 27.2 for monthly retraining (seed 11) and 19.1 against 28.5 (seed 21).
  Those runs were less tidy, with 2 and 4 rollbacks.
- This is simulated data. It shows how the pipeline behaves, not how accurate it
  would be on a real station.

## Using real data

**CSV from a station** (one row per hour): copy `config/csv_station.example.yaml`,
map your column names, then

```bash
aqi-pipeline --config my_station.yaml bootstrap --until 2025-12-31
aqi-pipeline --config my_station.yaml run-cycle --now "2026-01-01 23:00"   # or from cron, hourly
aqi-pipeline --config my_station.yaml report
```

Units are µg/m³, except CO in mg/m³ (`scale: {co: 0.001}` converts). If the file
has no weather-forecast columns they are rebuilt from the weather that followed
each row; live forecasts are less accurate than that, so expect some loss of skill.

**Open-Meteo** (`config/open_meteo.example.yaml`): no API key. Its values are
modelled (CAMS), not measured. This connector was written against the documented
API and tested only on a payload of the documented shape, because the build
environment could not reach the API. Check the first run's data-quality report.

## Scheduling and serving

```bash
0 * * * *  aqi-pipeline --config /etc/aqi/station.yaml run-cycle     # cron, hourly
aqi-pipeline --config station.yaml schedule --every 3600             # or the built-in loop
docker compose --profile demo up --build                             # 45-day synthetic demo + UI
docker compose --profile live up -d                                  # scheduler + API
```

`run-cycle` is idempotent: running it twice for the same hour changes nothing.

## Configuration

Every setting has a default in `src/aqi_pipeline/config.py`; `config/default.yaml`
lists them all with comments. Unknown keys are rejected when the file is loaded.

## Layout

```
.github/            GitHub Actions CI/CD workflows and Dependabot config
src/aqi_pipeline/   the package (pipeline stages, API, static Operations Console, report builder)
tests/              unit, end-to-end, dashboard, and DevOps config tests
config/             default settings and two real-data examples
sample_run/         report and summary from the demo run
```

## Limits

- Evaluated on synthetic data only; one station at a time; ammonia and lead are
  not part of the index computation.
- A 5 to 10 day holdout is short. The gate still lets through an occasional
  model that does worse live, which is what probation and rollback are for.
- Safe mode switches on and off more often than is comfortable (15% of hours in
  the demo); its thresholds deserve tuning on real data.
- Thresholds were chosen on simulated data and will need recalibrating for a
  real station.
