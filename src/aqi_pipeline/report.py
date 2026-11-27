"""Self-contained HTML monitoring report (no external assets; works offline).

The report is built from what the pipeline recorded: served forecasts and their
outcomes, per-cycle monitoring snapshots, the event log and the model registry.
For a simulation run it also shows the two baselines from the run summary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .modeling import score
from .pipeline import Pipeline

TEMPLATE = Path(__file__).with_name("report_template.html")


def _served(pipe: Pipeline) -> pd.DataFrame:
    """Served forecasts that can be scored: clean inputs, clean matured labels."""
    preds = pipe.store.load_predictions(role="champion", by="made_at")
    ok = (
        (preds["label_ok"] == 1)
        & (preds["input_ok"] == 1)
        & preds["y_true"].notna()
        & preds["y_pred"].notna()
        & preds["y_naive"].notna()
    )
    return preds[ok].set_index("made_at")


def live_summary(pipe: Pipeline) -> dict[str, Any]:
    """Accuracy summary from the forecast log alone (no baselines), for real-data runs."""
    served = _served(pipe)
    events = pipe.store.load_events()

    def count(kind: str) -> int:
        return sum(1 for e in events if e["kind"] == kind)

    summary: dict[str, Any] = {
        "location": pipe.cfg.location,
        "comparison": {},
        "monthly_mae": [],
        "daily": [],
        "scored_hours": int(len(served)),
        "safe_mode_hours": int((served["served"] == "persistence").sum()),
        "retraining": {
            "attempts": count("promotion") + count("retrain_rejected"),
            "promotions": count("promotion"),
            "rejected": count("retrain_rejected"),
            "rollbacks": count("rollback"),
        },
        "period": {"production_start": "", "production_end": ""},
        "injected_events": [],
    }
    if served.empty:
        return summary
    summary["period"] = {"production_start": str(served.index[0]), "production_end": str(served.index[-1])}

    def block(pred: pd.Series) -> dict[str, Any]:
        m = score(served["y_true"], pred, served["y_naive"])
        return {
            k: round(float(m[k]), 4)
            for k in ("mae", "rmse", "bias", "rel_mae", "category_accuracy", "category_within_one")
        }

    summary["comparison"] = {"pipeline": block(served["y_pred"]), "persistence": block(served["y_naive"])}
    err = pd.DataFrame(
        {
            "y": served["y_true"],
            "pipeline": (served["y_pred"] - served["y_true"]).abs(),
            "persistence": (served["y_naive"] - served["y_true"]).abs(),
        }
    )
    for period, g in err.groupby(err.index.to_period("M")):
        summary["monthly_mae"].append(
            {
                "month": str(period),
                "n": int(len(g)),
                "mean_aqi": round(float(g["y"].mean()), 1),
                "pipeline": round(float(g["pipeline"].mean()), 2),
                "persistence": round(float(g["persistence"].mean()), 2),
            }
        )
    for d, g in err.resample("D"):
        if len(g):
            summary["daily"].append(
                {
                    "date": str(d.date()),
                    "y": round(float(g["y"].mean()), 2),
                    "pipeline": round(float(g["pipeline"].mean()), 2),
                    "persistence": round(float(g["persistence"].mean()), 2),
                }
            )
    return summary


def _spans(dates: list[str], flags: list[Any]) -> list[dict[str, Any]]:
    """Contiguous runs of equal truthy values: [{value, start, end}]."""
    spans: list[dict[str, Any]] = []
    last_index = -2
    for i, (date, flag) in enumerate(zip(dates, flags)):
        if not flag:
            continue
        if spans and spans[-1]["value"] == flag and last_index == i - 1:
            spans[-1]["end"] = date
        else:
            spans.append({"value": flag, "start": date, "end": date})
        last_index = i
    for s in spans:
        s["end"] = str((pd.Timestamp(s["end"]) + pd.Timedelta(days=1)).date())
    return spans


def collect(pipe: Pipeline, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the report template needs, as one JSON-serialisable dict."""
    simulated = summary is not None and "seed" in summary
    data = dict(summary) if summary is not None else live_summary(pipe)
    cfg = pipe.cfg
    cycles = pipe.store.load_cycles()
    events = pipe.store.load_events()
    dates = [c["cycle_time"][:10] for c in cycles]

    served = _served(pipe)
    forecast = served["y_pred"].resample("D").mean()
    champion_by_day = {d: c.get("champion_after", c.get("champion")) for d, c in zip(dates, cycles)}
    daily = []
    for row in data.get("daily", []):
        ts = pd.Timestamp(row["date"])
        value = forecast.get(ts)
        daily.append(
            {
                **row,
                "forecast": None if value is None or pd.isna(value) else round(float(value), 2),
                "champion": champion_by_day.get(row["date"]),
            }
        )

    retrain_log = []
    for e in events:
        if e["kind"] not in ("promotion", "retrain_rejected"):
            continue
        p = e["payload"]
        retrain_log.append(
            {
                "date": e["ts"][:10],
                "trigger": p.get("trigger", ""),
                "outcome": "promoted" if e["kind"] == "promotion" else "rejected",
                "version": p.get("version"),
                "champion_mae": p.get("champion_metrics", {}).get("mae"),
                "best": p.get("best", {}).get("name", ""),
                "best_mae": p.get("best", {}).get("mae"),
                "prob": p.get("gate", {}).get("prob_better"),
                "trials": len(p.get("trials", [])),
                "reason": p.get("reason", ""),
            }
        )

    versions = []
    for v in pipe.registry.versions():
        meta = pipe.registry.meta(v["version"])
        versions.append(
            {
                "version": v["version"],
                "stage": v["stage"],
                "trained_at": meta["trained_at"],
                "trigger": meta["trigger"],
                "kind": meta["kind"],
                "strategy": meta["strategy"],
                "n_train_rows": meta["n_train_rows"],
                "val_mae": meta["validation"].get("mae"),
                "ref_rel_mae": meta["ref_rel_mae"],
            }
        )

    data.update(
        {
            "simulated": simulated,
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "daily": daily,
            "cycles": [
                {
                    "date": d,
                    "ratio": c["performance"].get("ratio"),
                    "drift": c["drift"]["score"],
                    "cusum": c["cusum"]["g"],
                    "action": c["decision"]["action"],
                    "champion": c.get("champion_after", c.get("champion")),
                }
                for d, c in zip(dates, cycles)
            ],
            "version_spans": [
                {"version": s["value"], "start": s["start"], "end": s["end"]}
                for s in _spans(dates, [c.get("champion_after", c.get("champion")) for c in cycles])
            ],
            "safe_spans": _spans(dates, [bool(c.get("safe_mode")) for c in cycles]),
            "hold_spans": _spans(dates, [c["decision"]["action"] == "hold" for c in cycles]),
            "retrain_log": retrain_log,
            "rollbacks": [{"date": e["ts"][:10], "message": e["message"]} for e in events if e["kind"] == "rollback"],
            "versions": versions,
            "events": [{"ts": e["ts"], "kind": e["kind"], "message": e["message"]} for e in events],
            "thresholds": {
                "perf_warn": cfg.monitor.perf_warn_ratio,
                "perf_critical": cfg.monitor.perf_critical_ratio,
                "drift_warn": cfg.monitor.drift_warn_score,
                "drift_alert": cfg.monitor.drift_alert_score,
                "cusum_alarm": cfg.monitor.cusum_alarm,
            },
        }
    )
    data.setdefault("injected_events", [])
    return data


def build_report(pipe: Pipeline, summary: dict[str, Any] | None = None, path: str | Path | None = None) -> Path:
    data = collect(pipe, summary)
    out = Path(path) if path else pipe.cfg.report_dir / "monitoring_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, default=str).replace("</", "<\\/")  # keep the JSON inside its <script>
    out.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    return out
