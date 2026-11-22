"""Unit tests for the building blocks: AQI, data quality, features, drift, policy, registry."""
import numpy as np
import pandas as pd
import pytest

from aqi_pipeline import aqi
from aqi_pipeline.config import DataConfig, MonitorConfig, RetrainConfig, load_config
from aqi_pipeline.drift import calibrate_thresholds, drift_report, psi, reference_slice
from aqi_pipeline.features import build_dataset
from aqi_pipeline.modeling import ForecastModel, score
from aqi_pipeline.performance import cusum_update, performance_report, safe_mode_update
from aqi_pipeline.policy import Signals, decide
from aqi_pipeline.registry import ModelRegistry
from aqi_pipeline.retrain import paired_block_bootstrap
from aqi_pipeline.schema import COLUMN_BIT, OBS_COLUMNS
from aqi_pipeline.sources import parse_open_meteo
from aqi_pipeline.store import Store
from aqi_pipeline.synthetic import generate
from aqi_pipeline.validation import apply_mask, validate_batch


@pytest.fixture(scope="module")
def data():
    return generate("2025-01-01", "2025-05-01", seed=3)


# ---------------------------------------------------------------- AQI
def test_sub_index_matches_cpcb_worked_example():
    # CPCB example: PM2.5 of 150 µg/m³ -> 300 + (150 - 120) * 100 / 130 = 323
    assert aqi.sub_index("pm25", 150)[()] == pytest.approx(323.08, abs=0.01)


@pytest.mark.parametrize("pollutant,conc,expected", [
    ("pm25", 30, 50), ("pm25", 60, 100), ("pm10", 250, 200), ("no2", 80, 100),
    ("co", 2.0, 100), ("o3", 168, 200), ("so2", 380, 200), ("pm10", 430, 400),
])
def test_sub_index_at_breakpoints(pollutant, conc, expected):
    assert aqi.sub_index(pollutant, conc)[()] == pytest.approx(expected)


def test_categories():
    assert [aqi.category_name(v) for v in (20, 51, 150, 250, 350, 450)] == aqi.CATEGORIES
    assert aqi.category_name(float("nan")) == "Unknown"


def test_aqi_is_highest_sub_index_and_needs_three_pollutants_with_pm():
    idx = pd.date_range("2025-01-01", periods=48, freq="h")
    obs = pd.DataFrame({"pm25": 150.0, "pm10": 100.0, "no2": 40.0, "so2": 10.0, "co": 1.0, "o3": 30.0}, index=idx)
    out = aqi.compute_aqi(obs)
    assert out["aqi"].iloc[-1] == pytest.approx(323.1, abs=0.1)
    assert out["dominant"].iloc[-1] == "pm25"
    assert out["aqi"].iloc[:15].isna().all()          # fewer than 16 hours of data
    no_pm = obs.assign(pm25=np.nan, pm10=np.nan)
    assert aqi.compute_aqi(no_pm)["aqi"].isna().all()
    two_only = obs[["pm25", "no2"]]
    assert aqi.compute_aqi(two_only)["aqi"].isna().all()


# ---------------------------------------------------------------- data quality
def test_quality_gate_flags_stuck_range_gaps_and_duplicates(data):
    batch = data.iloc[200:272].copy()
    batch.iloc[10:20, batch.columns.get_loc("pm25")] = 77.7            # stuck for 10 hours
    batch.iloc[30, batch.columns.get_loc("no2")] = -999.0             # logger error code
    batch = pd.concat([batch.drop(batch.index[40:43]), batch.iloc[[5]]])   # 3-hour gap and a duplicate
    clean, report = validate_batch(batch, DataConfig())
    assert len(clean) == 72 and report.gap_rows == 3 and report.duplicates == 1
    assert report.stuck_cells == 10 and report.range_cells == 1
    assert report.status == "warn"
    masked = apply_mask(clean)
    assert masked["pm25"].iloc[10:20].isna().all() and np.isnan(masked["no2"].iloc[30])
    assert clean["pm25"].iloc[12] == 77.7                              # raw value kept for audit
    assert (clean["qc_mask"].iloc[10:20] & (1 << COLUMN_BIT["pm25"])).all()


def test_quality_gate_fails_batch_when_core_sensor_is_mostly_bad(data):
    batch = data.iloc[300:324].copy()
    batch["pm10"] = np.nan
    _, report = validate_batch(batch, DataConfig())
    assert report.status == "fail" and report.core_bad_fraction == 1.0


