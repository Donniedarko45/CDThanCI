"""Data quality gate: every batch passes through here before it is stored.

Checks, per batch:

* schema - required columns present, timestamps parseable
* duplicates - repeated timestamps are dropped (first kept)
* gaps - missing hours are inserted as empty rows so the series stays hourly
* range - physically impossible readings (BOUNDS)
* stuck - a continuous sensor repeating one value for ``stuck_hours`` or more

Raw readings are never altered. Faulty cells are recorded in a per-row bitmask
(``qc_mask``, one bit per column) and blanked only when data is loaded for
modelling, so the original values stay available for audit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .config import DataConfig
from .schema import BOUNDS, COLUMN_BIT, CORE_SENSORS, OBS_COLUMNS, REQUIRED_COLUMNS, STUCK_COLUMNS, to_utc_naive_dt


@dataclass
class QualityReport:
    rows: int = 0
    duplicates: int = 0
    gap_rows: int = 0
    range_cells: int = 0
    stuck_cells: int = 0
    missing_cells: int = 0
    bad_fraction: dict[str, float] = field(default_factory=dict)  # per column: faulty or missing
    core_bad_fraction: float = 0.0
    status: str = "pass"  # pass | warn | fail
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _stuck_mask(values: pd.Series, min_run: int) -> np.ndarray:
    """True where a value belongs to a run of >= min_run identical readings."""
    v = values.to_numpy(dtype=float)
    if len(v) == 0:
        return np.zeros(0, dtype=bool)
    valid = ~np.isnan(v)
    change = np.ones(len(v), dtype=bool)
    change[1:] = (v[1:] != v[:-1]) | ~valid[1:] | ~valid[:-1]
    run_id = np.cumsum(change)
    run_len = np.bincount(run_id)[run_id]
    return valid & (run_len >= min_run)


def validate_batch(
    batch: pd.DataFrame, cfg: DataConfig, context: pd.DataFrame | None = None
) -> tuple[pd.DataFrame, QualityReport]:
    """Check one batch of raw observations.

    ``batch`` has a ``timestamp`` column (or DatetimeIndex) plus observation columns.
    ``context`` is the tail of already-stored raw rows; it lets a stuck sensor be
    recognised when the run started in an earlier batch.

    Returns the batch on a complete hourly index, with ``qc_mask`` and ``qc_notes``
    columns added, and the quality report.
    """
    report = QualityReport()
    df = batch.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = to_utc_naive_dt(df["timestamp"])
        df = df.set_index("timestamp")
    else:
        df.index = to_utc_naive_dt(df.index)
    df.index.name = "timestamp"
    if df.index.isna().any():
        raise ValueError("batch contains missing or unparseable timestamps")
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"batch is missing required columns: {missing_cols}")
    for col in OBS_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df = df[OBS_COLUMNS].apply(pd.to_numeric, errors="coerce").sort_index()

    dup = df.index.duplicated(keep="first")
    report.duplicates = int(dup.sum())
    df = df[~dup]
    if df.empty:
        report.status = "fail"
        report.issues.append("empty batch")
        df["qc_mask"] = pd.Series(dtype="int64")
        df["qc_notes"] = pd.Series(dtype="object")
        return df, report

    ctx_df = None
    if context is not None and len(context):
        ctx_df = context[[c for c in OBS_COLUMNS if c in context.columns]].copy()
        ctx_df.index = to_utc_naive_dt(ctx_df.index)
        ctx_df = ctx_df.sort_index()

    start = df.index[0]
    if ctx_df is not None and len(ctx_df):
        start = min(start, ctx_df.index[-1] + pd.Timedelta(hours=1))
    full = pd.date_range(start, df.index[-1], freq="h")
    gap = ~full.isin(df.index)
    report.gap_rows = int(gap.sum())
    df = df.reindex(full)
    df.index.name = "timestamp"
    report.rows = len(df)

    mask = np.zeros(len(df), dtype=np.int64)
    notes = [[] for _ in range(len(df))]

    def flag(col: str, where: np.ndarray, check: str) -> int:
        if not where.any():
            return 0
        mask[where] |= 1 << COLUMN_BIT[col]
        for i in np.flatnonzero(where):
            notes[i].append(f"{check}:{col}")
        return int(where.sum())

    for col, (lo, hi) in BOUNDS.items():
        v = df[col].to_numpy(dtype=float)
        bad = ~np.isnan(v) & ((v < lo) | (v > hi))
        report.range_cells += flag(col, bad, "range")

    joined = df
    if ctx_df is not None and len(ctx_df):
        ctx_tail = ctx_df.tail(cfg.stuck_hours * 2)
        joined_idx = ctx_tail.index.union(df.index).sort_values()
        joined = df[STUCK_COLUMNS].reindex(joined_idx)
        for col in STUCK_COLUMNS:
            if col in ctx_tail.columns:
                missing_in_df = ~joined_idx.isin(df.index)
                ctx_vals = pd.to_numeric(ctx_tail[col], errors="coerce").reindex(joined_idx)
                joined.loc[missing_in_df, col] = ctx_vals.loc[missing_in_df]
    for col in STUCK_COLUMNS:
        stuck = (
            pd.Series(_stuck_mask(joined[col], cfg.stuck_hours), index=joined.index)
            .reindex(df.index, fill_value=False)
            .to_numpy()
        )
        report.stuck_cells += flag(col, stuck, "stuck")

    for i in np.flatnonzero(gap):
        notes[i].append("gap")

    values = df[OBS_COLUMNS].to_numpy(dtype=float)
    required_idx = [OBS_COLUMNS.index(c) for c in REQUIRED_COLUMNS]
    report.missing_cells = int(np.isnan(values[:, required_idx]).sum())
    for col in REQUIRED_COLUMNS:
        bit = 1 << COLUMN_BIT[col]
        bad = df[col].isna().to_numpy() | ((mask & bit) > 0)
        report.bad_fraction[col] = round(float(bad.mean()), 4)
    report.core_bad_fraction = max(report.bad_fraction[c] for c in CORE_SENSORS)
    worst = max(report.bad_fraction.values())

    if report.core_bad_fraction >= cfg.fail_bad_fraction:
        report.status = "fail"
    elif worst >= cfg.warn_bad_fraction or report.duplicates or report.gap_rows:
        report.status = "warn"
    if report.duplicates:
        report.issues.append(f"{report.duplicates} duplicate timestamps dropped")
    if report.gap_rows:
        report.issues.append(f"{report.gap_rows} missing hours")
    if report.range_cells:
        report.issues.append(f"{report.range_cells} out-of-range readings")
    if report.stuck_cells:
        report.issues.append(f"{report.stuck_cells} readings from a stuck sensor")
    for col, frac in report.bad_fraction.items():
        if frac >= cfg.warn_bad_fraction:
            report.issues.append(f"{col}: {frac:.0%} of readings missing or faulty")

    df["qc_mask"] = mask
    df["qc_notes"] = [",".join(x) for x in notes]
    return df, report


def apply_mask(df: pd.DataFrame) -> pd.DataFrame:
    """Blank the cells that the quality gate flagged; drops the qc columns."""
    out = df[[c for c in OBS_COLUMNS if c in df.columns]].copy()
    mask = df["qc_mask"].to_numpy(dtype=np.int64)
    if mask.any():
        for col in out.columns:
            bad = (mask & (1 << COLUMN_BIT[col])) > 0
            if bad.any():
                out.loc[bad, col] = np.nan
    return out
