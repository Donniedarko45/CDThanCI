"""Input drift detection that respects seasons.

Air quality inputs change with the seasons, so comparing this fortnight with
"all training data" would raise an alarm every few weeks. Three measures keep
the signal meaningful:

1. Seasonal reference. The recent window is compared with training data from
   the same time of year (+/- ``season_half_width_days``) when the model has
   seen that season; otherwise with all training data, where a large shift
   correctly says "the model has never seen conditions like these".
2. Calibrated thresholds. At training time the same test is replayed over the
   training history, and each input's alarm threshold is set above the drift
   that ordinary weather variability produced there.
   An input also counts as drifted when a quarter of its last three days of
   readings lie outside the range of *all* training data: the model is being
   asked to extrapolate, and that shows within days, not weeks.
3. Importance weighting. The drift score is the share of the model's reliance
   (permutation importance) that sits on drifted inputs, so a shift in an input
   the model barely uses does not raise an alert on its own.

The statistic is the Population Stability Index (PSI) on quantile bins.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import MonitorConfig


def psi(reference, current, bins: int = 10) -> float:
    """Population Stability Index between two samples (NaNs ignored)."""
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    ref, cur = ref[~np.isnan(ref)], cur[~np.isnan(cur)]
    if len(ref) < bins * 5 or len(cur) < bins * 2:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)[1:-1]))
    r = np.bincount(np.searchsorted(edges, ref, side="right"), minlength=len(edges) + 1) / len(ref)
    c = np.bincount(np.searchsorted(edges, cur, side="right"), minlength=len(edges) + 1) / len(cur)
    eps = 1e-3
    r, c = np.clip(r, eps, None), np.clip(c, eps, None)
    return float(np.sum((c - r) * np.log(c / r)))


def reference_slice(ref: pd.DataFrame, center: pd.Timestamp, cfg: MonitorConfig,
                    exclude: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> tuple[pd.DataFrame, str]:
    """Reference rows for a window centred on ``center``.

    Returns the same-season slice of ``ref`` when it holds at least
    ``min_reference_rows`` rows, else all of ``ref``; and which one was used.
    ``exclude`` removes a time span (the window under test, during calibration).
    """
    pool = ref
    if exclude is not None:
        pool = ref[(ref.index < exclude[0]) | (ref.index >= exclude[1])]
    doy = pool.index.dayofyear.to_numpy()
    dist = np.abs(doy - center.dayofyear)
    dist = np.minimum(dist, 365 - dist)
    seasonal = pool[dist <= cfg.season_half_width_days]
    if len(seasonal) >= cfg.min_reference_rows:
        return seasonal, "seasonal"
    return pool, "all"


def calibrate_thresholds(ref: pd.DataFrame, columns: list[str], cfg: MonitorConfig) -> dict[str, float]:
    """Per-input PSI alarm thresholds learned from the training history.

    Slides a monitoring-sized window through ``ref``, scores each against its
    reference (built without the window itself), and sets the threshold to
    ``calibration_margin`` x the ``calibration_quantile`` of those scores, never
    below ``psi_floor``.
    """
    window = pd.Timedelta(days=cfg.drift_window_days)
    scores: dict[str, list[float]] = {c: [] for c in columns}
    start = ref.index[0]
    while start + window <= ref.index[-1]:
        end = start + window
        current = ref[(ref.index >= start) & (ref.index < end)]
        margin = pd.Timedelta(days=7)
        reference, _ = reference_slice(ref, start + window / 2, cfg, exclude=(start - margin, end + margin))
        if len(reference) >= cfg.min_reference_rows:
            for col in columns:
                value = psi(reference[col], current[col], cfg.psi_bins)
                if not np.isnan(value):
                    scores[col].append(value)
        start += pd.Timedelta(days=cfg.calibration_step_days)
    thresholds = {}
    for col in columns:
        if len(scores[col]) >= 8:
            learned = cfg.calibration_margin * float(np.quantile(scores[col], cfg.calibration_quantile))
            thresholds[col] = round(max(cfg.psi_floor, learned), 4)
        else:
            thresholds[col] = cfg.psi_floor
    return thresholds


def drift_report(ref: pd.DataFrame, current: pd.DataFrame, thresholds: dict[str, float],
                 importance: dict[str, float], cfg: MonitorConfig) -> dict[str, Any]:
    """Compare the recent window with its reference.

    ``score`` is the importance-weighted share of monitored inputs whose PSI
    exceeds its threshold (0 = nothing drifted, 1 = everything the model relies on).
    """
    center = current.index[0] + (current.index[-1] - current.index[0]) / 2
    reference, mode = reference_slice(ref, center, cfg)
    columns = [c for c in thresholds if c in current.columns and c in reference.columns]
    weights = {c: max(importance.get(c, 0.0), 0.0) for c in columns}
    total = sum(weights.values())
    if total <= 0:
        weights, total = {c: 1.0 for c in columns}, float(len(columns))
    detail, score = {}, 0.0
    recent = current[current.index > current.index[-1] - pd.Timedelta(days=cfg.novelty_window_days)]
    q = cfg.envelope_quantile
    for col in columns:
        value = psi(reference[col], current[col], cfg.psi_bins)
        if np.isnan(value):
            continue
        # novelty: recent readings outside anything the model was trained on (all seasons)
        lo, hi = ref[col].quantile(q), ref[col].quantile(1 - q)
        vals = recent[col].dropna()
        novel = float(((vals < lo) | (vals > hi)).mean()) if len(vals) >= 12 else 0.0
        drifted = value > thresholds[col] or novel >= cfg.novelty_share
        detail[col] = {"psi": round(value, 4), "threshold": thresholds[col],
                       "ratio": round(value / thresholds[col], 3), "novel": round(novel, 3),
                       "drifted": bool(drifted), "weight": round(weights[col] / total, 4)}
        if drifted:
            score += weights[col] / total
    drifted_cols = sorted((c for c, d in detail.items() if d["drifted"]),
                          key=lambda c: -max(detail[c]["ratio"], detail[c]["novel"] / cfg.novelty_share))
    level = "alert" if score >= cfg.drift_alert_score else "warn" if score >= cfg.drift_warn_score else "ok"
    return {"level": level, "score": round(score, 4), "reference": mode,
            "reference_rows": int(len(reference)), "window_rows": int(len(current)),
            "drifted": drifted_cols, "columns": detail}