def test_stuck_sensor_is_caught_across_batches(data):
    frozen = data.iloc[400:430].copy()
    frozen["pm25"] = 42.0
    first, _ = validate_batch(frozen.iloc[:4], DataConfig())             # too short to tell
    assert first["qc_mask"].sum() == 0
    context = first.drop(columns=["qc_mask", "qc_notes"])
    second, report = validate_batch(frozen.iloc[4:10], DataConfig(), context=context)
    assert report.stuck_cells == 6


def test_missing_required_column_is_rejected(data):
    with pytest.raises(ValueError):
        validate_batch(data.drop(columns=["pm10"]).head(24), DataConfig())


# ---------------------------------------------------------------- features
def test_features_do_not_use_the_future(data):
    obs = data.set_index("timestamp")[OBS_COLUMNS]
    cut = obs.index[1500]
    changed = obs.copy()
    changed.loc[changed.index > cut, ["pm25", "pm10", "no2", "co", "o3", "wind_speed"]] *= 3.0
    a, b = build_dataset(obs), build_dataset(changed)
    pd.testing.assert_frame_equal(a.X.loc[:cut], b.X.loc[:cut])
    # ... and the label at t is the AQI 24 hours later
    assert a.y.loc[cut - pd.Timedelta(hours=24)] == a.aqi_now.loc[cut]


def test_rows_with_unreliable_labels_are_excluded(data):
    obs = data.set_index("timestamp")[OBS_COLUMNS].copy()
    obs.iloc[1000:1030, obs.columns.get_loc("pm25")] = np.nan
    ds = build_dataset(obs)
    bad_label_time = obs.index[1020]
    assert not ds.aqi_ok.loc[bad_label_time]
    assert not ds.usable().loc[bad_label_time - pd.Timedelta(hours=24)]


def test_model_round_trip_beats_nothing_on_trivial_target(data):
    ds = build_dataset(data.set_index("timestamp")[OBS_COLUMNS])
    rows = ds.X.index[ds.usable().to_numpy()]
    model = ForecastModel("ridge", {"alpha": 1.0}).fit(ds.X.loc[rows], ds.y.loc[rows], ds.aqi_now.loc[rows])
    pred = model.predict(ds.X.loc[rows], ds.aqi_now.loc[rows])
    assert pred.min() >= 0 and pred.max() <= 500
    assert score(ds.y.loc[rows], pred, ds.aqi_now.loc[rows])["rel_mae"] < 1.0   # in-sample, beats persistence


# ---------------------------------------------------------------- drift
def test_psi_is_small_for_same_distribution_and_large_for_a_shift():
    rng = np.random.default_rng(0)
    ref, same, shifted = rng.normal(0, 1, 5000), rng.normal(0, 1, 400), rng.normal(1.5, 1, 400)
    assert psi(ref, same) < 0.1
    assert psi(ref, shifted) > 1.0
    assert np.isnan(psi(ref[:10], same))


def test_seasonal_reference_and_fallback():
    idx = pd.date_range("2024-01-01", "2025-12-31 23:00", freq="h")
    ref = pd.DataFrame({"x": np.arange(len(idx), dtype=float)}, index=idx)
    cfg = MonitorConfig()
    seasonal, mode = reference_slice(ref, pd.Timestamp("2026-07-10"), cfg)
    assert mode == "seasonal" and set(seasonal.index.month) <= {6, 7, 8}
    short = ref.loc["2025-03-01":"2025-04-15"]
    _, mode = reference_slice(short, pd.Timestamp("2026-10-10"), cfg)
    assert mode == "all"


def test_drift_report_flags_shift_and_novelty(data):
    obs = data.set_index("timestamp")[["pm25", "wind_speed", "temperature"]]
    ref, window = obs.iloc[:2000], obs.iloc[2000:2336].copy()
    cfg = MonitorConfig()
    thresholds = calibrate_thresholds(ref, list(ref.columns), cfg)
    assert all(v >= cfg.psi_floor for v in thresholds.values())
    importance = {"pm25": 0.6, "wind_speed": 0.3, "temperature": 0.1}
    window["pm25"] = window["pm25"] * 8 + 400                   # far outside anything in the reference
    report = drift_report(ref, window, thresholds, importance, cfg)
    assert "pm25" in report["drifted"] and report["columns"]["pm25"]["novel"] == 1.0
    assert report["score"] >= 0.6 and report["level"] == "alert"


