"""Tests for the Live Web Dashboard UI and supporting FastAPI endpoints."""
from __future__ import annotations

import json

import pandas as pd
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from aqi_pipeline.api import app
from aqi_pipeline.config import load_config
from aqi_pipeline.pipeline import Pipeline
from aqi_pipeline.simulate import run_simulation

SIMULATION = {
    "seed": 5,
    "history_start": "2025-03-01",
    "production_start": "2025-07-01",
    "production_end": "2025-08-15",
    "baseline_interval_days": 20,
    "events": [
        {"type": "sensor_stuck", "column": "pm25", "start": "2025-07-08", "end": "2025-07-12", "label": "stuck"},
        {"type": "sensor_bias", "column": "fc_wind_speed", "start": "2025-07-18", "factor": 3.6, "label": "units"},
    ],
}


@pytest.fixture(scope="module")
def populated_run(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("dash_run")
    cfg = load_config(overrides={
        "run_dir": str(run_dir),
        "simulation": SIMULATION,
        "model": {"hgb_params": {
            "learning_rate": 0.1, "max_iter": 60, "max_leaf_nodes": 15,
            "min_samples_leaf": 40, "l2_regularization": 1.0,
        }},
        "retrain": {"tune_iterations": 1},
    })
    summary = run_simulation(cfg, verbose=False)
    return cfg, summary


def test_empty_run_dir_dashboard_and_endpoints(tmp_path, monkeypatch):
    empty_dir = tmp_path / "empty_run"
    empty_dir.mkdir()
    monkeypatch.setenv("AQI_RUN_DIR", str(empty_dir))
    client = TestClient(app)

    # Dashboard HTML loads cleanly without a model or database
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "AQI Pipeline Dashboard" in root.text
    assert "/report" in root.text and "/docs" in root.text
    assert "http://" not in root.text.replace("http://www.w3.org/2000/svg", "") and "https://" not in root.text
    assert "`none`" not in root.text

    # Favicon route prevents browser 404 console errors
    fav = client.get("/favicon.ico")
    assert fav.status_code == 200
    assert "image/svg+xml" in fav.headers["content-type"]

    # FastAPI Swagger UI remains intact at /docs
    assert client.get("/docs").status_code == 200

    # /dashboard/data handles empty/unbootstrapped run_dir safely
    dash_res = client.get("/dashboard/data", params={"hours": 72})
    assert dash_res.status_code == 200
    data = dash_res.json()
    assert data["status"]["bootstrapped"] is False
    assert data["status"]["champion"] is None
    assert data["status"]["last_observation"] is None
    assert data["status"]["safe_mode"] is False
    assert data["status"]["latest_cycle"] is None
    assert data["forecast"]["forecasts"] == []
    assert data["hourly_series"] == []
    assert data["cycles"] == []
    assert data["versions"] == []

    # /observations/recent returns empty list by default, or synthetic sample on request
    recent_empty = client.get("/observations/recent", params={"hours": 216})
    assert recent_empty.status_code == 200
    assert recent_empty.json()["count"] == 0
    assert recent_empty.json()["source"] == "none"
    assert recent_empty.json()["observations"] == []

    sample = client.get("/observations/recent", params={"hours": 48, "sample": True})
    assert sample.status_code == 200
    sample_body = sample.json()
    assert sample_body["count"] == 48
    assert sample_body["source"] == "synthetic_sample"
    assert len(sample_body["observations"]) == 48

    # /report returns clean 404 HTML when no model exists yet
    rep = client.get("/report")
    assert rep.status_code == 404
    assert "text/html" in rep.headers["content-type"]
    assert "No model has been trained yet" in rep.text


def test_populated_dashboard_data_and_horizons(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    client = TestClient(app)

    for hours in (24, 48, 72, 168):
        res = client.get("/dashboard/data", params={"hours": hours})
        assert res.status_code == 200
        # Verify strict JSON (no NaN/Infinity tokens that break browser JSON.parse)
        def _reject_nan(val):
            raise AssertionError(f"Non-JSON float constant in response: {val}")
        data = json.loads(res.text, parse_constant=_reject_nan)

        st = data["status"]
        assert st["bootstrapped"] is True
        assert st["champion"].startswith("v")
        assert st["last_observation"] is not None
        assert st["latest_cycle"]["decision"]["action"] in {"none", "watch", "hold", "retrain"}
        assert "drift" in st["latest_cycle"] and "performance" in st["latest_cycle"]
        assert "cusum" in st["latest_cycle"]
        assert st["current"] is not None
        assert st["current"]["readings"]["pm25"] is not None

        # collect(pipe, summary) fields present
        assert data["simulated"] is True
        assert len(data["cycles"]) == 45
        assert len(data["versions"]) >= 1
        assert {"pipeline", "scheduled", "persistence", "static"} <= set(data["comparison"])
        assert "thresholds" in data

        # Upcoming forecast horizon + hourly actual-vs-forecast series
        fcs = data["forecast"]["forecasts"]
        assert len(fcs) == min(hours, st["horizon_hours"])
        assert fcs[0]["source"] in {"model", "persistence"}
        assert fcs[0]["category"] != "Unknown"

        series = data["hourly_series"]
        assert len(series) >= hours + st["horizon_hours"]
        future_pts = [p for p in series if p["is_future"]]
        past_pts = [p for p in series if not p["is_future"]]
        assert len(future_pts) == st["horizon_hours"]
        assert len(past_pts) == hours
        assert any(p["actual_aqi"] is not None and p["forecast_aqi"] is not None for p in past_pts)


def test_recent_observations_and_playground_predict(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    client = TestClient(app)

    rec = client.get("/observations/recent", params={"hours": 216})
    assert rec.status_code == 200
    body = rec.json()
    assert body["source"] == "station"
    assert body["count"] == 216
    assert len(body["observations"]) == 216

    # Timestamps are strictly ascending (oldest first)
    ts_list = [pd.Timestamp(r["timestamp"]) for r in body["observations"]]
    assert ts_list == sorted(ts_list)
    assert ts_list[-1] == Pipeline(cfg).store.last_observation_time()

    # Modifying latest row and submitting to POST /predict works end-to-end
    obs_payload = body["observations"]
    obs_payload[-1]["pm25"] = 180.0
    pred_res = client.post("/predict", json={"observations": obs_payload})
    assert pred_res.status_code == 200
    pred = pred_res.json()
    assert 0 <= pred["aqi"] <= 500
    assert pred["category"]
    assert pred["data_quality"] in {"pass", "warn", "fail"}
    assert "issues" in pred


def test_report_endpoint_generates_and_serves_html(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    report_path = cfg.report_dir / "monitoring_report.html"
    if report_path.exists():
        report_path.unlink()

    client = TestClient(app)

    # First call builds monitoring_report.html via report.build_report
    first = client.get("/report")
    assert first.status_code == 200
    assert "text/html" in first.headers["content-type"]
    assert report_path.exists()
    assert "__DATA__" not in first.text
    assert '<script id="report-data" type="application/json">' in first.text

    # Second call serves the generated file cleanly
    second = client.get("/report")
    assert second.status_code == 200
    assert "AQI pipeline monitoring report" in second.text
