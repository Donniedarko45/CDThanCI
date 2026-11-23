"""HTTP API and live Web Dashboard over the pipeline's store and registry.

    GET  /                    live Web Dashboard UI
    GET  /report              HTML monitoring report
    GET  /dashboard/data      consolidated dashboard state, metrics, and forecast series
    GET  /observations/recent recent hourly observations (for What-If /predict)
    GET  /health              liveness and champion version
    GET  /forecast            latest served forecasts (the next ``hours`` target times)
    GET  /model               champion metadata: how it was trained and validated
    GET  /monitoring          the latest cycle: quality, drift, accuracy, decision
    GET  /events              recent audit-log entries
    POST /predict             stateless scoring of caller-supplied recent observations

Configuration comes from AQI_CONFIG (YAML path) and AQI_RUN_DIR.
"""
from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .aqi import category_name, compute_aqi
from .config import load_config
from .features import build_dataset
from .pipeline import Pipeline
from .report import build_report, collect
from .schema import OBS_COLUMNS
from .synthetic import generate
from .validation import apply_mask, validate_batch

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
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
        ..., description="Hourly rows, oldest first. At least 24; nine days gives every feature its history.")


app = FastAPI(title="AQI forecasting pipeline", version="0.1.0")


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
    <p>{detail} Run <code>aqi-pipeline simulate</code> or <code>aqi-pipeline bootstrap</code> + <code>run-cycle</code> first.</p>
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


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/report", response_class=HTMLResponse)
def report_page() -> HTMLResponse:
    pipe = get_pipeline()
    report_path = pipe.cfg.report_dir / "monitoring_report.html"
    if report_path.exists():
        db_newer = (
            pipe.cfg.db_path.exists()
            and pipe.cfg.db_path.stat().st_mtime > report_path.stat().st_mtime + 1.0
        )
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
        return _json_safe({
            "location": pipe.cfg.location,
            "source": "station",
            "count": len(rows),
            "hours": clamped,
            "start": rows[0]["timestamp"] if rows else None,
            "end": rows[-1]["timestamp"] if rows else None,
            "observations": rows,
        })
    if sample:
        end = pd.Timestamp.now("UTC").tz_localize(None).floor("h") + pd.Timedelta(hours=1)
        start = end - pd.Timedelta(hours=clamped)
        syn = generate(str(start), str(end), seed=7).set_index("timestamp").tail(clamped)
        rows = _frame_to_observations(syn)
        return _json_safe({
            "location": pipe.cfg.location,
            "source": "synthetic_sample",
            "count": len(rows),
            "hours": clamped,
            "start": rows[0]["timestamp"] if rows else None,
            "end": rows[-1]["timestamp"] if rows else None,
            "observations": rows,
        })
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
        raw_tail = pipe.store.load_observations(
            start=window_start - pd.Timedelta(hours=48), end=last_obs, clean=False
        )
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
                    col: (None if pd.isna(latest_raw[col]) else round(float(latest_raw[col]), 2))
                    for col in OBS_COLUMNS
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
            hourly_series.append({
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
            })

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


@app.get("/health")
def health() -> dict[str, Any]:
    pipe = get_pipeline()
    last = pipe.store.last_observation_time()
    return {"status": "ok", "champion": pipe.registry.champion,
            "last_observation": str(last) if last is not None else None}


@app.get("/forecast")
def forecast(hours: int = 24) -> dict[str, Any]:
    pipe = get_pipeline()
    champion = _champion(pipe)
    last = pipe.store.last_observation_time()
    if last is None:
        raise HTTPException(status_code=503, detail="no observations yet")
    preds = pipe.store.load_predictions(start=last, role="champion").head(max(1, min(hours, 24 * 7)))
    return {
        "champion": champion, "issued_up_to": str(last), "horizon_hours": pipe.horizon,
        "forecasts": [{"target_time": str(r.target_time), "made_at": str(r.made_at),
                       "aqi": None if pd.isna(r.y_pred) else round(float(r.y_pred), 1),
                       "category": category_name(r.y_pred), "source": r.served,
                       "inputs_clean": bool(r.input_ok)} for r in preds.itertuples()],
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
    ds = build_dataset(apply_mask(validated)[OBS_COLUMNS], pipe.horizon, pipe.cfg.data.min_label_hours,
                       pipe.cfg.model.use_day_of_year)
    at = ds.X.index[-1:]
    naive = ds.aqi_now.ffill(limit=24).loc[at]
    value = float(pipe.registry.load(champion).predict(ds.X.loc[at], naive)[0])
    if pd.isna(value):
        raise HTTPException(status_code=422, detail="not enough valid PM readings to compute the current AQI")
    return {"champion": champion, "made_at": str(at[0]),
            "target_time": str(at[0] + pd.Timedelta(hours=pipe.horizon)),
            "aqi": round(value, 1), "category": category_name(value),
            "current_aqi": None if pd.isna(naive.iloc[0]) else float(naive.iloc[0]),
            "inputs_clean": bool(ds.aqi_ok.loc[at[0]]), "data_quality": report.status,
            "issues": report.issues}
