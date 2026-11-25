"""India's National Air Quality Index (CPCB, 2014).

Each pollutant's averaged concentration is mapped to a 0-500 sub-index by linear
interpolation between health breakpoints; the AQI is the highest sub-index.
PM2.5, PM10, NO2 and SO2 use 24-hour running means (at least 16 valid hours);
CO and O3 use 8-hour running means. An AQI is reported only when at least three
sub-indices are available and one of them is PM2.5 or PM10.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

INDEX_POINTS = [0, 50, 100, 200, 300, 400, 500]
# Concentration breakpoints matching INDEX_POINTS. The last value extends the
# "Severe" band with the slope used in CPCB's calculator so that the scale tops
# out at 500.
BREAKPOINTS = {
    "pm25": [0, 30, 60, 90, 120, 250, 380],
    "pm10": [0, 50, 100, 250, 350, 430, 510],
    "no2": [0, 40, 80, 180, 280, 400, 520],
    "so2": [0, 40, 80, 380, 800, 1600, 2400],
    "co": [0, 1.0, 2.0, 10, 17, 34, 51],  # mg/m³
    "o3": [0, 50, 100, 168, 208, 748, 1288],
}
AVERAGING = {  # pollutant -> (window, minimum valid hours)
    "pm25": ("24h", 16),
    "pm10": ("24h", 16),
    "no2": ("24h", 16),
    "so2": ("24h", 16),
    "co": ("8h", 6),
    "o3": ("8h", 6),
}
CATEGORIES = ["Good", "Satisfactory", "Moderate", "Poor", "Very poor", "Severe"]
CATEGORY_EDGES = [50, 100, 200, 300, 400]


def sub_index(pollutant: str, concentration) -> np.ndarray:
    """Sub-index (0-500) for an already-averaged concentration."""
    conc = np.asarray(concentration, dtype=float)
    out = np.interp(conc, BREAKPOINTS[pollutant], INDEX_POINTS)
    return np.where(np.isnan(conc), np.nan, out)


def category(aqi) -> np.ndarray:
    """AQI category index 0..5 (Good..Severe); -1 where the AQI is missing."""
    a = np.asarray(aqi, dtype=float)
    cat = np.digitize(a, CATEGORY_EDGES, right=True)
    return np.where(np.isnan(a), -1, cat).astype(int)


def category_name(aqi: float) -> str:
    idx = int(np.asarray(category(aqi)).item())
    return CATEGORIES[idx] if idx >= 0 else "Unknown"


def compute_aqi(obs: pd.DataFrame) -> pd.DataFrame:
    """Hourly AQI from hourly concentrations.

    ``obs`` needs a sorted DatetimeIndex and any of the pollutant columns.
    Returns ``aqi``, ``dominant`` (the pollutant that set it) and one ``si_*``
    column per pollutant, on the same index.
    """
    subs = {}
    for pol, (window, min_valid) in AVERAGING.items():
        if pol not in obs.columns:
            continue
        series = pd.to_numeric(obs[pol], errors="coerce")
        mean = series.rolling(window, min_periods=min_valid).mean()
        subs[f"si_{pol}"] = sub_index(pol, mean.to_numpy())
    si = pd.DataFrame(subs, index=obs.index)
    if si.empty or si.shape[1] == 0:
        out = si.copy()
        out.insert(0, "aqi", np.full(len(obs), np.nan, dtype=float))
        out.insert(1, "dominant", np.full(len(obs), None, dtype=object))
        return out
    values = si.to_numpy()
    n_valid = np.sum(~np.isnan(values), axis=1)
    has_pm = np.zeros(len(si), dtype=bool)
    for col in ("si_pm25", "si_pm10"):
        if col in si.columns:
            has_pm |= si[col].notna().to_numpy()
    ok = (n_valid >= 3) & has_pm
    filled = np.where(np.isnan(values), -1.0, values)
    aqi = np.where(ok, filled.max(axis=1), np.nan)
    names = np.array([c[3:] for c in si.columns], dtype=object)
    dominant = np.where(ok, names[filled.argmax(axis=1)], None)
    out = si.copy()
    out.insert(0, "aqi", np.round(aqi, 1))
    out.insert(1, "dominant", dominant)
    return out
