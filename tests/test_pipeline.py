"""End-to-end tests: a short simulated run, the report, the CLI and the HTTP API."""

import json

import pandas as pd
import pytest

from aqi_pipeline.config import load_config
from aqi_pipeline.pipeline import Pipeline
from aqi_pipeline.report import build_report
from aqi_pipeline.simulate import run_simulation

SIMULATION = {
    "seed": 5,
    "history_start": "2025-03-01",
    "production_start": "2025-07-01",
    "production_end": "2025-08-15",
    "baseline_interval_days": 20,
    "events": [
        {"type": "sensor_stuck", "column": "pm25", "start": "2025-07-08", "end": "2025-07-12", "label": "stuck"},
        # wind forecasts switch to km/h: a silent upstream change that breaks the model
        {"type": "sensor_bias", "column": "fc_wind_speed", "start": "2025-07-18", "factor": 3.6, "label": "units"},
    ],
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("run")
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


def test_simulation_runs_every_cycle_and_scores_all_methods(run):
    cfg, summary = run
    assert summary["cycles"] == 45
    assert set(summary["comparison"]) == {"pipeline", "static", "scheduled", "persistence"}
    assert summary["scored_hours"] > 24 * 25
    for block in summary["comparison"].values():
        assert block["mae"] > 0 and 0 <= block["category_accuracy"] <= 1
    assert sum(summary["decisions"].values()) == 45


def test_sensor_fault_puts_retraining_on_hold_and_is_excluded_from_scoring(run):
    cfg, _ = run
    pipe = Pipeline(cfg)
    cycles = {c["cycle_time"][:10]: c for c in pipe.store.load_cycles()}
    assert cycles["2025-07-10"]["decision"]["action"] == "hold"
    assert cycles["2025-07-10"]["quality"]["stuck_cells"] == 24
    preds = pipe.store.load_predictions("2025-07-09", "2025-07-11", role="champion", by="made_at")
    assert (preds["input_ok"] == 0).all()  # forecasts made from the stuck sensor are flagged
    assert not any(
        e["kind"] in ("promotion", "retrain_rejected") and "2025-07-08" <= e["ts"][:10] <= "2025-07-13"
        for e in pipe.store.load_events()
    )


def test_upstream_change_is_detected_and_handled(run):
    cfg, summary = run
    pipe = Pipeline(cfg)
    cycles = pipe.store.load_cycles()
    after = [c for c in cycles if c["cycle_time"][:10] >= "2025-07-19"]
    # the drift monitor names the broken input within days, before any retraining
    first_flag = next(c["cycle_time"][:10] for c in after if "fc_wind_speed" in c["drift"]["drifted"])
    assert first_flag <= "2025-07-22"
    # the pipeline reacted: it retrained or fell back to persistence
    kinds = {e["kind"] for e in pipe.store.load_events() if e["ts"][:10] >= "2025-07-19"}
    assert kinds & {"promotion", "retrain_rejected", "safe_mode_on"}
    assert summary["comparison"]["pipeline"]["mae"] <= summary["comparison"]["static"]["mae"]


def test_state_is_consistent_after_the_run(run):
    cfg, summary = run
    pipe = Pipeline(cfg)  # a fresh process sees the same state
    stages = [v["stage"] for v in pipe.registry.versions()]
    assert stages.count("champion") == 1
    assert pipe.registry.champion in {v["version"] for v in summary["versions"]}
    served = pipe.store.load_predictions(role="champion", by="made_at")
    assert served["made_at"].is_unique  # exactly one served forecast per hour
    matured = served[served["target_time"] <= pipe.store.last_observation_time()]
    assert matured["label_ok"].notna().all()  # every matured forecast has been scored
    assert pd.Timestamp(served["made_at"].max()) == pipe.store.last_observation_time()


def test_running_a_cycle_twice_changes_nothing(run):
    cfg, _ = run
    from aqi_pipeline.sources import FrameSource
    from aqi_pipeline.synthetic import generate

    pipe = Pipeline(
        cfg,
        FrameSource(
            generate(
                SIMULATION["history_start"],
                SIMULATION["production_end"],
                seed=SIMULATION["seed"],
                events=SIMULATION["events"],
            )
        ),
    )
    before = len(pipe.store.load_predictions())
    record = pipe.run_cycle(pipe.store.last_observation_time())
    assert record["quality"]["status"] == "no_data" and record["forecasts"] == 0
    assert len(pipe.store.load_predictions()) == before


def test_report_is_self_contained(run, tmp_path):
    cfg, summary = run
    path = build_report(Pipeline(cfg), summary, tmp_path / "report.html")
    html = path.read_text(encoding="utf-8")
    assert "__DATA__" not in html and "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html
    payload = json.loads(html.split('<script id="report-data" type="application/json">')[1].split("</script>")[0])
    assert payload["simulated"] is True and len(payload["cycles"]) == 45
    assert payload["version_spans"][0]["version"] == "v001"
    assert {"pipeline", "scheduled", "persistence", "forecast"} <= set(payload["daily"][5])


def test_cli_status_and_report(run, tmp_path, capsys):
    cfg, _ = run
    from aqi_pipeline.cli import main

    assert main(["--run-dir", cfg.run_dir, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["champion"].startswith("v") and status["decision"] in {"none", "watch", "hold", "retrain"}
    assert main(["--run-dir", cfg.run_dir, "report", "--out", str(tmp_path / "r.html")]) == 0
    assert (tmp_path / "r.html").exists()


def test_api(run, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aqi_pipeline.api import app

    cfg, _ = run
    monkeypatch.setenv("AQI_RUN_DIR", cfg.run_dir)
    client = TestClient(app)

    health = client.get("/health").json()
    assert health["status"] == "ok" and health["champion"]

    forecast = client.get("/forecast", params={"hours": 6}).json()
    assert len(forecast["forecasts"]) == 6
    first = forecast["forecasts"][0]
    assert 0 <= first["aqi"] <= 500 and first["category"] and first["source"] in {"model", "persistence"}
    assert pd.Timestamp(first["target_time"]) > pd.Timestamp(forecast["issued_up_to"])

    assert client.get("/model").json()["champion"]["version"] == health["champion"]
    assert client.get("/monitoring").json()["decision"]["action"]
    assert len(client.get("/events", params={"limit": 5}).json()) == 5

    obs = Pipeline(cfg).store.load_observations(clean=False).tail(24 * 9).reset_index()
    rows = json.loads(
        obs.drop(columns=["qc_mask", "qc_notes"])
        .assign(timestamp=obs["timestamp"].astype(str))
        .to_json(orient="records")
    )
    scored = client.post("/predict", json={"observations": rows}).json()
    assert 0 <= scored["aqi"] <= 500 and scored["champion"] == health["champion"]
    assert client.post("/predict", json={"observations": rows[:5]}).status_code == 422

    # /api/config returns sanitized config and runtime telemetry
    cfg_res = client.get("/api/config")
    assert cfg_res.status_code == 200
    cfg_body = cfg_res.json()
    assert cfg_body["bootstrapped"] is True and cfg_body["champion"] == health["champion"]
    assert cfg_body["db_exists"] is True and cfg_body["db_size_bytes"] > 0
    assert cfg_body["cycle_count"] == 45 and cfg_body["version_count"] >= 1
    assert cfg_body["data"]["horizon_hours"] == 24 and cfg_body["python_version"]

    # /api/analytics returns category counts, monthly MAE, feature importance, drift history, and 7 stages
    ana_res = client.get("/api/analytics")
    assert ana_res.status_code == 200
    ana = json.loads(ana_res.text, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(c)))
    assert ana["bootstrapped"] is True and ana["champion"] == health["champion"]
    assert sum(ana["category_counts"]["actual"].values()) > 0
    assert sum(ana["category_counts"]["served"].values()) > 0
    assert len(ana["monthly_mae"]) >= 1 and len(ana["feature_importance"]) > 0
    assert len(ana["version_metrics"]) == cfg_body["version_count"]
    assert sum(ana["decision_counts"].values()) == 45
    assert len(ana["drift_history"]) == 45
    assert [s["id"] for s in ana["pipeline_stages"]] == [
        "ingest",
        "quality",
        "forecast",
        "monitor",
        "decide",
        "retrain",
        "registry",
    ]

    # /predict/batch supports JSON observations (24..240 cap) and CSV with columns/scale
    obs_long = Pipeline(cfg).store.load_observations(clean=False).tail(270).reset_index()
    rows_long = json.loads(
        obs_long.drop(columns=["qc_mask", "qc_notes"])
        .assign(timestamp=obs_long["timestamp"].astype(str))
        .to_json(orient="records")
    )
    batch_res = client.post("/predict/batch", json={"observations": rows_long})
    assert batch_res.status_code == 200
    batch = batch_res.json()
    assert batch["champion"] == health["champion"]
    assert batch["count"] == 240
    assert batch["summary"]["min_aqi"] <= batch["summary"]["mean_aqi"] <= batch["summary"]["max_aqi"]
    assert sum(batch["summary"]["category_counts"].values()) == batch["count"]

    csv_df = obs.tail(72).drop(columns=["qc_mask", "qc_notes"]).copy()
    csv_df["co"] = csv_df["co"] * 1000.0
    csv_df = csv_df.rename(columns={"timestamp": "dt", "pm25": "PM25", "co": "CO_ug"})
    csv_res = client.post(
        "/predict/batch",
        json={
            "csv": csv_df.to_csv(index=False),
            "columns": {"timestamp": "dt", "pm25": "PM25", "co": "CO_ug"},
            "scale": {"co": 0.001},
        },
    )
    assert csv_res.status_code == 200
    assert csv_res.json()["count"] == 72 - 23
    assert client.post("/predict/batch", json={"observations": rows[:10]}).status_code == 422
    assert client.post("/predict/batch", json={}).status_code == 422


def test_api_config_masks_secrets_and_handles_empty_run_dir(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aqi_pipeline.api import app

    cfg_path = tmp_path / "secret.yaml"
    run_dir = tmp_path / "empty_run"
    cfg_path.write_text(
        f"run_dir: {run_dir}\nsource:\n  type: csv\n  path: station.csv\n  api_key: super-secret\n  token: abc\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AQI_CONFIG", str(cfg_path))
    monkeypatch.delenv("AQI_RUN_DIR", raising=False)
    client = TestClient(app)

    cfg_body = client.get("/api/config").json()
    assert cfg_body["bootstrapped"] is False and cfg_body["champion"] is None
    assert cfg_body["source"]["api_key"] == "***" and cfg_body["source"]["token"] == "***"
    assert cfg_body["source"]["path"] == "station.csv"

    ana_body = client.get("/api/analytics").json()
    assert ana_body["bootstrapped"] is False and ana_body["drift_history"] == []
    assert all(s["status"] == "empty" for s in ana_body["pipeline_stages"])


def test_csv_source_bootstrap_and_cycle_through_the_cli(tmp_path, capsys):
    """The real-data path: a station CSV with its own column names and CO in µg/m³."""
    from aqi_pipeline.cli import main
    from aqi_pipeline.synthetic import generate

    data = generate("2025-03-01", "2025-07-10", seed=9).drop(
        columns=["fc_temperature", "fc_humidity", "fc_wind_speed", "fc_precipitation"]
    )
    data["co"] = data["co"] * 1000.0
    data = data.rename(columns={"timestamp": "Datetime", "pm25": "PM2.5", "pm10": "PM10", "co": "CO"})
    csv = tmp_path / "station.csv"
    data.to_csv(csv, index=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"location: Test station\nrun_dir: {tmp_path / 'run'}\n"
        f"source:\n  type: csv\n  path: {csv}\n"
        "  columns: {timestamp: Datetime, pm25: PM2.5, pm10: PM10, co: CO}\n  scale: {co: 0.001}\n"
        "model:\n  candidates: [ridge]\n"
    )
    assert main(["--config", str(config), "bootstrap", "--until", "2025-07-01"]) == 0
    assert "v001" in capsys.readouterr().out
    for now in ("2025-07-02 00:00", "2025-07-03 00:00", "2025-07-04 00:00"):
        assert main(["--config", str(config), "run-cycle", "--now", now]) == 0
    pipe = Pipeline(load_config(config))
    obs = pipe.store.load_observations()
    assert obs["co"].median() < 5 and obs["fc_wind_speed"].notna().mean() > 0.9  # units converted, forecasts built
    served = pipe.store.load_predictions(role="champion", by="made_at")
    assert len(served) == 72 and served["y_true"].notna().sum() >= 47  # 3 days served, 2 matured
    assert len(pipe.store.load_cycles()) == 3


def test_clear_run_dir_keeps_mount_point_with_readonly_parent(tmp_path):
    """Fresh simulations must not delete the run dir itself (e.g. a Docker volume)."""
    import os
    import stat

    from aqi_pipeline.simulate import clear_run_dir

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")

    parent = tmp_path / "root"
    run_dir = parent / "data"
    (run_dir / "models" / "v001").mkdir(parents=True)
    (run_dir / "models" / "v001" / "model.joblib").write_text("x")
    (run_dir / "summary.json").write_text("{}")
    (run_dir / "link").symlink_to(run_dir / "summary.json")

    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)  # parent read-only, like "/" above "/data"
    try:
        clear_run_dir(run_dir)
    finally:
        parent.chmod(stat.S_IRWXU)

    assert run_dir.is_dir()
    assert list(run_dir.iterdir()) == []