# ---------------------------------------------------------------- performance signals
def _preds(n, err_model, err_naive, end="2026-01-10"):
    t = pd.date_range(end=end, periods=n, freq="h")
    return pd.DataFrame({"target_time": t, "y_true": 100.0, "y_model": 100.0 + err_model,
                         "y_pred": 100.0 + err_model, "y_naive": 100.0 + err_naive,
                         "label_ok": 1, "input_ok": 1})


def test_performance_levels():
    cfg = MonitorConfig()
    ok = performance_report(_preds(168, 6, 10), 0.6, cfg, 168)
    assert ok["level"] == "ok" and ok["ratio"] == pytest.approx(1.0)
    bad = performance_report(_preds(168, 12, 10), 0.6, cfg, 168)
    assert bad["level"] == "critical" and bad["ratio"] == pytest.approx(2.0)
    sparse = performance_report(_preds(40, 12, 10), 0.6, cfg, 168)
    assert sparse["level"] == "unknown"                      # too little clean data to judge


def test_faulty_rows_do_not_count_against_the_model():
    cfg = MonitorConfig()
    preds = _preds(168, 6, 10)
    preds.loc[:60, "y_model"] = 400.0
    preds.loc[:60, "input_ok"] = 0                           # these forecasts came from flagged inputs
    assert performance_report(preds, 0.6, cfg, 168)["level"] == "ok"


def test_cusum_accumulates_only_sustained_excess():
    cfg = MonitorConfig()
    state = None
    for _ in range(30):
        state = cusum_update(state, 1.1, cfg)                # within the slack: never alarms
    assert state["g"] == 0 and not state["alarm"]
    for _ in range(8):
        state = cusum_update(state, 1.9, cfg)
    assert state["alarm"]


def test_safe_mode_enters_and_leaves():
    cfg = MonitorConfig()
    now = pd.Timestamp("2026-01-10")
    active, info = safe_mode_update(False, _preds(72, 20, 10), now, cfg)
    assert active and info["ratio"] == pytest.approx(2.0)
    active, _ = safe_mode_update(True, _preds(72, 11, 10), now, cfg)
    assert active                                            # still worse than persistence: stay
    active, _ = safe_mode_update(True, _preds(72, 8, 10), now, cfg)
    assert not active


# ---------------------------------------------------------------- policy
def _signals(**kw):
    base = dict(quality_bad_fraction=0.0, drift_level="ok", drift_streak=0, perf_level="ok", perf_streak=0,
                cusum_alarm=False, days_since_attempt=30.0, cooldown_days=5.0, base_cooldown_days=5.0,
                model_age_days=30.0, age_limit_days=60.0, in_probation=False, labelled_days_since_training=29.0)
    return Signals(**{**base, **kw})


@pytest.mark.parametrize("kwargs,action,trigger", [
    ({}, "none", ""),
    ({"quality_bad_fraction": 0.4, "perf_level": "critical", "perf_streak": 5}, "hold", ""),
    ({"perf_level": "critical", "perf_streak": 2}, "retrain", "performance"),
    ({"perf_level": "critical", "perf_streak": 1}, "watch", ""),
    ({"perf_level": "warn", "perf_streak": 3, "drift_level": "warn"}, "retrain", "performance+drift"),
    ({"perf_level": "warn", "perf_streak": 3}, "watch", ""),
    ({"drift_level": "alert", "drift_streak": 2}, "watch", ""),
    ({"drift_level": "alert", "drift_streak": 5}, "retrain", "drift"),
    ({"cusum_alarm": True}, "retrain", "slow_decay"),
    ({"model_age_days": 61.0, "days_since_attempt": 61.0}, "retrain", "scheduled"),
    ({"perf_level": "critical", "perf_streak": 4, "in_probation": True}, "watch", ""),
    ({"cusum_alarm": True, "days_since_attempt": 2.0}, "watch", ""),
    # back-off does not delay a retrain when accuracy has collapsed
    ({"perf_level": "critical", "perf_streak": 3, "days_since_attempt": 6.0, "cooldown_days": 20.0}, "retrain", "performance"),
    ({"cusum_alarm": True, "days_since_attempt": 6.0, "cooldown_days": 20.0}, "watch", ""),
])
def test_policy_decision_table(kwargs, action, trigger):
    decision = decide(_signals(**kwargs), RetrainConfig())
    assert (decision.action, decision.trigger) == (action, trigger)
    assert decision.reason


