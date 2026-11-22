"""The retraining decision.

Retraining is not free and not always right: retraining on faulty sensor data
teaches the model the fault, and retraining on every seasonal wobble burns
compute and risks a worse model. The policy looks at *why* things changed and
picks one of four actions each cycle:

    none     nothing to do
    watch    something moved, accuracy is holding; keep monitoring
    hold     recent data is unreliable; do not retrain until it is clean
    retrain  train challengers now

Rules are evaluated in order; the first that applies wins. Every decision
carries its reason, so the log reads as an audit trail.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import RetrainConfig


@dataclass
class Signals:
    quality_bad_fraction: float      # share of recent core-sensor readings missing or faulty
    drift_level: str                 # ok | warn | alert
    drift_streak: int                # consecutive cycles at alert
    perf_level: str                  # ok | warn | critical | unknown
    perf_streak: int                 # consecutive cycles at warn or critical
    cusum_alarm: bool                # slow, sustained decay
    days_since_attempt: float        # since the last retraining attempt (or first training)
    cooldown_days: float             # current cooldown, including back-off
    base_cooldown_days: float        # cooldown without back-off; applies when accuracy has collapsed
    model_age_days: float            # since the champion was trained
    age_limit_days: float            # scheduled-refresh age for this champion
    in_probation: bool               # a new champion is still being shadowed
    labelled_days_since_training: float


@dataclass
class Decision:
    action: str                      # none | watch | hold | retrain
    reason: str
    trigger: str = ""                # performance | performance+drift | slow_decay | drift | scheduled
    urgency: str = "normal"          # normal | high
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(s: Signals, cfg: RetrainConfig) -> Decision:
    sig = asdict(s)
    concern = (s.perf_level in ("warn", "critical") or s.drift_level in ("warn", "alert") or s.cusum_alarm)

    def out(action: str, reason: str, trigger: str = "", urgency: str = "normal") -> Decision:
        return Decision(action=action, reason=reason, trigger=trigger, urgency=urgency, signals=sig)

    # 1. Unreliable data: fix the data, not the model.
    if s.quality_bad_fraction >= cfg.quality_hold_fraction:
        return out("hold", f"{s.quality_bad_fraction:.0%} of recent core-sensor readings are missing or faulty; "
                           "retraining is on hold until the data is clean")

    # 2. A freshly promoted model is still on probation against its predecessor.
    if s.in_probation:
        return out("watch" if concern else "none", "new champion is on probation; the shadow comparison decides")

    # 3. Respect the cooldown so the pipeline cannot thrash. Failed attempts lengthen it
    #    (back-off), except when accuracy has collapsed: then only the base cooldown applies.
    urgent = s.perf_level == "critical" and s.perf_streak >= cfg.perf_persist_cycles
    cooldown = s.base_cooldown_days if urgent else s.cooldown_days
    if s.days_since_attempt < cooldown:
        wait = cooldown - s.days_since_attempt
        if concern:
            return out("watch", f"signals are raised, but the last attempt was {s.days_since_attempt:.0f} days ago; "
                                f"next attempt allowed in {wait:.0f} days")
        return out("none", "healthy")

    # 4. Need new labelled data to learn from.
    if s.labelled_days_since_training < 2:
        return out("watch" if concern else "none", "not enough new labelled data since the last training")

    # 5. Accuracy has clearly dropped.
    if urgent:
        return out("retrain", "forecast error is far above the level expected at validation",
                   "performance", "high")
    # 6. Accuracy is slipping and the inputs have drifted: the two signals agree.
    if (s.perf_level in ("warn", "critical") and s.perf_streak >= cfg.perf_persist_cycles
            and s.drift_level in ("warn", "alert")):
        return out("retrain", "forecast error is above expectation and input drift corroborates it",
                   "performance+drift")
    # 7. Slow decay that never crossed the window thresholds.
    if s.cusum_alarm:
        return out("retrain", "sustained excess error accumulated over many days", "slow_decay")
    # 8. Inputs have stayed far from the training data for a while: refresh proactively.
    if s.drift_level == "alert" and s.drift_streak >= cfg.drift_persist_cycles:
        return out("retrain", f"input drift has persisted for {s.drift_streak} cycles", "drift")
    # 9. Scheduled refresh as a backstop.
    if s.model_age_days >= s.age_limit_days and s.days_since_attempt >= s.age_limit_days:
        return out("retrain", f"champion is {s.model_age_days:.0f} days old (limit {s.age_limit_days:.0f})",
                   "scheduled")

    if concern:
        what = []
        if s.perf_level in ("warn", "critical"):
            what.append(f"accuracy {s.perf_level}")
        if s.drift_level in ("warn", "alert"):
            what.append(f"input drift {s.drift_level}")
        return out("watch", ", ".join(what) + "; not yet persistent or corroborated")
    return out("none", "healthy")
