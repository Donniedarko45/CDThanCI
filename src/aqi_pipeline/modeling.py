"""Forecast models, metrics, time-ordered backtesting and model selection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .aqi import category
from .config import ModelConfig
from .features import Dataset


def make_estimator(kind: str, params: dict[str, Any], random_state: int = 0):
    if kind == "ridge":
        return make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), Ridge(**params)
        )
    if kind == "hgb":
        return HistGradientBoostingRegressor(early_stopping=False, random_state=random_state, **params)
    if kind == "rf":
        defaults = {"n_estimators": 150, "min_samples_leaf": 20, "max_features": 0.4, "n_jobs": -1}
        return make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            RandomForestRegressor(random_state=random_state, **{**defaults, **params}),
        )
    raise ValueError(f"unknown model kind: {kind}")


@dataclass
class ForecastModel:
    """A fitted estimator plus what is needed to turn its output into an AQI."""

    kind: str
    params: dict[str, Any]
    target_mode: str = "logratio"
    horizon: int = 24
    feature_names: list[str] = field(default_factory=list)
    estimator: Any = None

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        aqi_now: pd.Series,
        sample_weight: np.ndarray | None = None,
        random_state: int = 0,
    ) -> "ForecastModel":
        if X.empty:
            raise ValueError("cannot fit model on empty dataset")
        self.feature_names = list(X.columns)
        target = self._to_target(y, aqi_now)
        self.estimator = make_estimator(self.kind, self.params, random_state)
        Xv, tv = X.to_numpy(), target.to_numpy()
        if sample_weight is None:
            self.estimator.fit(Xv, tv)
        elif self.kind == "ridge":
            self.estimator.fit(Xv, tv, ridge__sample_weight=sample_weight)
        else:
            # Tree models get the weights as a weighted resample of the rows: the same
            # emphasis in expectation, without the slow weighted binning.
            p = np.asarray(sample_weight, dtype=float)
            total = float(p.sum())
            if total <= 0 or np.any(p < 0) or np.any(~np.isfinite(p)):
                raise ValueError("sample_weight must be finite, non-negative, and have positive sum")
            pick = np.sort(np.random.default_rng(random_state).choice(len(tv), size=len(tv), p=p / total))
            self.estimator.fit(Xv[pick], tv[pick])
        return self

    def predict(self, X: pd.DataFrame, aqi_now: pd.Series) -> np.ndarray:
        """AQI forecasts in [0, 500]; NaN where no reference AQI exists (delta mode)."""
        if X.empty:
            return np.empty(0, dtype=float)
        raw = self.estimator.predict(X[self.feature_names].to_numpy())
        ref = aqi_now.to_numpy(dtype=float)
        if self.target_mode == "delta":
            raw = raw + ref
        elif self.target_mode == "logratio":
            raw = np.exp(np.clip(raw, -3.0, 3.0)) * (ref + 1.0) - 1.0
        return np.clip(raw, 0.0, 500.0)

    def _to_target(self, y: pd.Series, aqi_now: pd.Series) -> pd.Series:
        if self.target_mode == "delta":
            return y - aqi_now
        if self.target_mode == "logratio":
            return np.log((y + 1.0) / (aqi_now + 1.0))
        if self.target_mode == "level":
            return y
        raise ValueError(f"unknown target_mode: {self.target_mode}")


def score(y_true, y_pred, y_naive) -> dict[str, float]:
    """Accuracy of a forecast, with the persistence forecast as the yardstick."""
    y_true, y_pred, y_naive = (np.asarray(a, dtype=float) for a in (y_true, y_pred, y_naive))
    ok = ~(np.isnan(y_true) | np.isnan(y_pred) | np.isnan(y_naive))
    n = int(ok.sum())
    if n == 0:
        return {"n": 0}
    t, p, b = y_true[ok], y_pred[ok], y_naive[ok]
    err = p - t
    mae, mae_naive = float(np.abs(err).mean()), float(np.abs(b - t).mean())
    ss_tot = float(((t - t.mean()) ** 2).sum())
    cat_t, cat_p = category(t), category(p)
    return {
        "n": n,
        "mae": mae,
        "rmse": float(np.sqrt((err**2).mean())),
        "bias": float(err.mean()),
        "r2": float(1 - (err**2).sum() / ss_tot) if ss_tot > 0 else float("nan"),
        "mae_naive": mae_naive,
        "rel_mae": mae / mae_naive if mae_naive > 0 else float("nan"),  # < 1 beats persistence
        "category_accuracy": float((cat_t == cat_p).mean()),
        "category_within_one": float((np.abs(cat_t - cat_p) <= 1).mean()),
    }


def training_rows(ds: Dataset, stride: int = 1, start=None, end=None) -> pd.Index:
    """Index of rows usable for fitting, optionally thinned and time-bounded."""
    ok = ds.usable()
    idx = ds.X.index[ok.to_numpy()]
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx < pd.Timestamp(end)]
    return idx[::stride] if stride > 1 else idx


def fit_model(
    ds: Dataset,
    rows: pd.Index,
    kind: str,
    params: dict[str, Any],
    cfg: ModelConfig,
    sample_weight: np.ndarray | None = None,
) -> ForecastModel:
    model = ForecastModel(kind=kind, params=dict(params), target_mode=cfg.target_mode)
    return model.fit(
        ds.X.loc[rows], ds.y.loc[rows], ds.aqi_now.loc[rows], sample_weight=sample_weight, random_state=cfg.random_state
    )


def evaluate(model: ForecastModel, ds: Dataset, rows: pd.Index) -> tuple[dict[str, float], pd.Series]:
    pred = pd.Series(model.predict(ds.X.loc[rows], ds.aqi_now.loc[rows]), index=rows)
    return score(ds.y.loc[rows], pred, ds.aqi_now.loc[rows]), pred


def backtest(ds: Dataset, kind: str, params: dict[str, Any], cfg: ModelConfig, horizon: int = 24) -> dict[str, Any]:
    """Expanding-window backtest over the most recent ``cv_folds`` test blocks.

    Each fold trains on rows whose label was already known when the test block
    started (a gap of ``horizon`` hours), then scores the block.
    """
    usable = training_rows(ds)
    if len(usable) == 0:
        raise ValueError("not enough history for a backtest")
    end = usable[-1] + pd.Timedelta(hours=1)
    preds, fold_scores = [], []
    for k in range(cfg.cv_folds, 0, -1):
        test_start = end - pd.Timedelta(days=k * cfg.cv_test_days)
        test_end = test_start + pd.Timedelta(days=cfg.cv_test_days)
        train = training_rows(ds, cfg.train_stride_hours, end=test_start - pd.Timedelta(hours=horizon))
        test = usable[(usable >= test_start) & (usable < test_end)]
        if len(train) < 24 * 30 or len(test) == 0:
            continue
        model = fit_model(ds, train, kind, params, cfg)
        metrics, pred = evaluate(model, ds, test)
        if metrics.get("n", 0) == 0:
            continue
        fold_scores.append(metrics)
        preds.append(pred)
    if not preds:
        raise ValueError("not enough history for a backtest")
    oof = pd.concat(preds)
    overall = score(ds.y.loc[oof.index], oof, ds.aqi_now.loc[oof.index])
    return {"metrics": overall, "folds": fold_scores, "oof": oof}


def select_and_train(ds: Dataset, cfg: ModelConfig, horizon: int = 24) -> tuple[ForecastModel, dict[str, Any]]:
    """Backtest every candidate, keep the one with the lowest MAE, refit it on all rows."""
    if not cfg.candidates:
        raise ValueError("model.candidates must not be empty")
    param_map = {"hgb": cfg.hgb_params, "ridge": cfg.ridge_params, "rf": {}}
    results: dict[str, Any] = {}
    best_kind, best_mae = None, np.inf
    for kind in cfg.candidates:
        t0 = time.time()
        bt = backtest(ds, kind, param_map.get(kind, {}), cfg, horizon)
        results[kind] = {**bt["metrics"], "seconds": round(time.time() - t0, 2)}
        if bt["metrics"]["mae"] < best_mae:
            best_kind, best_mae = kind, bt["metrics"]["mae"]
    if best_kind is None:
        raise ValueError("no candidate model could be trained")
    rows = training_rows(ds, cfg.train_stride_hours)
    model = fit_model(ds, rows, best_kind, param_map.get(best_kind, {}), cfg)
    info = {
        "selected": best_kind,
        "candidates": results,
        "validation": results[best_kind],
        "n_train_rows": int(len(rows)),
        "train_start": str(rows[0]),
        "train_end": str(rows[-1]),
    }
    return model, info


def group_importance(
    model: ForecastModel, ds: Dataset, rows: pd.Index, max_rows: int = 3000, seed: int = 0
) -> dict[str, float]:
    """Permutation importance per raw input.

    All features derived from one raw input are shuffled together, and the rise
    in MAE is that input's importance. Results are normalised to sum to 1 and
    weight the drift score, so drift in an input the model barely uses counts little.
    """
    if len(rows) == 0:
        return {g: 0.0 for g, feats in ds.groups.items() if feats}
    rng = np.random.default_rng(seed)
    if len(rows) > max_rows:
        rows = rows[np.sort(rng.choice(len(rows), max_rows, replace=False))]
    X, y, ref = ds.X.loc[rows], ds.y.loc[rows].to_numpy(), ds.aqi_now.loc[rows]
    base = np.abs(model.predict(X, ref) - y).mean()
    perm = rng.permutation(len(rows))
    gains = {}
    for group, feats in ds.groups.items():
        if not feats:
            continue
        Xp = X.copy()
        Xp[feats] = X[feats].to_numpy()[perm]
        gains[group] = max(float(np.abs(model.predict(Xp, ref) - y).mean() - base), 0.0)
    total = sum(gains.values()) or 1.0
    return {g: round(v / total, 4) for g, v in gains.items()}
