"""Typed configuration with defaults; any value can be overridden from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DataConfig:
    horizon_hours: int = 24             # forecast lead time
    stuck_hours: int = 6                # identical consecutive readings that mean "stuck"
    warn_bad_fraction: float = 0.05     # batch status thresholds on core sensors
    fail_bad_fraction: float = 0.25
    min_label_hours: int = 16           # valid PM hours needed in a 24-hour label window


@dataclass
class ModelConfig:
    candidates: list[str] = field(default_factory=lambda: ["ridge", "hgb"])
    target_mode: str = "logratio"       # predict log(AQI tomorrow / AQI now); also "delta" or "level"
    use_day_of_year: bool = False       # see features.build_dataset
    cv_folds: int = 3                   # expanding-window backtest
    cv_test_days: int = 30
    train_stride_hours: int = 2         # use every n-th hourly row for training
    hgb_params: dict[str, Any] = field(default_factory=lambda: {
        "learning_rate": 0.06, "max_iter": 200, "max_leaf_nodes": 31,
        "min_samples_leaf": 40, "l2_regularization": 1.0})
    ridge_params: dict[str, Any] = field(default_factory=lambda: {"alpha": 10.0})
    random_state: int = 0


@dataclass
class MonitorConfig:
    # input drift (no labels needed)
    drift_window_days: int = 14
    season_half_width_days: int = 30    # reference = same time of year, +/- this many days
    min_reference_rows: int = 500
    psi_bins: int = 10
    psi_floor: float = 0.25             # never call drift below this PSI
    calibration_quantile: float = 0.95  # threshold = margin x this quantile of historical PSI
    calibration_margin: float = 1.25
    calibration_step_days: int = 7
    drift_warn_score: float = 0.15      # importance-weighted share of drifted inputs
    drift_alert_score: float = 0.35
    envelope_quantile: float = 0.005    # training range = this quantile to (1 - this quantile)
    novelty_window_days: int = 3
    novelty_share: float = 0.25         # drifted if this share of recent readings leaves the training range
    # performance (labels arrive horizon_hours late)
    perf_window_days: int = 7
    perf_warn_ratio: float = 1.25       # error vs the error expected at validation time
    perf_critical_ratio: float = 1.5
    perf_min_gap: float = 3.0           # and at least this many AQI points worse than expected
    min_coverage: float = 0.5           # share of the window with clean inputs and labels
    cusum_slack: float = 0.30           # CUSUM on the daily error ratio: ignored excess per day ...
    cusum_alarm: float = 4.0            # ... and the accumulated excess that raises the alarm
    # safe mode: serve the persistence forecast while the model is doing worse than it
    safe_window_days: int = 3
    safe_enter_ratio: float = 1.25      # model MAE / persistence MAE over the window
    safe_exit_ratio: float = 1.00


@dataclass
class RetrainConfig:
    strategies: list[str] = field(default_factory=lambda: ["full_history", "recent_window", "recency_weighted"])
    recent_window_days: int = 150
    half_life_days: float = 45.0
    eval_days: int = 10                 # most recent days held out to compare challenger and champion
    urgent_eval_days: int = 5           # shorter holdout when accuracy has collapsed (more new data to learn from)
    min_improvement: float = 0.05       # challenger must cut MAE by at least this share ...
    confidence: float = 0.95            # ... with at least this bootstrap probability of being better
    bootstrap_samples: int = 500
    cooldown_days: float = 5.0          # minimum gap between retraining attempts
    max_backoff: float = 4.0            # cooldown multiplier ceiling after failed attempts
    max_age_days: float = 60.0          # scheduled refresh when nothing else triggers
    perf_persist_cycles: int = 2
    drift_persist_cycles: int = 5
    quality_window_days: int = 3
    quality_hold_fraction: float = 0.15
    probation_days: float = 7.0         # new champion is shadowed by the previous one
    rollback_tolerance: float = 0.05    # roll back if the new champion is this much worse
    tune_on_failure: bool = True        # add a hyper-parameter search after a failed attempt
    tune_iterations: int = 4


@dataclass
class Config:
    location: str = "SIM-01 (synthetic)"
    run_dir: str = "runs/default"
    data: DataConfig = field(default_factory=DataConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    retrain: RetrainConfig = field(default_factory=RetrainConfig)
    source: dict[str, Any] = field(default_factory=lambda: {"type": "synthetic"})
    simulation: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return Path(self.run_dir) / "pipeline.db"

    @property
    def registry_dir(self) -> Path:
        return Path(self.run_dir) / "registry"

    @property
    def report_dir(self) -> Path:
        return Path(self.run_dir) / "reports"


def _update(obj: Any, values: dict[str, Any]) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise KeyError(f"unknown config key: {key!r} in {type(obj).__name__}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if value is None:
                continue
            if not isinstance(value, dict):
                raise TypeError(f"expected mapping for config section {key!r}, got {type(value).__name__}")
            _update(current, value)
        elif isinstance(current, dict):
            if value is None:
                setattr(obj, key, {})
            elif not isinstance(value, dict):
                raise TypeError(f"expected mapping for config section {key!r}, got {type(value).__name__}")
            else:
                setattr(obj, key, value)
        else:
            if key == "run_dir" and value is not None:
                value = str(value)
            setattr(obj, key, value)


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Defaults, then the YAML file if given, then explicit overrides."""
    cfg = Config()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(f"config file {path} must contain a YAML mapping")
            _update(cfg, loaded)
    if overrides:
        _update(cfg, overrides)
    return cfg
