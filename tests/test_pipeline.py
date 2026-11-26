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
    cfg = load_config(overrides={
        "run_dir": str(run_dir), "simulation": SIMULATION,
        "model": {"hgb_params": {"learning_rate": 0.1, "max_iter": 60, "max_leaf_nodes": 15,
                                 "min_samples_leaf": 40, "l2_regularization": 1.0}},
        "retrain": {"tune_iterations": 1},
    })
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
    assert (preds["input_ok"] == 0).all()                    # forecasts made from the stuck sensor are flagged
    assert not any(e["kind"] in ("promotion", "retrain_rejected") and "2025-07-08" <= e["ts"][:10] <= "2025-07-13"
                   for e in pipe.store.load_events())


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
    pipe = Pipeline(cfg)                                     # a fresh process sees the same state
    stages = [v["stage"] for v in pipe.registry.versions()]
    assert stages.count("champion") == 1
    assert pipe.registry.champion in {v["version"] for v in summary["versions"]}
    served = pipe.store.load_predictions(role="champion", by="made_at")
    assert served["made_at"].is_unique                       # exactly one served forecast per hour
    matured = served[served["target_time"] <= pipe.store.last_observation_time()]
    assert matured["label_ok"].notna().all()                 # every matured forecast has been scored
    assert pd.Timestamp(served["made_at"].max()) == pipe.store.last_observation_time()


def test_running_a_cycle_twice_changes_nothing(run):
    cfg, _ = run
    from aqi_pipeline.sources import FrameSource
    from aqi_pipeline.synthetic import generate
    pipe = Pipeline(cfg, FrameSource(generate(SIMULATION["history_start"], SIMULATION["production_end"],
                                              seed=SIMULATION["seed"], events=SIMULATION["events"])))
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
    fastapi = pytest.importorskip("fastapi")
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
    rows = json.loads(obs.drop(columns=["qc_mask", "qc_notes"]).assign(
        timestamp=obs["timestamp"].astype(str)).to_json(orient="records"))
    scored = client.post("/predict", json={"observations": rows}).json()
    assert 0 <= scored["aqi"] <= 500 and scored["champion"] == health["champion"]
    assert client.post("/predict", json={"observations": rows[:5]}).status_code == 422


def test_csv_source_bootstrap_and_cycle_through_the_cli(tmp_path, capsys):
    """The real-data path: a station CSV with its own column names and CO in µg/m³."""
    from aqi_pipeline.cli import main
    from aqi_pipeline.synthetic import generate
    data = generate("2025-03-01", "2025-07-10", seed=9).drop(
        columns=["fc_temperature", "fc_humidity", "fc_wind_speed", "fc_precipitation"])
    data["co"] = data["co"] * 1000.0
    data = data.rename(columns={"timestamp": "Datetime", "pm25": "PM2.5", "pm10": "PM10", "co": "CO"})
    csv = tmp_path / "station.csv"
    data.to_csv(csv, index=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"location: Test station\nrun_dir: {tmp_path / 'run'}\n"
        f"source:\n  type: csv\n  path: {csv}\n"
        "  columns: {timestamp: Datetime, pm25: PM2.5, pm10: PM10, co: CO}\n  scale: {co: 0.001}\n"
        "model:\n  candidates: [ridge]\n")
    assert main(["--config", str(config), "bootstrap", "--until", "2025-07-01"]) == 0
    assert "v001" in capsys.readouterr().out
    for now in ("2025-07-02 00:00", "2025-07-03 00:00", "2025-07-04 00:00"):
        assert main(["--config", str(config), "run-cycle", "--now", now]) == 0
    pipe = Pipeline(load_config(config))
    obs = pipe.store.load_observations()
    assert obs["co"].median() < 5 and obs["fc_wind_speed"].notna().mean() > 0.9   # units converted, forecasts built
    served = pipe.store.load_predictions(role="champion", by="made_at")
    assert len(served) == 72 and served["y_true"].notna().sum() >= 47            # 3 days served, 2 matured
    assert len(pipe.store.load_cycles()) == 3