# ---------------------------------------------------------------- gate, registry, store, config
def test_paired_bootstrap_probability():
    idx = pd.date_range("2026-01-01", periods=240, freq="h")
    rng = np.random.default_rng(1)
    champion = pd.Series(rng.uniform(5, 15, 240), index=idx)
    assert paired_block_bootstrap(champion, champion - 3.0, 300, 0) == 1.0
    assert paired_block_bootstrap(champion, champion + 3.0, 300, 0) == 0.0
    noisy = champion + rng.normal(0, 6, 240)
    assert 0.05 < paired_block_bootstrap(champion, noisy, 300, 0) < 0.95


def test_registry_promote_shadow_and_rollback(tmp_path):
    reg = ModelRegistry(tmp_path / "registry")
    ref = pd.DataFrame({"x": [1.0]}, index=pd.DatetimeIndex(["2026-01-01"]))
    v1 = reg.register(ForecastModel("ridge", {}), {"kind": "ridge", "trigger": "initial"}, ref, "2026-01-01")
    reg.promote(v1, "2026-01-01", "initial")
    v2 = reg.register(ForecastModel("hgb", {}), {"kind": "hgb", "trigger": "performance"}, ref, "2026-02-01")
    reg.promote(v2, "2026-02-01", "better on holdout")
    assert (reg.champion, reg.shadow) == (v2, v1)
    assert reg.rollback("2026-02-08", "worse live") == v1
    assert (reg.champion, reg.shadow) == (v1, None)
    stages = {v["version"]: v["stage"] for v in reg.versions()}
    assert stages == {v1: "champion", v2: "rolled_back"}
    assert [h["action"] for h in reg.history()] == ["register", "promote", "register", "promote", "rollback"]
    assert ModelRegistry(tmp_path / "registry").champion == v1          # state survives a restart
    with pytest.raises(RuntimeError):
        reg.rollback("2026-02-09", "no shadow left")


def test_store_keeps_raw_values_and_blanks_flagged_cells_on_clean_load(tmp_path, data):
    store = Store(tmp_path / "p.db")
    batch = data.iloc[100:148].copy()
    batch.iloc[5, batch.columns.get_loc("pm25")] = 5000.0
    validated, _ = validate_batch(batch, DataConfig())
    assert store.upsert_observations(validated) == 48
    raw = store.load_observations(clean=False)
    clean = store.load_observations(clean=True)
    assert raw["pm25"].iloc[5] == 5000.0 and np.isnan(clean["pm25"].iloc[5])
    assert store.last_observation_time() == validated.index[-1]
    store.set_state("k", {"a": 1})
    assert store.get_state("k") == {"a": 1} and store.get_state("missing", 7) == 7


def test_config_overrides_and_unknown_keys(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("retrain:\n  eval_days: 12\nmonitor:\n  drift_window_days: 10\n")
    cfg = load_config(path, {"run_dir": str(tmp_path)})
    assert cfg.retrain.eval_days == 12 and cfg.monitor.drift_window_days == 10 and cfg.run_dir == str(tmp_path)
    path.write_text("retrain:\n  no_such_option: 1\n")
    with pytest.raises(KeyError):
        load_config(path)


def test_open_meteo_parser_converts_units_and_builds_forecast_columns():
    hours = pd.date_range("2026-01-01", periods=96, freq="h").strftime("%Y-%m-%dT%H:%M").tolist()
    air = {"hourly_units": {"carbon_monoxide": "μg/m³"},
           "hourly": {"time": hours[:72], "pm2_5": [50.0] * 72, "pm10": [90.0] * 72, "nitrogen_dioxide": [30.0] * 72,
                      "sulphur_dioxide": [8.0] * 72, "carbon_monoxide": [800.0] * 72, "ozone": [40.0] * 72}}
    weather = {"hourly": {"time": hours, "temperature_2m": [20.0] * 96, "relative_humidity_2m": [50.0] * 96,
                          "wind_speed_10m": [2.0] * 48 + [6.0] * 48, "precipitation": [0.0] * 96}}
    frame = parse_open_meteo(air, weather, pd.Timestamp("2026-01-03 12:00"))
    assert list(frame.columns) == ["timestamp"] + OBS_COLUMNS
    assert frame["timestamp"].max() == pd.Timestamp("2026-01-03 12:00")
    assert frame["co"].iloc[0] == pytest.approx(0.8)                     # µg/m³ -> mg/m³
    at = frame.set_index("timestamp")
    assert at.loc["2026-01-01 00:00", "fc_wind_speed"] == pytest.approx(2.0)
    assert at.loc["2026-01-02 23:00", "fc_wind_speed"] == pytest.approx(6.0)   # looks into the forecast hours
