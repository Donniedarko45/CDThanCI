"""Tests for the AQI Pipeline Operations Console UI and supporting API endpoints."""

from __future__ import annotations

import json

import pandas as pd
import pytest
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


def _reject_nan(val: str) -> None:
    raise AssertionError(f"Non-JSON float constant in response: {val}")


def _strict_json(response) -> dict:
    return json.loads(response.text, parse_constant=_reject_nan)


@pytest.fixture(scope="module")
def populated_run(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("dash_run")
    cfg = load_config(
        overrides={
            "run_dir": str(run_dir),
            "simulation": SIMULATION,
            "model": {
                "hgb_params": {
                    "learning_rate": 0.1,
                    "max_iter": 60,
                    "max_leaf_nodes": 15,
                    "min_samples_leaf": 40,
                    "l2_regularization": 1.0,
                }
            },
            "retrain": {"tune_iterations": 1},
        }
    )
    summary = run_simulation(cfg, verbose=False)
    return cfg, summary


def test_static_assets_and_empty_run_dir(tmp_path, monkeypatch):
    empty_dir = tmp_path / "empty_run"
    empty_dir.mkdir()
    monkeypatch.setenv("AQI_RUN_DIR", str(empty_dir))
    client = TestClient(app)

    # Operations console HTML & modular static assets load offline without external CDNs
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "AQI Pipeline" in root.text
    assert "/static/styles.css" in root.text
    assert "/static/charts.js" in root.text
    assert "/static/app.js" in root.text
    assert "/report" in root.text and "/docs" in root.text
    assert "http://" not in root.text.replace("http://www.w3.org", "") and "https://" not in root.text

    for asset, marker in (
        ("/static/styles.css", ".viz-root"),
        ("/static/charts.js", "AqiCharts"),
        ("/static/app.js", "loadAllData"),
    ):
        res = client.get(asset)
        assert res.status_code == 200
        assert marker in res.text

    fav = client.get("/favicon.ico")
    assert fav.status_code == 200
    assert "image/svg+xml" in fav.headers["content-type"]
    assert client.get("/docs").status_code == 200

    # /dashboard/data handles empty/unbootstrapped run_dir safely
    dash_res = client.get("/dashboard/data", params={"hours": 72})
    assert dash_res.status_code == 200
    data = _strict_json(dash_res)
    assert data["status"]["bootstrapped"] is False
    assert data["status"]["champion"] is None
    assert data["status"]["last_observation"] is None
    assert data["status"]["safe_mode"] is False
    assert data["status"]["latest_cycle"] is None
    assert data["forecast"]["forecasts"] == []
    assert data["hourly_series"] == []

    # /api/config & /api/analytics handle unbootstrapped run_dir safely
    cfg_res = client.get("/api/config")
    assert cfg_res.status_code == 200
    cfg_body = _strict_json(cfg_res)
    assert cfg_body["bootstrapped"] is False
    assert cfg_body["champion"] is None
    assert cfg_body["version_count"] == 0
    assert cfg_body["cycle_count"] == 0

    an_res = client.get("/api/analytics")
    assert an_res.status_code == 200
    an_body = _strict_json(an_res)
    assert an_body["bootstrapped"] is False
    assert an_body["champion"] is None
    assert an_body["feature_importance"] == []
    assert len(an_body["pipeline_stages"]) == 7

    # /observations/recent returns empty by default or synthetic sample on request
    recent_empty = client.get("/observations/recent", params={"hours": 216})
    assert recent_empty.status_code == 200
    assert recent_empty.json()["count"] == 0
    assert recent_empty.json()["source"] == "none"

    sample = client.get("/observations/recent", params={"hours": 48, "sample": True})
    assert sample.status_code == 200
    assert sample.json()["count"] == 48
    assert sample.json()["source"] == "synthetic_sample"

    # /report returns clean 404 HTML when no model exists yet
    rep = client.get("/report")
    assert rep.status_code == 404
    assert "text/html" in rep.headers["content-type"]
    assert "No model has been trained yet" in rep.text


def test_populated_dashboard_config_and_analytics(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    client = TestClient(app)

    for hours in (24, 48, 72, 168):
        res = client.get("/dashboard/data", params={"hours": hours})
        assert res.status_code == 200
        data = _strict_json(res)

        st = data["status"]
        assert st["bootstrapped"] is True
        assert st["champion"].startswith("v")
        assert st["last_observation"] is not None
        assert st["latest_cycle"]["decision"]["action"] in {"none", "watch", "hold", "retrain"}
        assert st["current"] is not None
        assert st["current"]["readings"]["pm25"] is not None

        assert data["simulated"] is True
        assert len(data["cycles"]) == 45
        assert len(data["versions"]) >= 1
        assert {"pipeline", "scheduled", "persistence", "static"} <= set(data["comparison"])

        fcs = data["forecast"]["forecasts"]
        assert len(fcs) == min(hours, st["horizon_hours"])
        assert fcs[0]["source"] in {"model", "persistence"}

        series = data["hourly_series"]
        assert len(series) >= hours + st["horizon_hours"]
        assert len([p for p in series if p["is_future"]]) == st["horizon_hours"]
        assert len([p for p in series if not p["is_future"]]) == hours

    # /api/config populated
    cfg_res = client.get("/api/config")
    assert cfg_res.status_code == 200
    cfg_body = _strict_json(cfg_res)
    assert cfg_body["bootstrapped"] is True
    assert cfg_body["champion"].startswith("v")
    assert cfg_body["db_exists"] is True and cfg_body["db_size_bytes"] > 0
    assert cfg_body["cycle_count"] == 45
    assert cfg_body["version_count"] >= 1
    assert "data" in cfg_body and "model" in cfg_body and "monitor" in cfg_body and "retrain" in cfg_body

    # /api/analytics populated
    an_res = client.get("/api/analytics")
    assert an_res.status_code == 200
    an_body = _strict_json(an_res)
    assert an_body["bootstrapped"] is True
    assert len(an_body["feature_importance"]) > 0
    assert len(an_body["version_metrics"]) >= 1
    assert len(an_body["drift_history"]) == 45
    assert len(an_body["pipeline_stages"]) == 7
    assert sum(an_body["decision_counts"].values()) == 45
    assert "actual" in an_body["category_counts"] and "served" in an_body["category_counts"]


def test_recent_observations_predict_and_batch_scoring(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    client = TestClient(app)

    rec = client.get("/observations/recent", params={"hours": 216})
    assert rec.status_code == 200
    body = rec.json()
    assert body["source"] == "station"
    assert body["count"] == 216

    ts_list = [pd.Timestamp(r["timestamp"]) for r in body["observations"]]
    assert ts_list == sorted(ts_list)
    assert ts_list[-1] == Pipeline(cfg).store.last_observation_time()

    # Single-point POST /predict
    obs_payload = body["observations"]
    obs_payload[-1]["pm25"] = 180.0
    pred_res = client.post("/predict", json={"observations": obs_payload})
    assert pred_res.status_code == 200
    pred = _strict_json(pred_res)
    assert 0 <= pred["aqi"] <= 500
    assert pred["category"]
    assert pred["data_quality"] in {"pass", "warn", "fail"}

    # Batch scoring via JSON observations
    batch_json = client.post("/predict/batch", json={"observations": body["observations"][-72:]})
    assert batch_json.status_code == 200
    b_data = _strict_json(batch_json)
    assert b_data["count"] > 0
    assert b_data["champion"].startswith("v")
    assert 0 <= b_data["summary"]["mean_aqi"] <= 500
    assert len(b_data["predictions"]) == b_data["count"]

    # Batch scoring via raw CSV string
    csv_text = pd.DataFrame(body["observations"][-72:]).to_csv(index=False)
    batch_csv = client.post("/predict/batch", json={"csv": csv_text})
    assert batch_csv.status_code == 200
    c_data = _strict_json(batch_csv)
    assert c_data["count"] == b_data["count"]
    assert c_data["summary"]["mean_aqi"] == b_data["summary"]["mean_aqi"]

    # Too few rows returns 422
    assert client.post("/predict/batch", json={"observations": body["observations"][-10:]}).status_code == 422


def test_report_endpoint_generates_and_serves_html(populated_run, monkeypatch):
    cfg, _ = populated_run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    report_path = cfg.report_dir / "monitoring_report.html"
    if report_path.exists():
        report_path.unlink()

    client = TestClient(app)

    first = client.get("/report")
    assert first.status_code == 200
    assert "text/html" in first.headers["content-type"]
    assert report_path.exists()
    assert "__DATA__" not in first.text
    assert '<script id="report-data" type="application/json">' in first.text

    second = client.get("/report")
    assert second.status_code == 200
    assert "AQI pipeline monitoring report" in second.text
