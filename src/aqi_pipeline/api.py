"""HTTP API and Operations Console over the pipeline's store and registry.

    GET  /                    live Web Dashboard / Operations Console UI
    GET  /report              HTML monitoring report
    GET  /dashboard/data      consolidated dashboard state, metrics, and forecast series
    GET  /observations/recent recent hourly observations (for What-If /predict)
    GET  /api/config          sanitized runtime configuration and store/registry telemetry
    GET  /api/analytics       category counts, monthly MAE, feature importance, drift history
    GET  /health              liveness and champion version
    GET  /forecast            latest served forecasts (the next ``hours`` target times)
    GET  /model               champion metadata: how it was trained and validated
    GET  /monitoring          the latest cycle: quality, drift, accuracy, decision
    GET  /events              recent audit-log entries
    POST /predict             stateless scoring of caller-supplied recent observations
    POST /predict/batch       stateless batch scoring over >=24 hourly observations or CSV

Configuration comes from AQI_CONFIG (YAML path) and AQI_RUN_DIR.
"""

from __future__ import annotations

import html
import io
import json
import math
import os
import platform
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .aqi import CATEGORIES, category_name, compute_aqi
from .config import load_config
from .features import build_dataset
from .pipeline import Pipeline
from .report import build_report, collect
from .schema import OBS_COLUMNS
from .synthetic import generate
from .validation import apply_mask, validate_batch

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
STATIC_DIR = Path(__file__).with_name("static")
STATIC_INDEX = STATIC_DIR / "index.html"
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
SECRET_KEY_TOKENS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key")
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<circle cx="16" cy="16" r="14" fill="#2a78d6"/>'
    '<path d="M8 19l5-6 4 4 7-8" stroke="#fff" stroke-width="3" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def get_pipeline() -> Pipeline:
    run_dir = os.environ.get("AQI_RUN_DIR")
    cfg = load_config(os.environ.get("AQI_CONFIG"), {"run_dir": run_dir} if run_dir else None)
    return Pipeline(cfg)


class Observation(BaseModel):
    timestamp: str
    pm25: float | None = None
    pm10: float | None = None
    no2: float | None = None
    so2: float | None = None
    co: float | None = None
    o3: float | None = None
    temperature: float | None = None
    humidity: float | None = None
    wind_speed: float | None = None
    precipitation: float | None = None
    fc_temperature: float | None = None
    fc_humidity: float | None = None
    fc_wind_speed: float | None = None
    fc_precipitation: float | None = None


class PredictRequest(BaseModel):
    observations: list[Observation] = Field(
        ..., description="Hourly rows, oldest first. At least 24; nine days gives every feature its history."
    )


class BatchPredictRequest(BaseModel):
    observations: list[Observation] | None = Field(
        default=None,
        description="Hourly rows, oldest first. At least 24; scores every valid row from row 24 onward.",
    )
    csv: str | None = Field(default=None, description="Raw CSV text with hourly rows and at least 24 rows.")
    columns: dict[str, str] | None = Field(
        default=None, description="Optional column mapping from pipeline names to CSV headers."
    )
    scale: dict[str, float] | None = Field(
        default=None, description="Optional unit multipliers keyed by pipeline column name."
    )


app = FastAPI(title="AQI forecasting pipeline", version="0.1.0")
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _champion(pipe: Pipeline) -> str:
    champion = pipe.registry.champion
    if not champion:
        raise HTTPException(status_code=503, detail="no model has been trained yet")
    return champion


