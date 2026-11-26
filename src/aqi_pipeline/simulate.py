"""End-to-end simulation: replay a year of production against the pipeline.

A synthetic station goes live with a few months of history. The pipeline then
runs one cycle per simulated day while seasons change and the configured events
(sensor faults, recalibration, a new emission regime) hit the data stream.

To judge whether the monitoring and retraining earn their keep, the same period
is also forecast by two baselines:

* ``static``    - the initial model, never retrained
* ``scheduled`` - every ``baseline_interval_days`` the model family is re-selected by
  backtest and refitted on all data, then always deployed: no monitoring, no
  strategy trials, no gate, no safe mode

All three are scored on the same hours: forecasts made from clean inputs with
clean labels.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import Config
from .modeling import score, select_and_train, training_rows
from .pipeline import Pipeline
from .schema import to_utc_naive
from .sources import FrameSource
from .store import _json_default, _sanitize_json
from .synthetic import generate

DEFAULT_SIMULATION = {
    "seed": 7,
    "history_start": "2025-03-01",
    "production_start": "2025-09-01",
    "production_end": "2026-09-01",
    "cycle_hours": 24,
    "baseline_interval_days": 30,
    "events": [
        {"type": "sensor_stuck", "column": "pm25", "start": "2025-09-18", "end": "2025-09-23",
         "label": "PM2.5 sensor stuck"},
        {"type": "sensor_dropout", "columns": ["pm10", "no2"], "start": "2025-09-19", "end": "2025-09-22",
         "rate": 0.7, "label": "PM10 and NO2 dropouts"},
        {"type": "sensor_bias", "column": "pm10", "start": "2026-03-10", "factor": 1.3,
         "label": "PM10 analyser recalibrated (+30%)"},
        {"type": "emission_regime", "start": "2026-05-20", "ramp_days": 15,
         "params": {"ozone_scale": 2.8}, "label": "Ozone regime (precursor emissions up)"},
    ],
}


def simulation_settings(cfg: Config) -> dict[str, Any]:
    return {**DEFAULT_SIMULATION, **(cfg.simulation or {})}


def run_simulation(cfg: Config, fresh: bool = True, verbose: bool = True) -> dict[str, Any]:
    sim = simulation_settings(cfg)
    run_dir = Path(cfg.run_dir)
    if fresh and run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    data = generate(sim["history_start"], sim["production_end"], seed=int(sim["seed"]), events=sim["events"])
    pipe = Pipeline(cfg, FrameSource(data))
    start, end = pd.Timestamp(sim["production_start"]), pd.Timestamp(sim["production_end"])
    first = pipe.bootstrap(start - pd.Timedelta(hours=1))
    if verbose:
        meta = pipe.registry.meta(first)
        print(f"bootstrap: {first} ({meta['kind']}) trained on {meta['n_train_rows']} rows, "
              f"backtest MAE {meta['validation']['mae']:.1f} ({meta['validation']['rel_mae']:.2f} x persistence)")

    step = pd.Timedelta(hours=int(sim["cycle_hours"]))
    now = start + step - pd.Timedelta(hours=1)
    last = end - pd.Timedelta(hours=1)
    n_cycles = 0
    while now <= last:
        record = pipe.run_cycle(now)
        n_cycles += 1
        if verbose:
            _print_cycle(now, record)
        now += step

    summary = summarise(pipe, sim, first)
    summary["runtime_seconds"] = round(time.time() - t0, 1)
    summary["cycles"] = n_cycles
    with open(run_dir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(_sanitize_json(summary), fh, indent=2, default=_json_default, allow_nan=False)
    if verbose:
        print(f"\n{n_cycles} cycles in {summary['runtime_seconds']} s")
        for name, m in summary["comparison"].items():
            mae = f"{m['mae']:6.2f}" if m.get("mae") is not None else "   n/a"
            rmse = f"{m['rmse']:6.2f}" if m.get("rmse") is not None else "   n/a"
            cat_acc = f"{m['category_accuracy']:.1%}" if m.get("category_accuracy") is not None else "n/a"
            print(f"  {name:10s} MAE {mae}  RMSE {rmse}  category accuracy "
                  f"{cat_acc}  retrainings {m['retrainings']}")
    return summary


def _print_cycle(now: pd.Timestamp, record: dict[str, Any]) -> None:
    decision = record["decision"]
    interesting = decision["action"] in ("retrain", "hold") or "probation" in record
    if not interesting:
        return
    line = f"{now:%Y-%m-%d} {decision['action']:8s} {decision['reason']}"
    if "retrain" in record:
        r = record["retrain"]
        line += f"\n           -> {'PROMOTED ' + r.get('version', '') if r['promoted'] else 'rejected'}: {r['reason']}"
    if "probation" in record:
        p = record["probation"]
        line += f"\n           -> probation: {p['action']} " + (
            f"(live MAE {p['champion_mae']} vs shadow {p['shadow_mae']})" if "champion_mae" in p else "")
    print(line)


def summarise(pipe: Pipeline, sim: dict[str, Any], first_version: str) -> dict[str, Any]:
    """Score the pipeline and the two baselines on the same production hours."""
    cfg = pipe.cfg
    start, end = to_utc_naive(sim["production_start"]), to_utc_naive(sim["production_end"])
    ds = pipe._dataset()
    served = pipe.store.load_predictions(role="champion", by="made_at")
    served = served[(served["made_at"] >= start) & (served["target_time"] < end)].set_index("made_at")
    rows = training_rows(ds, start=start)
    rows = rows[rows.isin(served.index[served["y_pred"].notna()])]
    y, naive = ds.y.loc[rows], ds.aqi_now.loc[rows]

    static_model = pipe.registry.load(first_version)
    static_pred = pd.Series(static_model.predict(ds.X.loc[rows], naive), index=rows)

    # fixed-schedule baseline: the same selection procedure as the first model, on everything known
    scheduled_pred = pd.Series(np.nan, index=rows)
    interval = pd.Timedelta(days=int(sim["baseline_interval_days"]))
    model, t, n_scheduled = static_model, start, 0
    while t < end:
        if t > start:
            model, _ = select_and_train(pipe._dataset(end=t - pd.Timedelta(hours=1)), cfg.model, pipe.horizon)
            n_scheduled += 1
        block = rows[(rows >= t) & (rows < t + interval)]
        if len(block):
            scheduled_pred.loc[block] = model.predict(ds.X.loc[block], naive.loc[block])
        t += interval

    pipeline_pred = served.loc[rows, "y_pred"]
    events = pipe.store.load_events()
    promotions = [e for e in events if e["kind"] == "promotion"]
    rejected = [e for e in events if e["kind"] == "retrain_rejected"]
    rollbacks = [e for e in events if e["kind"] == "rollback"]

    def block(pred: pd.Series, retrainings: int) -> dict[str, Any]:
        m = score(y, pred, naive)
        if m["n"] == 0:
            return {"mae": None, "rmse": None, "bias": None, "rel_mae": None,
                    "category_accuracy": None, "category_within_one": None, "n": 0,
                    "retrainings": retrainings}
        return {"mae": round(m["mae"], 3), "rmse": round(m["rmse"], 3), "bias": round(m["bias"], 3),
                "rel_mae": None if np.isnan(m["rel_mae"]) else round(m["rel_mae"], 4),
                "category_accuracy": round(m["category_accuracy"], 4),
                "category_within_one": round(m["category_within_one"], 4), "n": m["n"],
                "retrainings": retrainings}

    comparison = {
        "pipeline": block(pipeline_pred, len(promotions) + len(rejected)),
        "static": block(static_pred, 0),
        "scheduled": block(scheduled_pred, n_scheduled),
        "persistence": block(naive, 0),
    }
    comparison["pipeline"]["promotions"] = len(promotions)

    frame = pd.DataFrame({"y": y, "pipeline": pipeline_pred, "static": static_pred,
                          "scheduled": scheduled_pred, "persistence": naive})
    monthly = []
    daily_records = []
    if not frame.empty:
        for period, g in frame.groupby(frame.index.to_period("M")):
            row = {"month": str(period), "n": int(len(g)), "mean_aqi": round(float(g["y"].mean()), 1)}
            for name in ("pipeline", "static", "scheduled", "persistence"):
                row[name] = round(float((g[name] - g["y"]).abs().mean()), 2)
            monthly.append(row)
        daily_err = pd.DataFrame({
            "y": frame["y"],
            **{n: (frame[n] - frame["y"]).abs() for n in ("pipeline", "static", "scheduled", "persistence")},
        }).resample("D").mean()
        daily_records = [{"date": str(d.date()), **{k: (None if pd.isna(v) else round(float(v), 2)) for k, v in r.items()}}
                         for d, r in daily_err.iterrows()]

    cycles = pipe.store.load_cycles()
    decisions = pd.Series([c["decision"]["action"] for c in cycles], dtype=object).value_counts().to_dict()
    safe_hours = int((served.loc[rows, "served"] == "persistence").sum()) if len(rows) else 0
    return {
        "location": cfg.location,
        "period": {"history_start": sim["history_start"], "production_start": sim["production_start"],
                   "production_end": sim["production_end"]},
        "seed": sim["seed"],
        "injected_events": sim["events"],
        "scored_hours": int(len(rows)),
        "comparison": comparison,
        "monthly_mae": monthly,
        "daily": daily_records,
        "decisions": decisions,
        "safe_mode_hours": safe_hours,
        "retraining": {"attempts": len(promotions) + len(rejected), "promotions": len(promotions),
                       "rejected": len(rejected), "rollbacks": len(rollbacks)},
        "versions": pipe.registry.versions(),
        "registry_history": pipe.registry.history(),
    }
