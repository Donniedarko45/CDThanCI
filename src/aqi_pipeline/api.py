"""HTTP API over the pipeline's store and registry (read-only except /predict).

    GET  /health        liveness and champion version
    GET  /forecast      latest served forecasts (the next ``hours`` target times)
    GET  /model         champion metadata: how it was trained and validated
    GET  /monitoring    the latest cycle: quality, drift, accuracy, decision
    GET  /events        recent audit-log entries
    POST /predict       stateless scoring of caller-supplied recent observations

Configuration comes from AQI_CONFIG (YAML path) and AQI_RUN_DIR.
"""
from __future__ import annotations

import os
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .aqi import category_name
from .config import load_config
from .features import build_dataset
from .pipeline import Pipeline
from .schema import OBS_COLUMNS
from .validation import apply_mask, validate_batch


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