def _json_safe(obj: Any) -> Any:
    """Recursively convert numpy/pandas values and non-finite floats into strict JSON-safe primitives."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        val = float(obj)
        return None if (math.isnan(val) or math.isinf(val)) else val
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.strftime(TS_FORMAT)
    if isinstance(obj, np.ndarray):
        return [_json_safe(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return str(obj)


def _load_summary(pipe: Pipeline) -> dict[str, Any] | None:
    summary_path = pipe.cfg.db_path.parent / "summary.json"
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _empty_report_html(location: str, has_model: bool = False) -> str:
    detail = (
        "A model exists, but no monitoring cycles or scored forecasts have been recorded yet."
        if has_model
        else "No model has been trained yet for this run directory."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AQI monitoring report unavailable</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #f9f9f7; color: #0b0b0b;
         margin: 0; padding: 48px 24px; }}
  .box {{ max-width: 640px; margin: 0 auto; background: #fcfcfb; border: 1px solid rgba(11,11,11,0.12);
          border-radius: 10px; padding: 24px 28px; }}
  h1 {{ margin: 0 0 8px; font-size: 20px; }}
  p {{ color: #52514e; line-height: 1.5; }}
  code {{ background: rgba(11,11,11,0.06); padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  a {{ color: #2a78d6; text-decoration: none; font-weight: 600; }}
</style>
</head>
<body>
  <div class="box">
    <h1>Monitoring report not available yet</h1>
    <p><strong>Station:</strong> {html.escape(location)}</p>
    <p>{detail} Run <code>aqi-pipeline simulate</code> or
    <code>aqi-pipeline bootstrap</code> + <code>run-cycle</code> first.</p>
    <p><a href="/">&larr; Back to Live Dashboard</a></p>
  </div>
</body>
</html>"""


def _frame_to_observations(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        item: dict[str, Any] = {"timestamp": pd.Timestamp(ts).strftime(TS_FORMAT)}
        for col in OBS_COLUMNS:
            val = row.get(col)
            item[col] = None if val is None or pd.isna(val) else round(float(val), 4)
        rows.append(item)
    return rows


def _format_forecasts(preds: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "target_time": str(r.target_time),
            "made_at": str(r.made_at),
            "aqi": None if pd.isna(r.y_pred) else round(float(r.y_pred), 1),
            "model_aqi": None if pd.isna(r.y_model) else round(float(r.y_model), 1),
            "persistence_aqi": None if pd.isna(r.y_naive) else round(float(r.y_naive), 1),
            "category": category_name(r.y_pred),
            "source": r.served,
            "inputs_clean": bool(r.input_ok),
        }
        for r in preds.itertuples()
    ]


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return (
        any(tok in low for tok in SECRET_KEY_TOKENS)
        or low in ("key", "auth")
        or low.endswith(("_key", "_token", "_secret", "_auth"))
    )


