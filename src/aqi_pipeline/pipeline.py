"""The pipeline: one automated cycle from new data to (possibly) a new model.

    ingest -> quality gate -> score matured forecasts -> forecast
           -> monitor (quality, drift, accuracy) -> decide
           -> retrain and gate (when decided) -> probation / rollback -> log

``run_cycle(now)`` is idempotent per hour of data and safe to call on any
schedule (hourly cron, daily batch, a workflow engine). All state lives in the
SQLite store and the model registry, so a stopped pipeline resumes where it left off.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .aqi import compute_aqi
from .config import Config
from .drift import calibrate_thresholds, drift_report
from .features import HISTORY_HOURS, Dataset, build_dataset
from .modeling import ForecastModel, group_importance, select_and_train, training_rows
from .performance import cusum_update, daily_ratio, performance_report, safe_mode_update, valid_rows
from .policy import Decision, Signals, decide
from .registry import ModelRegistry
from .retrain import RetrainOutcome, run_retraining
from .schema import COLUMN_BIT, CORE_SENSORS, OBS_COLUMNS, to_utc_naive
from .sources import Source
from .store import Store, _ts
from .validation import validate_batch

MONITORED = OBS_COLUMNS + ["aqi"]


class Pipeline:
    def __init__(self, cfg: Config, source: Source | None = None):
        self.cfg = cfg
        self.source = source
        self.store = Store(cfg.db_path)
        self.registry = ModelRegistry(cfg.registry_dir)
        self._reference_cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def horizon(self) -> int:
        return self.cfg.data.horizon_hours

    def _dataset(self, start=None, end=None) -> Dataset:
        obs = self.store.load_observations(start, end, clean=True)
        return build_dataset(obs[OBS_COLUMNS], self.horizon, self.cfg.data.min_label_hours,
                             self.cfg.model.use_day_of_year)

    def _reference(self, version: str) -> pd.DataFrame:
        if version not in self._reference_cache:
            self._reference_cache = {version: self.registry.reference(version)}
        return self._reference_cache[version]

    def _state(self, key: str, default: Any = None) -> Any:
        return self.store.get_state(key, default)

    # ------------------------------------------------------------------ ingest
    def ingest(self, now: pd.Timestamp) -> dict[str, Any]:
        """Pull new rows from the source, run the quality gate, store them."""
        now = to_utc_naive(now)
        if self.source is None:
            return {"rows": 0, "status": "no_data", "issues": ["no source configured"]}
        last = self.store.last_observation_time()
        batch = self.source.fetch(last, now)
        if batch.empty:
            return {"rows": 0, "status": "no_data", "issues": ["source returned no new rows"]}
        context = None
        if last is not None:
            context = self.store.load_observations(last - pd.Timedelta(hours=24), last, clean=False)
        validated, report = validate_batch(batch, self.cfg.data, context)
        self.store.upsert_observations(validated)
        if report.status != "pass":
            self.store.log_event(now, "data_quality", "warning" if report.status == "warn" else "error",
                                 "; ".join(report.issues) or report.status, report.to_dict())
        return report.to_dict()

    def _quality_bad_fraction(self, now: pd.Timestamp) -> float:
        now = to_utc_naive(now)
        days = self.cfg.retrain.quality_window_days
        raw = self.store.load_observations(now - pd.Timedelta(days=days), now, clean=False)
        if raw.empty:
            return 1.0
        mask = raw["qc_mask"].to_numpy(dtype=np.int64)
        worst = 0.0
        for col in CORE_SENSORS:
            bad = raw[col].isna().to_numpy() | ((mask & (1 << COLUMN_BIT[col])) > 0)
            worst = max(worst, float(bad.mean()))
        return worst

    # ------------------------------------------------------------------ training
    def bootstrap(self, until: pd.Timestamp) -> str:
        """Load history up to ``until``, train the first model and make it champion."""
        until = to_utc_naive(until)
        report = self.ingest(until)
        ds = self._dataset(end=until)
        model, info = select_and_train(ds, self.cfg.model, self.horizon)
        rows = training_rows(ds, self.cfg.model.train_stride_hours)
        meta = self._model_meta(model, ds, rows, until, trigger="initial", strategy="full_history",
                                validation=info["validation"], extra={"candidates": info["candidates"]})
        version = self._register(model, meta, until)
        self.registry.promote(version, until, "initial model")
        self.store.set_state("last_attempt", str(until))
        self.store.set_state("backoff", 1.0)
        self.store.log_event(until, "bootstrap", "info",
                             f"trained {version} ({info['selected']}) on {len(rows)} rows; "
                             f"backtest MAE {info['validation']['mae']:.1f}, "
                             f"{info['validation']['rel_mae']:.2f} x persistence",
                             {"candidates": info["candidates"], "quality": report})
        return version

    def _model_meta(self, model: ForecastModel, ds: Dataset, rows: pd.Index, now: pd.Timestamp,
                    trigger: str, strategy: str, validation: dict[str, float],
                    extra: dict[str, Any] | None = None, prior_ref: float | None = None) -> dict[str, Any]:
        recent = rows[rows >= rows[-1] - pd.Timedelta(days=120)]
        importance = group_importance(model, ds, recent, seed=self.cfg.model.random_state)
        # Expected error relative to persistence. A challenger picked on a short holdout
        # looks better than it is, so the expectation may improve on the previous one by
        # at most 20% until a week of live forecasts (probation) has been scored.
        ref_rel = float(validation["rel_mae"])
        if prior_ref is not None:
            ref_rel = max(ref_rel, 0.8 * prior_ref)
        ref_rel = float(np.clip(ref_rel, 0.3, 1.0))   # never expect worse than persistence
        return {
            "kind": model.kind, "params": model.params, "target_mode": model.target_mode,
            "strategy": strategy, "trigger": trigger, "trained_at": str(now),
            "train_start": str(rows[0]), "train_end": str(rows[-1]), "n_train_rows": int(len(rows)),
            "validation": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in validation.items()},
            "ref_rel_mae": round(ref_rel, 4), "importance": importance, **(extra or {}),
        }

    def _register(self, model: ForecastModel, meta: dict[str, Any], now: pd.Timestamp) -> str:
        """Store the model with its drift reference and calibrated drift thresholds."""
        obs = self.store.load_observations(meta["train_start"], now, clean=True)[OBS_COLUMNS]
        reference = obs.copy()
        reference["aqi"] = compute_aqi(obs)["aqi"]
        meta["drift_thresholds"] = calibrate_thresholds(reference, MONITORED, self.cfg.monitor)
        return self.registry.register(model, meta, reference, now)

    # ------------------------------------------------------------------ forecasts
    def score_matured(self, now: pd.Timestamp) -> int:
        """Attach the true AQI to forecasts whose target time has passed."""
        now = to_utc_naive(now)
        targets = self.store.unscored_targets(now)
        if not targets:
            return 0
        first = pd.Timestamp(min(targets))
        ds = self._dataset(first - pd.Timedelta(hours=48), now)
        idx = pd.DatetimeIndex(pd.to_datetime(targets))
        idx = idx[idx.isin(ds.aqi_now.index)]
        labels = pd.DataFrame({"y_true": ds.aqi_now.reindex(idx),
                               "label_ok": ds.aqi_ok.reindex(idx, fill_value=False).astype(int)}, index=idx)
        return self.store.score_predictions(labels)

    def predict(self, now: pd.Timestamp) -> int:
        """Forecast for every hour of data not yet forecast, with champion and shadow."""
        now = to_utc_naive(now)
        last_obs = self.store.last_observation_time()
        if last_obs is None or self.registry.champion is None:
            return 0
        written = 0
        for role, version in (("champion", self.registry.champion), ("shadow", self.registry.shadow)):
            if not version:
                continue
            # the serving stream is continuous across model versions; a shadow keeps its own
            last_pred = (self.store.last_prediction_time() if role == "champion"
                         else self.store.last_prediction_time(version))
            if role == "shadow" and last_pred is None:
                last_pred = to_utc_naive(self._state("probation_start", str(last_obs)))
            start = last_pred if last_pred is not None else last_obs - pd.Timedelta(hours=24)
            if start >= last_obs:
                continue
            ds = self._dataset(start - pd.Timedelta(hours=HISTORY_HOURS), last_obs)
            issue = ds.X.index[(ds.X.index > start) & (ds.X.index <= last_obs)]
            if len(issue) == 0:
                continue
            naive = ds.aqi_now.ffill(limit=24).loc[issue]
            model = self.registry.load(version)
            pred = np.round(model.predict(ds.X.loc[issue], naive), 2)
            safe = role == "champion" and bool(self._state("safe_mode", False))
            frame = pd.DataFrame({
                "made_at": issue, "target_time": issue + pd.Timedelta(hours=self.horizon),
                "model_version": version, "role": role,
                "y_pred": naive.to_numpy() if safe else pred,      # what is served
                "y_model": pred,                                   # what the model said
                "y_naive": naive.to_numpy(), "served": "persistence" if safe else "model",
                "input_ok": ds.aqi_ok.loc[issue].astype(int).to_numpy()})
            written += self.store.insert_predictions(frame)
        return written

    # ------------------------------------------------------------------ monitoring
    def monitor(self, now: pd.Timestamp, advance_state: bool = True) -> dict[str, Any]:
        now = to_utc_naive(now)
        mcfg = self.cfg.monitor
        champion = self.registry.champion
        if not champion:
            raise RuntimeError("no champion model; run bootstrap first")
        meta = self.registry.meta(champion)

        obs = self.store.load_observations(now - pd.Timedelta(days=mcfg.drift_window_days + 2), now, clean=True)
        window = obs[OBS_COLUMNS].copy()
        window["aqi"] = compute_aqi(window)["aqi"]
        window = window[window.index > now - pd.Timedelta(days=mcfg.drift_window_days)]
        drift = drift_report(self._reference(champion), window, meta["drift_thresholds"],
                             meta["importance"], mcfg)

        preds = self.store.load_predictions(now - pd.Timedelta(days=mcfg.perf_window_days), now,
                                            model_version=champion)
        preds = preds[preds["role"] == "champion"]
        perf = performance_report(preds, meta["ref_rel_mae"], mcfg, mcfg.perf_window_days * 24)

        # floor on the expected daily error, so a calm day cannot blow the ratio up
        floor = max(0.25 * float(meta["validation"].get("mae", 10.0)), 1.0)
        ratio = daily_ratio(preds, meta["ref_rel_mae"], now, floor)
        was_safe = bool(self._state("safe_mode", False))
        if advance_state:
            cusum = cusum_update(self._state("cusum"), ratio, mcfg)
            self.store.set_state("cusum", cusum)

            safe, safe_info = safe_mode_update(was_safe, preds, now, mcfg)
            if safe != was_safe:
                self.store.set_state("safe_mode", safe)
                if safe:
                    self.store.log_event(now, "safe_mode_on", "warning",
                                         f"serving persistence: {champion} MAE {safe_info['model_mae']} vs "
                                         f"{safe_info['persistence_mae']} for persistence over the last "
                                         f"{mcfg.safe_window_days} days", safe_info)
                else:
                    self.store.log_event(now, "safe_mode_off", "info",
                                         f"serving {champion} again: MAE {safe_info['model_mae']} vs "
                                         f"{safe_info['persistence_mae']} for persistence", safe_info)

            perf_streak = self._state("perf_streak", 0) + 1 if perf["level"] in ("warn", "critical") else 0
            drift_streak = self._state("drift_streak", 0) + 1 if drift["level"] == "alert" else 0
            self.store.set_state("perf_streak", perf_streak)
            self.store.set_state("drift_streak", drift_streak)
        else:
            cusum = self._state("cusum") or {"g": 0.0, "alarm": False}
            safe = was_safe
            perf_streak = int(self._state("perf_streak", 0))
            drift_streak = int(self._state("drift_streak", 0))
        return {"drift": drift, "performance": perf, "cusum": cusum, "daily_ratio": ratio,
                "safe_mode": safe, "perf_streak": perf_streak, "drift_streak": drift_streak,
                "quality_bad_fraction": round(self._quality_bad_fraction(now), 4)}

    def _signals(self, now: pd.Timestamp, mon: dict[str, Any]) -> Signals:
        rcfg = self.cfg.retrain
        meta = self.registry.meta(self.registry.champion)
        trained_at = pd.Timestamp(meta["trained_at"])
        last_attempt = pd.Timestamp(self._state("last_attempt", str(trained_at)))
        age = (now - trained_at) / pd.Timedelta(days=1)
        return Signals(
            quality_bad_fraction=mon["quality_bad_fraction"],
            drift_level=mon["drift"]["level"], drift_streak=mon["drift_streak"],
            perf_level=mon["performance"]["level"], perf_streak=mon["perf_streak"],
            cusum_alarm=mon["cusum"]["alarm"],
            days_since_attempt=(now - last_attempt) / pd.Timedelta(days=1),
            cooldown_days=rcfg.cooldown_days * float(self._state("backoff", 1.0)),
            base_cooldown_days=rcfg.cooldown_days,
            model_age_days=age,
            age_limit_days=rcfg.max_age_days,
            in_probation=self.registry.shadow is not None,
            labelled_days_since_training=age - self.horizon / 24.0,
        )

    # ------------------------------------------------------------------ retraining
    def retrain(self, now: pd.Timestamp, decision: Decision) -> dict[str, Any]:
        now = to_utc_naive(now)
        rcfg = self.cfg.retrain
        champion_version = self.registry.champion
        champion_meta = self.registry.meta(champion_version)
        ds = self._dataset(end=now)
        tune = bool(rcfg.tune_on_failure and self._state("tune_next", False))
        eval_days = rcfg.urgent_eval_days if decision.urgency == "high" else rcfg.eval_days
        outcome: RetrainOutcome = run_retraining(ds, now, self.registry.load(champion_version),
                                                 self.cfg.model, rcfg, self.horizon, tune=tune,
                                                 eval_days=eval_days)
        self.store.set_state("last_attempt", str(now))
        summary = {"trigger": decision.trigger, "urgency": decision.urgency, "tuned": tune,
                   "replaced": champion_version, **outcome.summary()}

        if outcome.promoted:
            validation = {"mae": outcome.best["mae"], "rel_mae": outcome.best["rel_mae"],
                          "category_accuracy": outcome.best["category_accuracy"],
                          "holdout_hours": outcome.gate["holdout_hours"]}
            meta = self._model_meta(outcome.model, ds, outcome.train_rows, now, decision.trigger,
                                    outcome.best["strategy"], validation,
                                    extra={"gate": outcome.gate, "replaced": champion_version},
                                    prior_ref=champion_meta["ref_rel_mae"])
            version = self._register(outcome.model, meta, now)
            self.registry.promote(version, now, outcome.reason)
            # a challenger that did not beat persistence on the holdout starts in safe mode
            safe = bool(outcome.best["rel_mae"] is not None and outcome.best["rel_mae"] > 1.0)
            for key, value in (("backoff", 1.0), ("tune_next", False), ("cusum", None),
                               ("perf_streak", 0), ("drift_streak", 0),
                               ("probation_start", str(now)), ("safe_mode", safe)):
                self.store.set_state(key, value)
            summary["version"] = version
            self.store.log_event(now, "promotion", "info",
                                 f"{version} replaces {champion_version} ({decision.trigger}): {outcome.reason}",
                                 summary)
        else:
            backoff = min(float(self._state("backoff", 1.0)) * 2.0, rcfg.max_backoff)
            self.store.set_state("backoff", backoff)
            self.store.set_state("tune_next", True)
            self.store.set_state("cusum", None)
            self.store.log_event(now, "retrain_rejected", "info",
                                 f"kept {champion_version} ({decision.trigger}): {outcome.reason}", summary)
        return summary

    def probation(self, now: pd.Timestamp) -> dict[str, Any] | None:
        """After the probation period, keep the new champion or roll back to the shadow."""
        now = to_utc_naive(now)
        shadow = self.registry.shadow
        if not shadow:
            return None
        rcfg = self.cfg.retrain
        start = to_utc_naive(self._state("probation_start", str(now)))
        if (now - start) / pd.Timedelta(days=1) < rcfg.probation_days:
            return None
        champion = self.registry.champion
        preds = self.store.load_predictions(start, now, by="made_at")
        good = valid_rows(preds)
        c = good[good["model_version"] == champion].set_index("made_at")
        s = good[good["model_version"] == shadow].set_index("made_at")
        common = c.index.intersection(s.index)
        if len(common) < 48:
            if (now - start) / pd.Timedelta(days=1) < 2 * rcfg.probation_days:
                return None                     # not enough matured forecasts yet; keep waiting
            self.registry.retire_shadow(now, "probation ended without enough comparable forecasts")
            return {"action": "retire_shadow", "n": int(len(common))}
        mae_c = float((c.loc[common, "y_model"] - c.loc[common, "y_true"]).abs().mean())
        mae_s = float((s.loc[common, "y_model"] - s.loc[common, "y_true"]).abs().mean())
        result = {"champion": champion, "shadow": shadow, "n": int(len(common)),
                  "champion_mae": round(mae_c, 3), "shadow_mae": round(mae_s, 3)}
        if mae_c > mae_s * (1 + rcfg.rollback_tolerance):
            self.registry.rollback(now, f"live MAE {mae_c:.1f} vs {mae_s:.1f} for the previous model")
            for key, value in (("cusum", None), ("perf_streak", 0), ("tune_next", True), ("safe_mode", False)):
                self.store.set_state(key, value)
            self.store.set_state("backoff", min(float(self._state("backoff", 1.0)) * 2.0, rcfg.max_backoff))
            self.store.log_event(now, "rollback", "warning",
                                 f"rolled back {champion} -> {shadow}: live MAE {mae_c:.1f} vs {mae_s:.1f} "
                                 f"over {len(common)} forecasts", result)
            return {"action": "rollback", **result}
        self.registry.retire_shadow(now, f"probation passed: live MAE {mae_c:.1f} vs {mae_s:.1f}")
        # the first unbiased look at the new champion: recalibrate what to expect from it
        naive = float((c.loc[common, "y_naive"] - c.loc[common, "y_true"]).abs().mean())
        if naive > 0:
            meta = self.registry.meta(champion)
            ref = float(np.clip(0.5 * meta["ref_rel_mae"] + 0.5 * mae_c / naive, 0.3, 1.0))
            self.registry.update_meta(champion, {"ref_rel_mae": round(ref, 4),
                                                 "live_rel_mae_probation": round(mae_c / naive, 4)})
            result["ref_rel_mae"] = round(ref, 4)
        self.store.log_event(now, "probation_passed", "info",
                             f"{champion} confirmed: live MAE {mae_c:.1f} vs {mae_s:.1f} for {shadow} "
                             f"over {len(common)} forecasts", result)
        return {"action": "confirm", **result}

    # ------------------------------------------------------------------ the cycle
    def run_cycle(self, now) -> dict[str, Any]:
        now = to_utc_naive(now)
        if self.registry.champion is None:
            raise RuntimeError("no champion model; run bootstrap first")
        record: dict[str, Any] = {"quality": self.ingest(now)}
        record["scored"] = self.score_matured(now)
        record["forecasts"] = self.predict(now)
        new_activity = (record["quality"].get("rows", 0) > 0) or (record["scored"] > 0) or (record["forecasts"] > 0)
        same_hour = self._state("last_cycle_time") == _ts(now)
        mon = self.monitor(now, advance_state=not (same_hour and not new_activity))
        record.update(mon)
        decision = decide(self._signals(now, mon), self.cfg.retrain)
        record["decision"] = decision.to_dict()
        record["champion"] = self.registry.champion
        if decision.action == "retrain":
            record["retrain"] = self.retrain(now, decision)
            if record["retrain"]["promoted"]:
                self.predict(now)               # the shadow starts predicting alongside
        elif decision.action == "hold":
            if self._state("last_action") != "hold":
                self.store.log_event(now, "hold", "warning", decision.reason, decision.signals)
        probation = self.probation(now)
        if probation:
            record["probation"] = probation
        self.store.set_state("last_action", decision.action)
        self.store.set_state("last_cycle_time", _ts(now))
        record["champion_after"] = self.registry.champion
        record["shadow"] = self.registry.shadow
        self.store.log_cycle(now, record)
        return record
