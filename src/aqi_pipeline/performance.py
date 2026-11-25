"""Performance monitoring with delayed labels.

The true AQI for a forecast arrives ``horizon`` hours after it was issued, so
accuracy is always judged on forecasts that have matured. Two things make the
signal trustworthy:

* Forecast error is compared with what the persistence forecast ("tomorrow =
  today") scored on the same hours. A stormy week is hard for every method; the
  question is whether the model has lost its edge over persistence. The model's
  expected edge (``ref_rel_mae``) is measured when it is validated.
* Only forecasts made from clean inputs and scored against clean labels count,
  so a sensor fault cannot pass for model decay.

A one-sided CUSUM on the daily error ratio catches slow decay that never
crosses the window thresholds, and a safe mode serves the persistence forecast
whenever the model is doing worse than it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import MonitorConfig
from .modeling import score


def valid_rows(preds: pd.DataFrame) -> pd.DataFrame:
    ok = (
        (preds["label_ok"] == 1)
        & (preds["input_ok"] == 1)
        & preds["y_true"].notna()
        & preds["y_model"].notna()
        & preds["y_naive"].notna()
    )
    return preds[ok]


def performance_report(
    preds: pd.DataFrame, ref_rel_mae: float, cfg: MonitorConfig, expected_rows: int
) -> dict[str, Any]:
    """Accuracy of matured forecasts in the monitoring window against expectation.

    ``ratio`` = MAE / (ref_rel_mae x persistence MAE): 1.0 means the model keeps
    exactly the edge over persistence it showed at validation.
    """
    good = valid_rows(preds)
    coverage = len(good) / expected_rows if expected_rows else 0.0
    report: dict[str, Any] = {"level": "unknown", "n": int(len(good)), "coverage": round(coverage, 3)}
    if len(good) < 24 or coverage < cfg.min_coverage:
        return report
    metrics = score(good["y_true"], good["y_model"], good["y_naive"])
    expected = ref_rel_mae * metrics["mae_naive"]
    ratio = metrics["mae"] / max(expected, 1e-6)
    gap = metrics["mae"] - expected
    if ratio >= cfg.perf_critical_ratio and gap >= cfg.perf_min_gap:
        level = "critical"
    elif ratio >= cfg.perf_warn_ratio and gap >= cfg.perf_min_gap:
        level = "warn"
    else:
        level = "ok"
    report.update(
        {
            "level": level,
            "mae": round(metrics["mae"], 3),
            "rmse": round(metrics["rmse"], 3),
            "bias": round(metrics["bias"], 3),
            "mae_naive": round(metrics["mae_naive"], 3),
            "rel_mae": None if np.isnan(metrics["rel_mae"]) else round(metrics["rel_mae"], 4),
            "expected_mae": round(expected, 3),
            "ratio": round(ratio, 3),
            "gap": round(gap, 3),
            "category_accuracy": round(metrics["category_accuracy"], 4),
        }
    )
    return report


def daily_ratio(preds: pd.DataFrame, ref_rel_mae: float, day_end: pd.Timestamp, floor: float) -> float | None:
    """Error ratio for the 24 hours of matured forecasts ending at ``day_end``."""
    good = valid_rows(preds)
    good = good[(good["target_time"] > day_end - pd.Timedelta(hours=24)) & (good["target_time"] <= day_end)]
    if len(good) < 12:
        return None
    mae = float((good["y_model"] - good["y_true"]).abs().mean())
    naive = float((good["y_naive"] - good["y_true"]).abs().mean())
    return mae / max(ref_rel_mae * naive, floor)


def cusum_update(state: dict[str, float] | None, ratio: float | None, cfg: MonitorConfig) -> dict[str, Any]:
    """One-sided CUSUM on (daily ratio - 1): accumulates only sustained excess error."""
    g = float(state["g"]) if state else 0.0
    if ratio is not None and not np.isnan(ratio):
        x = min(ratio - 1.0, 1.5)
        g = max(0.0, g + x - cfg.cusum_slack)
    return {"g": round(g, 4), "alarm": bool(g > cfg.cusum_alarm)}


def safe_mode_update(
    active: bool, preds: pd.DataFrame, now: pd.Timestamp, cfg: MonitorConfig
) -> tuple[bool, dict[str, Any]]:
    """Decide whether to serve persistence instead of the model.

    Enters when, over the last ``safe_window_days`` of matured forecasts, the
    model's MAE is clearly worse than persistence; leaves when the model is
    clearly better again. The model keeps forecasting in the background either
    way, so its recovery (or its replacement) is visible.
    """
    good = valid_rows(preds)
    good = good[good["target_time"] > now - pd.Timedelta(days=cfg.safe_window_days)]
    info: dict[str, Any] = {"n": int(len(good))}
    if len(good) < 36:
        return active, info
    mae = float((good["y_model"] - good["y_true"]).abs().mean())
    naive = float((good["y_naive"] - good["y_true"]).abs().mean())
    ratio = mae / max(naive, 1e-6)
    info.update({"model_mae": round(mae, 2), "persistence_mae": round(naive, 2), "ratio": round(ratio, 3)})
    if not active and ratio >= cfg.safe_enter_ratio and mae - naive >= cfg.perf_min_gap:
        return True, info
    if active and ratio <= cfg.safe_exit_ratio:
        return False, info
    return active, info
