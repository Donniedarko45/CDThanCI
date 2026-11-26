"""Retraining: strategy trials and the promotion gate.

When the policy asks for retraining, the pipeline does not just refit and
deploy. It trains several challengers that differ in *which data* they learn
from, scores them and the live champion on the most recent days (held out from
every challenger), and promotes the best one only if it is clearly better.

Data strategies:

* ``full_history``     - every clean row; best when the world has not changed
* ``recent_window``    - only the last ``recent_window_days``; best after an abrupt change
* ``recency_weighted`` - every row, weighted by 0.5 ** (age / half_life); a middle path

Each strategy is tried with every candidate model family. After a failed
attempt the next one also runs a small hyper-parameter search.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import ModelConfig, RetrainConfig
from .features import Dataset
from .modeling import ForecastModel, evaluate, fit_model, score, training_rows
from .schema import to_utc_naive

TUNE_SPACE = {
    "learning_rate": [0.03, 0.06, 0.1],
    "max_iter": [200, 300, 500],
    "max_leaf_nodes": [15, 31, 63],
    "min_samples_leaf": [20, 40, 80],
    "l2_regularization": [0.1, 1.0, 5.0],
}


@dataclass
class RetrainOutcome:
    promoted: bool
    reason: str
    trials: list[dict[str, Any]] = field(default_factory=list)
    champion_metrics: dict[str, float] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    best: dict[str, Any] = field(default_factory=dict)
    model: ForecastModel | None = None
    train_rows: pd.Index | None = None
    seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {"promoted": self.promoted, "reason": self.reason, "trials": self.trials,
                "champion_metrics": self.champion_metrics, "gate": self.gate,
                "best": self.best, "seconds": round(self.seconds, 2)}


def strategy_rows(ds: Dataset, strategy: str, cfg: RetrainConfig, mcfg: ModelConfig,
                  train_end: pd.Timestamp) -> tuple[pd.Index, np.ndarray | None]:
    """Training rows and sample weights for a data strategy, using labels known by ``train_end``."""
    train_end = to_utc_naive(train_end)
    rows = training_rows(ds, mcfg.train_stride_hours, end=train_end)
    if strategy == "full_history":
        return rows, None
    if strategy == "recent_window":
        return rows[rows >= train_end - pd.Timedelta(days=cfg.recent_window_days)], None
    if strategy == "recency_weighted":
        if cfg.half_life_days <= 0:
            raise ValueError("retrain.half_life_days must be positive")
        age_days = (train_end - rows) / pd.Timedelta(days=1)
        return rows, np.maximum(0.5 ** (np.asarray(age_days, dtype=float) / cfg.half_life_days), 0.02)
    raise ValueError(f"unknown strategy: {strategy}")


def paired_block_bootstrap(err_champion: pd.Series, err_challenger: pd.Series,
                           samples: int, seed: int) -> float:
    """Probability that the challenger's MAE is lower, resampling whole days.

    Hourly errors are strongly autocorrelated; resampling by day keeps that
    structure, so the probability is not inflated by treating hours as independent.
    """
    diff = (err_champion - err_challenger).dropna()
    days = diff.groupby(diff.index.floor("D")).agg(["sum", "count"])
    if len(days) < 3:
        return float("nan")
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(days), size=(samples, len(days)))
    sums, counts = days["sum"].to_numpy()[picks], days["count"].to_numpy()[picks]
    return float(((sums.sum(axis=1) / counts.sum(axis=1)) > 0).mean())


def _param_map(mcfg: ModelConfig) -> dict[str, dict[str, Any]]:
    return {"hgb": mcfg.hgb_params, "ridge": mcfg.ridge_params, "rf": {}}


def run_retraining(ds: Dataset, now: pd.Timestamp, champion: ForecastModel, mcfg: ModelConfig,
                   rcfg: RetrainConfig, horizon: int, tune: bool = False,
                   eval_days: int | None = None) -> RetrainOutcome:
    """Train challengers, compare with the champion on the latest days, apply the gate."""
    t0 = time.time()
    now = to_utc_naive(now)
    usable = training_rows(ds)
    eval_days = eval_days or rcfg.eval_days
    eval_start = now - pd.Timedelta(days=eval_days)
    holdout = usable[usable >= eval_start]
    # A training row's label must have been known before the holdout begins.
    train_end = eval_start - pd.Timedelta(hours=horizon)
    if len(holdout) < 72:
        return RetrainOutcome(False, f"only {len(holdout)} clean labelled hours in the last "
                                     f"{eval_days} days; too few to compare models",
                              seconds=time.time() - t0)

    y_hold, naive_hold = ds.y.loc[holdout], ds.aqi_now.loc[holdout]
    champ_pred = pd.Series(champion.predict(ds.X.loc[holdout], naive_hold), index=holdout)
    champ_metrics = score(y_hold, champ_pred, naive_hold)
    champ_summary = {k: (None if isinstance(v, float) and np.isnan(v) else round(v, 4))
                     for k, v in champ_metrics.items()}
    champ_err = (champ_pred - y_hold).abs()

    trials: list[dict[str, Any]] = []
    fitted: dict[int, tuple[ForecastModel, pd.Series]] = {}

    def trial(kind: str, params: dict[str, Any], strategy: str, label: str | None = None) -> None:
        rows, weights = strategy_rows(ds, strategy, rcfg, mcfg, train_end)
        if len(rows) < 24 * 14:
            return
        t = time.time()
        model = fit_model(ds, rows, kind, params, mcfg, sample_weight=weights)
        metrics, pred = evaluate(model, ds, holdout)
        fitted[len(trials)] = (model, pred)
        trials.append({"name": label or f"{kind}/{strategy}", "kind": kind, "strategy": strategy,
                       "params": dict(params), "n_train": int(len(rows)),
                       "mae": round(metrics["mae"], 3),
                       "rel_mae": None if np.isnan(metrics["rel_mae"]) else round(metrics["rel_mae"], 4),
                       "category_accuracy": round(metrics["category_accuracy"], 4),
                       "seconds": round(time.time() - t, 2)})

    params = _param_map(mcfg)
    for kind in mcfg.candidates:
        for strategy in rcfg.strategies:
            trial(kind, params[kind], strategy)
    if not trials:
        return RetrainOutcome(False, "not enough training data before the holdout window",
                              champion_metrics=champ_summary, seconds=time.time() - t0)

    if tune and "hgb" in mcfg.candidates:
        hgb_trials = [t for t in trials if t["kind"] == "hgb"]
        base_strategy = min(hgb_trials, key=lambda t: t["mae"])["strategy"] if hgb_trials else rcfg.strategies[0]
        rng = np.random.default_rng(mcfg.random_state + int(now.value % 10_000))
        for i in range(rcfg.tune_iterations):
            sampled = {k: v[int(rng.integers(len(v)))] for k, v in TUNE_SPACE.items()}
            trial("hgb", sampled, base_strategy, label=f"hgb/{base_strategy}/tuned-{i + 1}")

    best_i = min(range(len(trials)), key=lambda i: trials[i]["mae"])
    best, (best_model, best_pred) = trials[best_i], fitted[best_i]
    best_err = (best_pred - y_hold).abs()
    champ_mae = champ_metrics["mae"]
    improvement = (champ_mae - best["mae"]) / champ_mae if champ_mae > 0 else 0.0
    prob = paired_block_bootstrap(champ_err, best_err, rcfg.bootstrap_samples, mcfg.random_state)
    passed = bool(improvement >= rcfg.min_improvement and not np.isnan(prob) and prob >= rcfg.confidence)
    gate = {"improvement": round(improvement, 4),
            "prob_better": None if np.isnan(prob) else round(prob, 3),
            "min_improvement": rcfg.min_improvement, "confidence": rcfg.confidence, "passed": passed,
            "holdout_hours": int(len(holdout)), "holdout_start": str(eval_start)}

    if not passed:
        why = (f"best challenger {best['name']} cut holdout MAE by {improvement:.1%} "
               f"(P better = {prob:.2f}); needs >= {rcfg.min_improvement:.0%} with P >= {rcfg.confidence:.2f}")
        return RetrainOutcome(False, why, trials, champ_summary, gate, best, seconds=time.time() - t0)

    # The winner is refitted on everything up to now, so it also learns from the holdout days.
    rows, weights = strategy_rows(ds, best["strategy"], rcfg, mcfg, now + pd.Timedelta(hours=1))
    final = fit_model(ds, rows, best["kind"], best["params"], mcfg, sample_weight=weights)
    why = (f"{best['name']} cut holdout MAE by {improvement:.1%} "
           f"({champ_metrics['mae']:.1f} -> {best['mae']:.1f}, P better = {prob:.2f})")
    return RetrainOutcome(True, why, trials, champ_summary, gate, best, final, rows, time.time() - t0)