def _sanitize_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): ("***" if _is_secret_key(str(k)) and v is not None else _sanitize_mapping(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_mapping(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _category_counts(values: pd.Series) -> dict[str, int]:
    counts = {name: 0 for name in CATEGORIES}
    valid = values.dropna()
    if valid.empty:
        return counts
    for v in valid:
        name = category_name(float(v))
        if name in counts:
            counts[name] += 1
    return counts


def _pipeline_stages(
    latest_cycle: dict[str, Any] | None,
    champion: str | None,
    shadow: str | None,
    version_count: int,
    retraining_stats: dict[str, Any],
) -> list[dict[str, Any]]:
    if not latest_cycle:
        return [
            {"id": "ingest", "name": "Ingest", "status": "empty", "metric": "0 rows", "detail": "No cycle run yet"},
            {"id": "quality", "name": "Quality Gate", "status": "empty", "metric": "n/a", "detail": "Awaiting data"},
            {"id": "forecast", "name": "Forecast", "status": "empty", "metric": "0 issued", "detail": "No champion"},
            {"id": "monitor", "name": "Monitor", "status": "empty", "metric": "n/a", "detail": "No signals"},
            {"id": "decide", "name": "Decide", "status": "empty", "metric": "none", "detail": "No decision"},
            {
                "id": "retrain",
                "name": "Retrain & Gate",
                "status": "empty",
                "metric": f"{retraining_stats['attempts']} attempts",
                "detail": "No retraining yet",
            },
            {
                "id": "registry",
                "name": "Registry",
                "status": "ok" if champion else "empty",
                "metric": champion or "unbootstrapped",
                "detail": f"{version_count} version(s)",
            },
        ]

    quality = latest_cycle.get("quality") or {}
    q_status = quality.get("status", "no_data")
    q_rows = int(quality.get("rows", 0) or 0)
    ingest_status = "ok" if q_rows > 0 else "idle"
    ingest_detail = f"{q_rows} hourly rows" if q_rows > 0 else "No new rows"

    if q_status == "fail":
        gate_status = "alert"
    elif q_status == "warn":
        gate_status = "warn"
    else:
        gate_status = "ok"
    bad_frac = latest_cycle.get("quality_bad_fraction")
    gate_metric = q_status if bad_frac is None else f"{q_status} ({float(bad_frac):.1%} core bad)"
    gate_issues = quality.get("issues") or []
    gate_detail = "; ".join(gate_issues[:2]) if gate_issues else "All sensors within limits"

    n_forecasts = int(latest_cycle.get("forecasts", 0) or 0)
    safe_mode = bool(latest_cycle.get("safe_mode"))
    fc_status = "warn" if safe_mode else ("ok" if n_forecasts > 0 else "idle")
    fc_metric = "persistence (safe)" if safe_mode else f"{n_forecasts} forecasts"
    fc_detail = "Serving persistence fallback" if safe_mode else f"Serving champion {champion or 'n/a'}"

    drift = latest_cycle.get("drift") or {}
    perf = latest_cycle.get("performance") or {}
    cusum = latest_cycle.get("cusum") or {}
    d_level = drift.get("level", "ok")
    p_level = perf.get("level", "unknown")
    if d_level == "alert" or p_level == "critical" or cusum.get("alarm"):
        mon_status = "alert"
    elif d_level == "warn" or p_level == "warn":
        mon_status = "warn"
    else:
        mon_status = "ok"
    ratio = perf.get("ratio")
    d_score = drift.get("score")
    mon_metric = f"drift {d_score if d_score is not None else 'n/a'} | ratio {ratio if ratio is not None else 'n/a'}"
    mon_detail = f"drift={d_level}, perf={p_level}, cusum={'alarm' if cusum.get('alarm') else 'ok'}"

    decision = latest_cycle.get("decision") or {}
    action = decision.get("action", "none")
    dec_status = "alert" if action == "hold" else ("warn" if action in ("watch", "retrain") else "ok")
    dec_detail = decision.get("reason", "")

    retrain = latest_cycle.get("retrain")
    if retrain:
        ret_status = "ok" if retrain.get("promoted") else "warn"
        ret_metric = "promoted" if retrain.get("promoted") else "rejected"
        ret_detail = retrain.get("reason", "")
    else:
        ret_status = "idle"
        ret_metric = f"{retraining_stats['promotions']}/{retraining_stats['attempts']} promoted"
        ret_detail = "Not triggered in latest cycle"

    reg_detail = f"{version_count} version(s)" + (f", shadow={shadow}" if shadow else "")
    return [
        {"id": "ingest", "name": "Ingest", "status": ingest_status, "metric": ingest_detail, "detail": ingest_detail},
        {"id": "quality", "name": "Quality Gate", "status": gate_status, "metric": gate_metric, "detail": gate_detail},
        {"id": "forecast", "name": "Forecast", "status": fc_status, "metric": fc_metric, "detail": fc_detail},
        {"id": "monitor", "name": "Monitor", "status": mon_status, "metric": mon_metric, "detail": mon_detail},
        {"id": "decide", "name": "Decide", "status": dec_status, "metric": action, "detail": dec_detail},
        {"id": "retrain", "name": "Retrain & Gate", "status": ret_status, "metric": ret_metric, "detail": ret_detail},
        {
            "id": "registry",
            "name": "Registry",
            "status": "ok" if champion else "empty",
            "metric": champion or "unbootstrapped",
            "detail": reg_detail,
        },
    ]


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    if STATIC_INDEX.is_file():
        return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"))
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/report", response_class=HTMLResponse)
def report_page() -> HTMLResponse:
    pipe = get_pipeline()
    report_path = pipe.cfg.report_dir / "monitoring_report.html"
    if report_path.exists():
        db_newer = pipe.cfg.db_path.exists() and pipe.cfg.db_path.stat().st_mtime > report_path.stat().st_mtime + 1.0
        if not db_newer or pipe.registry.champion is None:
            return HTMLResponse(report_path.read_text(encoding="utf-8"))
    if pipe.registry.champion is None:
        return HTMLResponse(_empty_report_html(pipe.cfg.location, has_model=False), status_code=404)
    summary = _load_summary(pipe)
    cycles = pipe.store.load_cycles()
    has_preds = not pipe.store.load_predictions(role="champion").empty
    if summary is None and not cycles and not has_preds:
        return HTMLResponse(_empty_report_html(pipe.cfg.location, has_model=True), status_code=404)
    built = build_report(pipe, summary, report_path)
    return HTMLResponse(built.read_text(encoding="utf-8"))


@app.get("/observations/recent")
def recent_observations(hours: int = 216, sample: bool = False) -> dict[str, Any]:
    clamped = max(1, min(hours, 24 * 30))
    pipe = get_pipeline()
    last = pipe.store.last_observation_time()
    if last is not None:
        start = last - pd.Timedelta(hours=clamped - 1)
        obs = pipe.store.load_observations(start=start, end=last, clean=False).tail(clamped)
        rows = _frame_to_observations(obs)
        return _json_safe(
            {
                "location": pipe.cfg.location,
                "source": "station",
                "count": len(rows),
                "hours": clamped,
                "start": rows[0]["timestamp"] if rows else None,
                "end": rows[-1]["timestamp"] if rows else None,
                "observations": rows,
            }
        )
    if sample:
        end = pd.Timestamp.now("UTC").tz_localize(None).floor("h") + pd.Timedelta(hours=1)
        start = end - pd.Timedelta(hours=clamped)
        syn = generate(str(start), str(end), seed=7).set_index("timestamp").tail(clamped)
        rows = _frame_to_observations(syn)
        return _json_safe(
            {
                "location": pipe.cfg.location,
                "source": "synthetic_sample",
                "count": len(rows),
                "hours": clamped,
                "start": rows[0]["timestamp"] if rows else None,
                "end": rows[-1]["timestamp"] if rows else None,
                "observations": rows,
            }
        )
    return {
        "location": pipe.cfg.location,
        "source": "none",
        "count": 0,
        "hours": clamped,
        "start": None,
        "end": None,
        "observations": [],
    }


@app.get("/dashboard/data")
def dashboard_data(hours: int = 72) -> dict[str, Any]:
    clamped = max(1, min(hours, 24 * 7))
    pipe = get_pipeline()
    summary = _load_summary(pipe)
    data = collect(pipe, summary)

    champion = pipe.registry.champion
    shadow = pipe.registry.shadow
    last_obs = pipe.store.last_observation_time()
    latest_cycle = pipe.store.latest_cycle()
    safe_mode = bool(pipe.store.get_state("safe_mode", False))

    champion_meta = None
    if champion:
        champion_meta = pipe.registry.meta(champion)
        champion_meta.pop("drift_thresholds", None)

    future_forecasts: list[dict[str, Any]] = []
    hourly_series: list[dict[str, Any]] = []
    current_info: dict[str, Any] | None = None

    if last_obs is not None:
        window_start = last_obs - pd.Timedelta(hours=clamped - 1)
        raw_tail = pipe.store.load_observations(start=window_start - pd.Timedelta(hours=48), end=last_obs, clean=False)
        clean_tail = apply_mask(raw_tail)[OBS_COLUMNS]
        aqi_df = compute_aqi(clean_tail)

        if last_obs in raw_tail.index:
            latest_raw = raw_tail.loc[last_obs]
            latest_clean = clean_tail.loc[last_obs]
            latest_aq = aqi_df.loc[last_obs]
            aqi_val = None if pd.isna(latest_aq["aqi"]) else round(float(latest_aq["aqi"]), 1)
            dom = latest_aq["dominant"]
            current_info = {
                "timestamp": last_obs.strftime(TS_FORMAT),
                "aqi": aqi_val,
                "category": category_name(aqi_val) if aqi_val is not None else "Unknown",
                "dominant": None if dom is None or pd.isna(dom) else str(dom),
                "qc_mask": int(latest_raw["qc_mask"]),
                "qc_notes": str(latest_raw["qc_notes"] or ""),
                "readings": {
                    col: (None if pd.isna(latest_clean[col]) else round(float(latest_clean[col]), 2))
                    for col in OBS_COLUMNS
                },
                "raw_readings": {
                    col: (None if pd.isna(latest_raw[col]) else round(float(latest_raw[col]), 2)) for col in OBS_COLUMNS
                },
            }

        pred_map: dict[pd.Timestamp, Any] = {}
        if champion:
            preds = pipe.store.load_predictions(
                start=window_start - pd.Timedelta(hours=1), role="champion", by="target_time"
            )
            max_target = last_obs + pd.Timedelta(hours=max(clamped, pipe.horizon))
            preds = preds[preds["target_time"] <= max_target]
            if not preds.empty:
                preds = preds.drop_duplicates(subset=["target_time"], keep="last")
                for row in preds.itertuples():
                    pred_map[pd.Timestamp(row.target_time)] = row

            future_preds = preds[preds["target_time"] > last_obs].head(clamped)
            future_forecasts = _format_forecasts(future_preds)

        max_ts = max([last_obs, *pred_map.keys()]) if pred_map else last_obs
        timeline = pd.date_range(window_start, max_ts, freq="h")
        for ts in timeline:
            actual_val = aqi_df["aqi"].get(ts) if ts <= last_obs and ts in aqi_df.index else None
            actual_num = None if actual_val is None or pd.isna(actual_val) else round(float(actual_val), 1)
            p = pred_map.get(ts)
            fc_num = None if p is None or pd.isna(p.y_pred) else round(float(p.y_pred), 1)
            mod_num = None if p is None or pd.isna(p.y_model) else round(float(p.y_model), 1)
            naive_num = None if p is None or pd.isna(p.y_naive) else round(float(p.y_naive), 1)
            hourly_series.append(
                {
                    "timestamp": ts.strftime(TS_FORMAT),
                    "made_at": pd.Timestamp(p.made_at).strftime(TS_FORMAT) if p is not None else None,
                    "actual_aqi": actual_num,
                    "actual_category": category_name(actual_num) if actual_num is not None else None,
                    "forecast_aqi": fc_num,
                    "model_aqi": mod_num,
                    "persistence_aqi": naive_num,
                    "category": category_name(fc_num) if fc_num is not None else None,
                    "source": p.served if p is not None else None,
                    "inputs_clean": bool(p.input_ok) if p is not None else None,
                    "is_future": bool(ts > last_obs),
                }
            )

    payload = {
        **data,
        "status": {
            "bootstrapped": bool(champion is not None),
            "location": pipe.cfg.location,
            "champion": champion,
            "shadow": shadow,
            "champion_meta": champion_meta,
            "last_observation": last_obs.strftime(TS_FORMAT) if last_obs is not None else None,
            "horizon_hours": pipe.horizon,
            "safe_mode": safe_mode,
            "latest_cycle": latest_cycle,
            "current": current_info,
        },
        "forecast": {
            "champion": champion,
            "issued_up_to": last_obs.strftime(TS_FORMAT) if last_obs is not None else None,
            "horizon_hours": pipe.horizon,
            "requested_hours": clamped,
            "forecasts": future_forecasts,
        },
        "hourly_series": hourly_series,
    }
    return _json_safe(payload)


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    pipe = get_pipeline()
    cfg = pipe.cfg
    champion = pipe.registry.champion
    shadow = pipe.registry.shadow
    db_exists = cfg.db_path.exists()
    db_size = int(cfg.db_path.stat().st_size) if db_exists else 0
    report_path = cfg.report_dir / "monitoring_report.html"
    versions = pipe.registry.versions()
    cycles = pipe.store.load_cycles()
    events = pipe.store.load_events()
    sim = dict(cfg.simulation or {})
    sim_summary = {
        "configured": bool(sim),
        "seed": sim.get("seed"),
        "history_start": sim.get("history_start"),
        "production_start": sim.get("production_start"),
        "production_end": sim.get("production_end"),
        "cycle_hours": sim.get("cycle_hours"),
        "baseline_interval_days": sim.get("baseline_interval_days"),
        "event_count": len(sim.get("events", [])) if isinstance(sim.get("events"), list) else 0,
    }
    return _json_safe(
        {
            "location": cfg.location,
            "run_dir": str(cfg.run_dir),
            "data": asdict(cfg.data),
            "model": asdict(cfg.model),
            "monitor": asdict(cfg.monitor),
            "retrain": asdict(cfg.retrain),
            "source": _sanitize_mapping(cfg.source),
            "simulation": sim_summary,
            "bootstrapped": bool(champion is not None),
            "champion": champion,
            "shadow": shadow,
            "db_exists": db_exists,
            "db_size_bytes": db_size,
            "report_exists": report_path.exists(),
            "version_count": len(versions),
            "cycle_count": len(cycles),
            "event_count": len(events),
            "python_version": platform.python_version(),
        }
    )


@app.get("/api/analytics")
def api_analytics() -> dict[str, Any]:
    pipe = get_pipeline()
    summary = _load_summary(pipe)
    data = collect(pipe, summary)
    champion = pipe.registry.champion
    shadow = pipe.registry.shadow
    cycles = pipe.store.load_cycles()
    events = pipe.store.load_events()

    preds = pipe.store.load_predictions(role="champion", by="made_at")
    scored = preds[
        (preds["label_ok"] == 1) & (preds["input_ok"] == 1) & preds["y_true"].notna() & preds["y_pred"].notna()
    ]
    cat_actual = _category_counts(scored["y_true"])
    cat_served = _category_counts(scored["y_pred"])

    champion_importance: list[dict[str, Any]] = []
    if champion:
        imp = pipe.registry.meta(champion).get("importance") or {}
        champion_importance = [
            {"feature": str(k), "importance": round(float(v), 4)}
            for k, v in sorted(imp.items(), key=lambda kv: -float(kv[1]))
        ]

    version_metrics: list[dict[str, Any]] = []
    for v in pipe.registry.versions():
        meta = pipe.registry.meta(v["version"])
        val = meta.get("validation") or {}
        version_metrics.append(
            {
                "version": v["version"],
                "stage": v["stage"],
                "kind": meta.get("kind"),
                "strategy": meta.get("strategy"),
                "trigger": meta.get("trigger"),
                "trained_at": meta.get("trained_at"),
                "n_train_rows": meta.get("n_train_rows"),
                "val_mae": val.get("mae"),
                "val_rel_mae": val.get("rel_mae"),
                "ref_rel_mae": meta.get("ref_rel_mae"),
                "category_accuracy": val.get("category_accuracy"),
            }
        )

    decision_counts = {"none": 0, "watch": 0, "hold": 0, "retrain": 0}
    drift_history: list[dict[str, Any]] = []
    for c in cycles:
        act = (c.get("decision") or {}).get("action")
        if act in decision_counts:
            decision_counts[act] += 1
        elif act:
            decision_counts[act] = decision_counts.get(act, 0) + 1
        d = c.get("drift") or {}
        drifted = d.get("drifted") or []
        drift_history.append(
            {
                "cycle_time": c.get("cycle_time"),
                "date": str(c.get("cycle_time", ""))[:10],
                "level": d.get("level"),
                "score": d.get("score"),
                "reference": d.get("reference"),
                "drifted": drifted,
                "drifted_count": len(drifted),
            }
        )

    promotions = sum(1 for e in events if e["kind"] == "promotion")
    rejected = sum(1 for e in events if e["kind"] == "retrain_rejected")
    rollbacks = sum(1 for e in events if e["kind"] == "rollback")
    attempts = promotions + rejected
    retraining_stats = {
        "attempts": attempts,
        "promotions": promotions,
        "rejected": rejected,
        "rollbacks": rollbacks,
        "promotion_rate": round(promotions / attempts, 4) if attempts else 0.0,
        "scored_hours": int(data.get("scored_hours", len(scored)) or 0),
        "safe_mode_hours": int(data.get("safe_mode_hours", 0) or 0),
    }

    latest_cycle = cycles[-1] if cycles else None
    stages = _pipeline_stages(latest_cycle, champion, shadow, len(version_metrics), retraining_stats)
    return _json_safe(
        {
            "bootstrapped": bool(champion is not None),
            "champion": champion,
            "category_counts": {"actual": cat_actual, "served": cat_served},
            "monthly_mae": data.get("monthly_mae", []),
            "feature_importance": champion_importance,
            "version_metrics": version_metrics,
            "decision_counts": decision_counts,
            "retraining_stats": retraining_stats,
            "drift_history": drift_history,
            "pipeline_stages": stages,
        }
    )


@app.get("/health")
def health() -> dict[str, Any]:
    pipe = get_pipeline()
    last = pipe.store.last_observation_time()
    return {
        "status": "ok",
        "champion": pipe.registry.champion,
        "last_observation": str(last) if last is not None else None,
    }


@app.get("/forecast")
def forecast(hours: int = 24) -> dict[str, Any]:
    pipe = get_pipeline()
    champion = _champion(pipe)
    last = pipe.store.last_observation_time()
    if last is None:
        raise HTTPException(status_code=503, detail="no observations yet")
    preds = pipe.store.load_predictions(start=last, role="champion").head(max(1, min(hours, 24 * 7)))
    return {
        "champion": champion,
        "issued_up_to": str(last),
        "horizon_hours": pipe.horizon,
        "forecasts": [
            {
                "target_time": str(r.target_time),
                "made_at": str(r.made_at),
                "aqi": None if pd.isna(r.y_pred) else round(float(r.y_pred), 1),
                "category": category_name(r.y_pred),
                "source": r.served,
                "inputs_clean": bool(r.input_ok),
            }
            for r in preds.itertuples()
        ],
    }


@app.get("/model")
def model() -> dict[str, Any]:
    pipe = get_pipeline()
    meta = pipe.registry.meta(_champion(pipe))
    meta.pop("drift_thresholds", None)
    return {"champion": meta, "shadow": pipe.registry.shadow, "history": pipe.registry.history()[-20:]}


@app.get("/monitoring")
def monitoring() -> dict[str, Any]:
    pipe = get_pipeline()
    latest = pipe.store.latest_cycle()
    if not latest:
        raise HTTPException(status_code=404, detail="no cycle has run yet")
    return latest


@app.get("/events")
def events(limit: int = 50) -> list[dict[str, Any]]:
    return get_pipeline().store.load_events(limit=max(1, min(limit, 1000)))


@app.post("/predict")
def predict(request: PredictRequest) -> dict[str, Any]:
    pipe = get_pipeline()
    champion = _champion(pipe)
    if len(request.observations) < 24:
        raise HTTPException(status_code=422, detail="send at least 24 hourly observations")
    frame = pd.DataFrame([o.model_dump() for o in request.observations])
    try:
        validated, report = validate_batch(frame, pipe.cfg.data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ds = build_dataset(
        apply_mask(validated)[OBS_COLUMNS], pipe.horizon, pipe.cfg.data.min_label_hours, pipe.cfg.model.use_day_of_year
    )
    at = ds.X.index[-1:]
    naive = ds.aqi_now.ffill(limit=24).loc[at]
    value = float(pipe.registry.load(champion).predict(ds.X.loc[at], naive)[0])
    if pd.isna(value):
        raise HTTPException(status_code=422, detail="not enough valid PM readings to compute the current AQI")
    return {
        "champion": champion,
        "made_at": str(at[0]),
        "target_time": str(at[0] + pd.Timedelta(hours=pipe.horizon)),
        "aqi": round(value, 1),
        "category": category_name(value),
        "current_aqi": None if pd.isna(naive.iloc[0]) else float(naive.iloc[0]),
        "inputs_clean": bool(ds.aqi_ok.loc[at[0]]),
        "data_quality": report.status,
        "issues": report.issues,
    }


@app.post("/predict/batch")
def predict_batch(request: BatchPredictRequest) -> dict[str, Any]:
    pipe = get_pipeline()
    champion = _champion(pipe)
    if request.observations is not None:
        if len(request.observations) < 24:
            raise HTTPException(status_code=422, detail="send at least 24 hourly observations")
        frame = pd.DataFrame([o.model_dump() for o in request.observations])
    elif request.csv is not None and request.csv.strip():
        try:
            frame = pd.read_csv(io.StringIO(request.csv))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid CSV payload: {exc}") from exc
        if request.columns:
            mapping = {v: k for k, v in request.columns.items()}
            frame = frame.rename(columns=mapping)
        if request.scale:
            for col, factor in request.scale.items():
                if col in frame.columns:
                    frame[col] = pd.to_numeric(frame[col], errors="coerce") * factor
        if len(frame) < 24:
            raise HTTPException(status_code=422, detail="send at least 24 hourly observations in CSV")
    else:
        raise HTTPException(status_code=422, detail="provide either 'observations' (>=24 rows) or non-empty 'csv'")

    try:
        validated, report = validate_batch(frame, pipe.cfg.data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if len(validated) < 24:
        raise HTTPException(status_code=422, detail="at least 24 unique hourly rows are required after deduplication")

    ds = build_dataset(
        apply_mask(validated)[OBS_COLUMNS],
        pipe.horizon,
        pipe.cfg.data.min_label_hours,
        pipe.cfg.model.use_day_of_year,
    )
    cand_idx = ds.X.index[23:]
    naive = ds.aqi_now.ffill(limit=24).loc[cand_idx]
    preds = pipe.registry.load(champion).predict(ds.X.loc[cand_idx], naive)

    rows: list[dict[str, Any]] = []
    for ts, pred_val, cur_val, ok_val in zip(cand_idx, preds, naive.to_numpy(), ds.aqi_ok.loc[cand_idx].to_numpy()):
        if pd.isna(pred_val):
            continue
        aqi_num = round(float(pred_val), 1)
        cur_num = None if pd.isna(cur_val) else round(float(cur_val), 1)
        rows.append(
            {
                "made_at": pd.Timestamp(ts).strftime(TS_FORMAT),
                "target_time": (pd.Timestamp(ts) + pd.Timedelta(hours=pipe.horizon)).strftime(TS_FORMAT),
                "aqi": aqi_num,
                "current_aqi": cur_num,
                "category": category_name(aqi_num),
                "inputs_clean": bool(ok_val),
            }
        )

    if not rows:
        raise HTTPException(status_code=422, detail="not enough valid PM readings to compute AQI predictions")

    rows = rows[-240:]
    aqi_vals = [r["aqi"] for r in rows]
    cat_counts = {name: 0 for name in CATEGORIES}
    for r in rows:
        if r["category"] in cat_counts:
            cat_counts[r["category"]] += 1

    return _json_safe(
        {
            "champion": champion,
            "count": len(rows),
            "summary": {
                "mean_aqi": round(float(np.mean(aqi_vals)), 1),
                "min_aqi": round(float(np.min(aqi_vals)), 1),
                "max_aqi": round(float(np.max(aqi_vals)), 1),
                "category_counts": cat_counts,
            },
            "data_quality": report.status,
            "issues": report.issues,
            "predictions": rows,
        }
    )
